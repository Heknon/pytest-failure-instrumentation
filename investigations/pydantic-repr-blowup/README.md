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
DEPTH=14 python 8_fixes.py         # every fix vs every consumer; rich takes ~5 min
pytest test_repr_bomb.py           # the regression test
python 9_formats.py                # sharing vs cycles across every serialiser
python 10_exception_group.py       # the exception tree as a second multiplier
python 11_hermetic.py              # seal(), before and after
pytest test_hermetic.py            # the budget holds for shapes nobody planned for
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

---

# How to fix it

`fixes.py` is drop-in; `test_repr_bomb.py` is the regression test. Every row
below is measured in `8_fixes.py` at depth 14, against all five consumers.

|  | repr | model_dump_json | better_exc | sentry | rich locals |
|---|---|---|---|---|---|
| baseline (`echo: Turn`) | 1.0 MB | 950 KB | 0.11s | 1.0 MB | **145s / 40 MB** |
| `Field(exclude=True, repr=False)` | 414 B | 264 B | ok | 414 B | ok |
| `__repr__` override | 39 B | **950 KB** | ok | 39 B | **139s** |
| store a key, not the object | 481 B | 466 B | ok | 481 B | ok |

## 0. Confirm it, on the real object (30 seconds)

```python
from fixes import shared_nodes, predicted_repr_size
print(predicted_repr_size(state))   # bytes repr() would produce
print(shared_nodes(state)[:5])      # [(expansions, type, fields), ...]
```

Both are O(V+E) and memoised, so they are safe to run on the thing that hung.
On a 41-object graph whose repr is 64 TB they return in 160 microseconds.
If `shared_nodes` is empty, this whole theory is wrong - stop here.

## 1. Stop the bleeding (config only, ship today)

Makes the 10-minute hang impossible even while the data is still wrong:

- unset `BETTER_EXCEPTIONS` (and delete `better_exceptions_hook.pth` if uninstalling)
- `sentry_sdk.init(..., include_local_variables=False)`
- never turn on `rich` `show_locals=True`

This does **not** fix `model_dump_json()`, which is still exponential and is
what produced your write timeout.

## 2. The actual fix: delete the second path

```python
class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo_idx: int | None = None     # a key, not a second pointer
```

Fixes all five consumers, including the serializer. If you genuinely need the
object pointer in memory, the one-line version keeps it and hides it from both
walkers:

```python
    echo: "Turn | None" = Field(default=None, exclude=True, repr=False)
```

Same result, with the caveat that the field no longer serialises, so it will not
round-trip.

## 3. Defence in depth, one edit for the whole codebase

Make your base model non-recursive to print:

```python
from fixes import BoundedReprModel

class LaGuruModel(BoundedReprModel):
    ...
```

Covers `repr()`, logging, better_exceptions and sentry everywhere at once. Be
clear about what it does not cover: **`model_dump_json()`** walks in
pydantic-core and never calls `__repr__`, and **rich's `show_locals`**
introspects attributes directly and ignores `__repr__` too (measured: 139s even
with the override in place). It is a safety net, not a substitute for step 2.

## 4. Make it impossible to reintroduce

```python
from fixes import assert_repr_bounded

def test_agent_state_is_not_a_repr_bomb():
    state = build_production_like_state(turns=50)
    assert_repr_bounded(state, limit=100_000)
```

The failure message names the shared nodes and their expansion counts. The test
is O(V+E) - the suite here detects a 64 TB repr bomb, and the whole file runs in
0.18s - so it can run on every build. That is the part that stops this coming
back, because the bug is invisible at depth 20 and fatal at depth 27.

---

# Why isn't this fixed upstream?

Because it is not a bug. `9_formats.py` puts the same 17-node graph through
every serialiser in reach:

| | sharing (a DAG) | a cycle |
|---|---|---|
| `repr()` | 3,801,190 bytes | `RecursionError` |
| `model_dump_json()` | 3,801,190 bytes | **raises "Circular reference detected"** |
| `json.dumps()` | 4,456,545 bytes | **raises "Circular reference detected"** |
| `pprint.pformat()` | 40,108,163 bytes | handled |
| `pickle.dumps()` | **861 bytes** | fine |
| `yaml.dump()` | **1,380 bytes** | fine |

Every one of them implemented cycle detection. None of them preserve sharing.
That split is deliberate, and you can read the decision in the stdlib:

```python
# json/encoder.py
markerid = id(o)
if markerid in markers:
    raise ValueError("Circular reference detected")
markers[markerid] = o
...
    yield from _iterencode(o, _current_indent_level)
    if markers is not None:
        del markers[markerid]      # <-- this line
```

The encoder **already has** the `id()` table needed to spot a repeated object.
It deletes the entry on the way back out, so the table only ever holds the
current *path*. That catches cycles and lets sharing expand. Remove the `del`
and you would catch this too - and then be unable to emit anything, because
JSON has no syntax for "the node I already wrote".

YAML has that syntax, which is why it is 1,380 bytes:

