# LaGuru: 10 minutes and 8 GB inside `get_relevant_values` / `repr`

Reproduction of the incident where an exception handler pinned one core for
~10 minutes and swung RSS between roughly 5 and 8 GB, after a read/write
timeout while a Pydantic model was being serialized.

This is a reproduction of a *mechanism*, not of LaGuru itself - the code here
is not LaGuru's. Every observable in the report is reproduced, but see
"Confirming it on the real process" before treating it as the diagnosis.

## What the mechanism is

`better_exceptions` renders the local variables under every traceback line.
For each frame it parses the source line, collects the `ast.Name` nodes, looks
each one up in `f_locals`/`f_globals`, and formats it (`formatter.py`):

```python
def format_value(self, v):
    try:
        v = repr(v)          # <- full repr materialised first
    ...
    if max_length is not None and len(v) > max_length:
        v = v[:max_length] + '...'   # <- then thrown away down to 128 chars
```

Pydantic's `Representation.__repr__` is a plain recursive tree walk. It has no
memo table, so an object reachable by more than one path is expanded once **per
path**. A model graph that is a DAG rather than a tree therefore has a repr
whose size is exponential in nesting depth, while the graph itself stays tiny.

Two paths to the same object per level is all it takes:

```python
class Turn(BaseModel):
    idx: int
    note: str
    prev: "Turn | None" = None   # path 1 to the previous turn
    echo: "Turn | None" = None   # path 2 to the SAME object
```

That shape shows up naturally in agent/conversation state: a turn that keeps a
pointer to its parent *and* embeds the message or retrieval result that also
holds the parent.

## Measured

`2_dag_scaling.py`, one `Turn` per conversation turn, nothing else:

| turns | objects | `len(repr())` | wall |
|------:|--------:|--------------:|-----:|
| 14 | 15 | 1,409,046 | 0.11s |
| 17 | 18 | 11,272,662 | 0.95s |
| 20 | 21 | 90,181,590 | 6.56s |
| 21 | 22 | 180,363,222 | 14.01s |

Every additional turn doubles both. Extrapolating the same curve: 24 turns is
~1.4 GB and ~2 minutes, 26 turns is ~5.8 GB, 27 turns is ~11 GB.

`3_end_to_end.py` at `DEPTH=24` - httpx `ReadTimeout` raised out of a
`model_dump_json()` + `post()` call chain, then `better_exceptions` renders it:

```
model graph: 26 objects, ~2000 bytes of Turn instances -- nothing looks wrong
timeout raised after 45.812s (serialization itself was fine)

better_exceptions formatting:
  wall clock      : 428.0s   (single core, pure python)
  RSS before      : 2.78 GB
  RSS peak        : 4.17 GB
  RSS swing       : 1.39 GB
  rendered output : 5291 bytes  <-- what all that work produced
  RSS shape       : ▁▁▁▂▂▂▃▃▄▄▄▅▅▆▆▆▇▇███▁▁▂▂▂▃▃▄▄▅▅▅▆▆▆▇▇███▁▁▁▂▂▃▃▃▄▄▅▅▆▆▆▇▇██
```

26 objects. Two kilobytes of actual data. Seven minutes, gigabytes of string,
5 KB of output.

### The parts that match the report

- **One core, ~10 minutes.** Pure-Python string building under the GIL.
- **RAM sawtoothing rather than climbing.** Three ramps in that trace, one per
  frame where the model is a relevant name. `1_amplification.py` counts
  **4 `repr()` calls on the same object for a single exception** - once per
  `ast.Name` occurrence per frame, no dedup. Each one builds a multi-GB string,
  truncates it to 128 characters, and drops it. Within a single ramp the
  `', '.join(...)` at each nesting level holds the parts and the result at the
  same time, so the peak is ~2x the level below before collapsing back.
- **"After a read and write timeout, serializing a Pydantic model."** The
  serializer walks the same DAG: `model_dump_json()` took **45s** on its own
  here, which is a plausible reason the write timed out in the first place. The
  resulting JSON string is then a frame local too, and `format_value` reprs
  *that* in full as well before cutting it to 128 chars (`4_why_not_reprlib.py`:
  0.6s and a full copy for a 200 MB body).

So the timeout and the hang have the same root cause. The slow serialize is
what produced the timeout; the exception handler then walked the same graph
again, four more times, without a length limit.

### What it is not

- **Not a reference cycle.** A true cycle raises `RecursionError` in ~1ms -
  you would have seen a crash, not a hang. (With `sys.setrecursionlimit()`
  raised it segfaults instead, also fast.) Silent sharing is the dangerous case.
- **Not `traceback`.** `traceback.format_exc()` on the same exception is
  0.0009s; it never touches locals. This only happens with
  `better_exceptions` / `rich` / `stackprinter`-style handlers installed.

## The fix

`5_fix_validation.py`, `DEPTH=22`:

