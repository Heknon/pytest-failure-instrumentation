"""The regression test. Point it at your real production state object.

It is O(V+E), so it costs microseconds even on a graph whose repr is terabytes -
which is exactly why it can run on every build.
"""
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).parent))
from fixes import assert_repr_bounded, predicted_repr_size, shared_nodes  # noqa: E402


class Broken(BaseModel):
    idx: int
    prev: "Broken | None" = None
    echo: "Broken | None" = None


class Guarded(BaseModel):
    idx: int
    prev: "Guarded | None" = None
    echo: "Guarded | None" = Field(default=None, exclude=True, repr=False)


def chain(cls, depth):
    turn = cls(idx=0)
    for i in range(1, depth + 1):
        turn = cls(idx=i, prev=turn, echo=turn)
    return turn


def test_prediction_is_exact():
    for depth in (3, 6, 10, 14):
        root = chain(Broken, depth)
        assert predicted_repr_size(root) == len(repr(root))


def test_detects_the_bomb_without_detonating_it():
    root = chain(Broken, 40)  # repr would be 64 TB
    assert predicted_repr_size(root) > 10**12
    worst = shared_nodes(root)
    assert worst[0][0] == 2**40  # the leaf is written out once per path to it


def test_guarded_model_is_flat():
    root = chain(Guarded, 40)
    assert predicted_repr_size(root) < 1000
    assert shared_nodes(root) == []
    assert_repr_bounded(root)


def test_assert_repr_bounded_explains_itself():
    with pytest.raises(AssertionError) as excinfo:
        assert_repr_bounded(chain(Broken, 40))
    message = str(excinfo.value)
    assert "expansions" in message
    assert "duplicate reference" in message