```yaml
echo: &id003
  echo: &id002
    echo: &id001 {echo: null, idx: 0, prev: null}
    idx: 1
    prev: *id001
  idx: 2
  prev: *id002
idx: 3
prev: *id003
```

`&id001` declares, `*id001` refers back. Pickle does the same thing in binary.
Both formats were designed for object *graphs*. JSON and `repr` were designed
for *trees*, and a tree has exactly one path to every node. Feeding a graph to a
tree serialiser and getting the paths enumerated is the correct answer to the
question that was asked.

CPython's one concession is `reprlib.recursive_repr`, whose docstring says what
it guards: *"Decorator to make a repr function return fillvalue for a recursive
call."* Recursive, i.e. the same object already open in the current call stack.
A cycle. Not sharing.

## So is there anything worth filing?

Not against `repr` or `json` - a memo table there would change the output of
every program in the language, into a format nothing can read back.

Against the **consumers**, yes, and this one is a real defect: better_exceptions'
`format_value` and sentry's `safe_repr` both intend to emit a bounded string
(128 chars; `max_value_length`) and both do unbounded work to get there. The
intent is already "small output"; the implementation just orders the truncation
after the expansion instead of before. That is fixable without changing any
format, and `fixes.py`'s `bounded_repr` is roughly the patch.

Searching the pydantic tracker turns up the *cycle* case repeatedly
(pydantic#9424 "__repr__ recursion", pydantic#524 "Multiple RecursionErrors with
self-referencing models") - the case that already raises. Nothing found for the
sharing case, though one search is not proof of absence.

---

# The exception tree is a second multiplier

An exception object holding sub-exceptions (an `ExceptionGroup`, a task group, a
mock-API fan-out) multiplies the model blow-up by however many sub-exceptions it
carries. `10_exception_group.py`, model depth 12, group of 12:

```
traceback.format_exception (group-aware)      0.00s        10,422 bytes
better_exceptions                             0.04s           980 bytes   <- pre-PEP654, does not descend
sentry (walks every sub-exception)            1.19s     8,790,341 bytes
                    one repr of that model:                237,550 bytes
```

Sentry paid 37x a single repr - 12 sub-exceptions times ~3 frames each. Nothing
about the model changed. So chasing duplicate references field by field is
never finished: the amplification can come from the exception side too.

# The hermetic fix

`hermetic.py`. One call, no model changes, no migration:

```python
import hermetic
hermetic.seal()          # at process start, before anything imports your models
```

It replaces `BaseModel.__repr__`, `__str__` and `__rich_repr__` process-wide
with a **fuel gauge**: one top-level repr gets a budget of N characters, nested
calls spend from it, and when it runs out everything returns `...`. Depth,
sharing, payload size and object count stop mattering, because the budget is on
the work rather than on the shape of the data.

Same scenario as above, model depth 10, group of 6:

| | before | after |
|---|---|---|
| `repr()` | 59,368 B | 4,096 B |
| `f"{model!r}"` | 59,368 B | 4,096 B |
| logging `"%r"` | 59,368 B | 4,096 B |
| sentry + ExceptionGroup | 712,531 B | 49,267 B |
| rich `show_locals` | **34.4s / 9.9 MB** | **0.13s / 38 KB** |
| `model_dump_json()` | 59,368 B | 59,368 B (deliberately untouched) |

`__rich_repr__` has to be patched separately: pydantic implements rich's
protocol, and rich drives its own recursion through it and never calls
`__repr__` at all. That is why the `BoundedReprModel` base class did not help
rich earlier.

## The budget is a real budget

`test_hermetic.py` tries to break it - deep DAGs (depth 200), wide-and-deep with
40-way fan-out, a 10 MB string field, a 5 MB bytes field, a million-element list:

```
worst case: 4096 chars against a 4096 budget -> bounded
```

Two mechanisms, because one is not enough. The fuel gauge bounds the **work**
(it stops the traversal). A hard slice at the top level bounds the **output**
exactly, mopping up the handful of characters each level can overshoot by as the
stack unwinds. The gauge alone leaked to 5,096 on the depth-200 case.

## What it deliberately does not seal

`model_dump_json()`. Silently truncating a serialiser corrupts real data that
something downstream is depending on - worse than the hang. That path gets a
loud guard instead:

```python
hermetic.guard_dumps(state)
# ReprBudgetExceeded: serialising this Turn would expand to ~63,773,821,894,630
# bytes; shared nodes: Turn x1,099,511,627,776, Turn x549,755,813,888, ...
```

The prediction is O(V+E), so the check costs microseconds even when the answer
is 64 TB.

## Hermetic is not a substitute for the data fix

It is a seal, and seals are for things you cannot enumerate: third-party models,
code you have not written yet, the exception side, the next library that decides
to walk your objects. It buys unbounded time. It does not make
`model_dump_json()` fast, and that is still what timed out.

Do both: `seal()` today so nothing can wedge the process again, then remove the
duplicate reference so the serialiser is fast too.
