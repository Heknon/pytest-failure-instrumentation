"""The seal has to hold for shapes nobody anticipated. That is the whole claim."""
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import hermetic  # noqa: E402

LIMIT = 4096


class Turn(BaseModel):
    idx: int
    prev: "Turn | None" = None
    echo: "Turn | None" = None


class Wide(BaseModel):
    a: str = ""
    kids: list = []
    payload: bytes = b""
    nested: "Wide | None" = None


@pytest.fixture(autouse=True)
def sealed():
    hermetic.seal(limit=LIMIT)
    yield
    hermetic.unseal()


def chain(depth):
    turn = Turn(idx=0)
    for i in range(1, depth + 1):
        turn = Turn(idx=i, prev=turn, echo=turn)
    return turn


def wide(depth, width=40):
    node = Wide(a="x" * 5000, payload=b"\xff" * 100_000, kids=list(range(width)))
    for _ in range(depth):
        node = Wide(a="y" * 5000, kids=[node] * width, nested=node, payload=b"\x00" * 100_000)
    return node


@pytest.mark.parametrize("obj", [
    chain(10), chain(30), chain(200),
    wide(5), wide(20),
    Wide(a="z" * 10_000_000),
    Wide(payload=b"\xfe" * 5_000_000),
    Wide(kids=list(range(1_000_000))),
], ids=["dag10", "dag30", "dag200", "wide5", "wide20", "huge_str", "huge_bytes", "huge_list"])
def test_repr_never_exceeds_budget(obj):
    assert len(repr(obj)) <= LIMIT
    assert len(str(obj)) <= LIMIT
    assert len("%r" % (obj,)) <= LIMIT
    assert len(f"{obj!r}") <= LIMIT


def test_rich_cannot_walk_the_graph():
    rich_repr = list(Turn(idx=1, prev=chain(30), echo=chain(30)).__rich_repr__())
    for _, value in rich_repr:
        assert not isinstance(value, BaseModel), "rich would recurse into this"


def test_unseal_restores_the_original():
    hermetic.unseal()
    assert len(repr(chain(14))) > LIMIT      # the bomb is back
    hermetic.seal(limit=LIMIT)
    assert len(repr(chain(14))) <= LIMIT


def test_guard_dumps_refuses_the_bomb_and_says_why():
    with pytest.raises(hermetic.ReprBudgetExceeded) as excinfo:
        hermetic.guard_dumps(chain(40))
    assert "shared nodes" in str(excinfo.value)


def test_guard_dumps_passes_healthy_models_through():
    assert hermetic.guard_dumps(Turn(idx=1)) == Turn(idx=1).model_dump_json()


def test_seal_is_idempotent_and_thread_local_state_resets():
    hermetic.seal(limit=LIMIT)
    hermetic.seal(limit=LIMIT)
    assert len(repr(chain(30))) <= LIMIT
    assert len(repr(chain(30))) <= LIMIT      # second call must not inherit spent fuel


class Thin(BaseModel):
    """Cheap per level: short name, one field. Maximises descent per character."""
    n: "Thin | None" = None


def thin(depth):
    node = Thin()
    for _ in range(depth):
        node = Thin(n=node)
    return node


@pytest.mark.parametrize("limit", [4096, 65536, 1_000_000])
@pytest.mark.parametrize("depth", [500, 5000, 20000])
def test_no_recursion_error_at_any_depth_or_budget(limit, depth):
    """Fuel alone bounds width, not descent - a bigger budget buys a DEEPER
    descent, so this used to raise RecursionError inside the exception handler.
    The depth cap is what makes the seal hold."""
    hermetic.unseal()
    hermetic.seal(limit=limit)
    assert len(repr(thin(depth))) <= limit
    assert len(repr(chain(depth))) <= limit


def test_seal_also_survives_a_real_cycle():
    a = Turn(idx=1)
    b = Turn(idx=2, prev=a)
    a.prev = b
    assert len(repr(a)) <= LIMIT      # unsealed this is a RecursionError


def test_depth_cap_alone_would_not_be_enough():
    """2 recursive fields x max_depth 12 is still 2^12 expansions. The budget is
    what catches that; the depth cap is what catches runaway descent. Both."""
    hermetic.unseal()
    hermetic.seal(limit=10_000_000, max_depth=12)
    assert len(repr(chain(30))) > 100_000        # depth cap alone leaves this much
    hermetic.unseal()
    hermetic.seal(limit=LIMIT, max_depth=12)
    assert len(repr(chain(30))) <= LIMIT         # budget closes it