```
better_exceptions (as shipped) :    90.98s   peak 1.02 GB (+0.33 GB)   -> 6159 bytes rendered
with bounded format_value     :     0.00s   peak 0.69 GB (+0.00 GB)   -> 6159 bytes rendered
traceback.format_exc()        :   0.0009s
```

Byte-identical output, because everything was being truncated to 128 characters
anyway.

The change is to bound the work *before* doing it instead of truncating after:

```python
class SafeFormatter(better_exceptions.ExceptionFormatter):
    def format_value(self, v):
        s = bounded_repr(v, budget=self._max_length or 128)
        m = self._max_length
        return s if m is None or len(s) <= m else s[:m] + "..."
```

where `bounded_repr` walks `BaseModel.__repr_args__()` with a depth cap and a
running character budget, and slices `str`/`bytes` before `repr()`ing them.
Full implementation in `5_fix_validation.py`.

**`reprlib` is not a substitute.** Its `repr_instance` calls `builtins.repr(x)`
and truncates the result, so `reprlib.Repr(maxlevel=2).repr(model)` still takes
6.1s on the depth-20 model - the limits arrive after the damage
(`4_why_not_reprlib.py`).

Worth doing alongside, in LaGuru itself:

- Give the state model a `__repr__` that does not recurse. Cheapest fix, and it
  also protects logging, pytest assertion output, and anything else that reprs.
- Drop the duplicate reference, or make the second one an id rather than an
  object. This is the actual bug - it costs the serializer too, not just the
  error path.
- Cap `max_length` and consider disabling `better_exceptions` in the service.
  It is a REPL/dev tool; on a request path it turns any exception into an
  unbounded walk of whatever happens to be in scope.

## Confirming it on the real process

Next time it is stuck, before killing it:

```
py-spy dump --pid <pid>          # expect Representation.__repr__ frames, or format_value
```

and on the model itself:

```python
seen = {}
def dupes(o, depth=0):
    if isinstance(o, BaseModel):
        seen[id(o)] = seen.get(id(o), 0) + 1
        for _, v in o.__repr_args__():
            dupes(v, depth + 1)
# any count > 1 is a shared node; the repr cost is the product of those counts
```

If the counts are all 1, the graph is a tree and this is the wrong theory - the
next candidate is simply a very large payload being repr'd once per frame,
which the same bounded `format_value` fixes anyway.

## Running

```
pip install -r requirements.txt
python 1_amplification.py          # repr calls per exception
python 2_dag_scaling.py            # the doubling table
DEPTH=24 python 3_end_to_end.py    # full incident; ~8 min, needs ~5 GB
python 4_why_not_reprlib.py        # cycles vs sharing, and the reprlib trap
DEPTH=22 python 5_fix_validation.py
DEPTH=14 python 6_who_else.py     # who else walks the graph; rich takes ~2 min
python 7_why.py                    # small-model-vs-huge-output, hand-checkable
```

`rss.py` samples `/proc/self/statm` and hard-aborts the process above 9 GB so a
too-large `DEPTH` cannot take the machine down. Start low - the cost doubles
with every step.

---

# "Can I just disable better_exceptions?"

Yes, and you should - but it is a tourniquet, not the fix. `6_who_else.py`
hands the *same* model and the *same* exception to every consumer, depth 14,
15 objects, ~1 KB of real data:

```
-- walkers with no memo table: every shared node re-expanded per path --
pydantic model_dump_json()                         0.031s   1,376,281 bytes
json.dumps(model_dump())                           0.059s   1,605,650 bytes
logger.exception (better_exceptions)               0.188s   1,006 bytes  <- work is invisible in the output
sentry (include_local_variables=True, default)     0.257s   1,343,514 bytes
rich Traceback(show_locals=True)                 112.610s  33,099,143 bytes

-- formatters that never read locals --
traceback.format_exc()                             0.000s   570 bytes
logger.exception (stdlib)                          0.000s   594 bytes
sentry (include_local_variables=False)             0.004s   -
rich Traceback(show_locals=False, default)         0.011s   3,463 bytes

-- walkers that DO carry a memo table: sharing costs nothing --
pickle.dumps()                                     0.000s   920 bytes
copy.deepcopy() then pickle                        0.000s   920 bytes
```

Every one of those numbers doubles per conversation turn.

## How to actually turn it off

`better_exceptions` installs a `better_exceptions_hook.pth` into site-packages
that auto-hooks whenever the `BETTER_EXCEPTIONS` env var is set:

```python
if 'BETTER_EXCEPTIONS' in os.environ:
    import better_exceptions; better_exceptions.hook()
```

So: **unset `BETTER_EXCEPTIONS`** in the service env, and if it was installed
deliberately, uninstall it and delete any leftover `better_exceptions_hook.pth`.

Note what `hook()` does beyond `sys.excepthook` (`better_exceptions/log.py`):

