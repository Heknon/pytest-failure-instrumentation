"""The control: ordinary tests that must raise nothing."""

import json


def test_small_work():
    assert json.loads(json.dumps({"a": [1, 2, 3]})) == {"a": [1, 2, 3]}


def test_tiny_allocation():
    data = [bytes(1000) for _ in range(100)]
    assert len(data) == 100
