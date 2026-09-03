"""Scenario 9: a load that does not stream. Expected: memory_profile
PEAK_OVER_CEILING (the example ini caps a test at 1000 MB), blamed on
loader.py in load_everything, owner=product - the function that was running
while the memory climbed, which is what a resident-memory number alone can
never say."""

from demo_product.loader import load_everything


def test_loads_the_export():
    assert load_everything(800_000) == 32_000_000