```python
logging.setLoggerClass(BetExcLogger)   # re-patches on every getLogger()
patch_logging()                        # replaces formatter.formatException
```

It replaces `formatException` on stderr `StreamHandler` formatters and installs
a Logger subclass that re-patches on **every logger created afterwards**. So a
plain `logger.exception("upstream failed")` inside an `except` block goes
through it. This almost certainly was the entry point - the timeout was caught
and logged, not unhandled.

## What it does *not* fix

- **`model_dump_json()` blows up on its own.** 45s at depth 24 in
  `3_end_to_end.py`, with no exception handler involved. This is likely what
  produced the write timeout in the first place.
- **Sentry ships `include_local_variables=True` by default**, and
  `sentry_sdk.utils.safe_repr` is literally `repr(value)` inside a
  `try/except` - no length bound at all, and `max_value_length` defaults to
  `None`. It stored a 1.3 MB string for one frame local at depth 14; 86 MB and
  12.4s at depth 20. If Sentry is on, disabling better_exceptions moves the
  bomb, it does not defuse it.
- **`rich` is worse, not better.** With `show_locals=True` it is ~600x slower
  than better_exceptions on the same object. Its `locals_max_length` /
  `locals_max_string` bound containers and strings, not the recursive expansion
  of an arbitrary object. `show_locals=False` is the default and is safe - so
  do not turn it on as a "nicer" replacement.

# "Why the fuck does this happen?"

Because `repr` is a *tree* serialisation of a *graph*, and there is no memo
table anywhere in the chain.

`pickle` and `deepcopy` walk the exact same object and cost nothing, because
they keep a `{id(obj): ...}` memo and emit a back-reference the second time they
meet a node. `repr` cannot do that - there is no syntax for "the object I
printed 4 MB ago" in a human-readable repr, and no syntax for it in JSON either.
So every tree-shaped output format re-expands shared nodes, once per path that
reaches them. With two paths per level that is 2^depth.

Python's only built-in defence is `reprlib.recursive_repr`, and it guards the
wrong thing: it detects *the same object already being repr'd in the current
call stack*, i.e. cycles. Sharing is not a cycle. The walk terminates, the
result is finite, nothing is detectably wrong - it is just astronomically large.
Pydantic's `Representation` does not use even that.

So the failure is quiet by construction:

- the object graph is a few kilobytes and looks fine in a debugger
- there is no error, no warning, no recursion limit hit
- the cost is invisible in the output, because every consumer truncates
  afterwards - better_exceptions produced 1,006 bytes for 0.188s of work
- and it is exponential, so it goes from "fine" to "wedged for ten minutes"
  within about three extra conversation turns

The one honest signal is that `model_dump_json()` got slow first. That was the
warning, and it read as "the upstream is slow" because it showed up as a
timeout.

---

# Small model, big model, or infinite loop?

Small model. `7_why.py`, depth 3, four objects, printable by eye:

```
Turn(idx=3, prev=Turn(idx=2, prev=Turn(idx=1, prev=Turn(idx=0, prev=None, echo=None),
echo=Turn(idx=0, prev=None, echo=None)), echo=Turn(idx=1, prev=Turn(idx=0, ...
```

There is **one** `idx=0` object in memory. It appears in that string **8 times** -
once per path from the root down to it. Four objects, 2^3 leaf printings.

| depth | objects | RAM bytes | times the leaf is printed | repr bytes |
|---:|---:|---:|---:|---:|
| 3 | 4 | 1,056 | 8 | 439 |
| 10 | 11 | 2,904 | 1,024 | 59,368 |
| 14 | 15 | 3,960 | 16,384 | 950,278 |
| 18 | 19 | 5,016 | 262,144 | 15,204,838 |
| 22 | 23 | 6,072 | 4,194,304 | 243,277,798 |

The left column grows by one object. The right column doubles.

**Not a big model.** At depth 22 the entire graph is 23 objects and 6,072 bytes,
holding 23 integers. There is no payload in it at all.

**Not an infinite loop.** It terminates - 22.1s, returns normally, 243 MB of
string. It makes steady forward progress the whole time. That is why nothing
catches it: no recursion limit, no error, no hang detector. A real cycle *is*
effectively infinite and Python catches that in 1ms with `RecursionError`
(`4_why_not_reprlib.py`). This is worse precisely because it is finite.

**What it actually is: a decompression bomb.** The object graph is a *compressed*
tree, and pointers are the compression - `prev` and `echo` are 16 bytes that
mean "that entire subtree, twice". `repr` has no syntax for a pointer, so it
cannot preserve the sharing; it has to write the subtree out again. The output
size is the number of root-to-leaf **paths**, not the number of objects.

6 KB in, 243 MB out, at a compression ratio of about 40,000:1. Add one more
conversation turn and it is 486 MB. Four more and you are at your 8 GB.
