"""Scenario 1: a Python loop in product code. Expected: cpu_hotspot PYTHON_CODE
blamed on image_compare.py in is_images_different, owner=product."""

import random

import pytest
from demo_product.image_compare import is_images_different

WIDTH, HEIGHT = 1600, 900


@pytest.fixture(scope="module")
def screenshots():
    random.seed(1)
    before = random.randbytes(WIDTH * HEIGHT * 3)
    after = bytearray(before)
    after[1000:1600] = bytes(600)  # a small change
    return before, bytes(after)


@pytest.mark.parametrize("attempt", range(3))
def test_screen_settles(screenshots, attempt):
    before, after = screenshots
    # A wait-until-changed loop: compare, compare again, compare again.
    for _ in range(3):
        is_images_different(before, after, WIDTH, HEIGHT)
    assert not is_images_different(before, after, WIDTH, HEIGHT)
