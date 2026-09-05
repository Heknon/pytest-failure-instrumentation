"""The function from the whiteboard: a per-pixel comparison in Python.

Two screenshots as byte strings, compared one pixel at a time. Correct, and
about a hundred times slower than the same comparison done by the image
library - which is invisible in a unit test and very visible as a worker
sitting at one full core every time a screen is checked.
"""

MIN_DIFF_PERCENTAGE = 2.0


def is_images_different(image1: bytes, image2: bytes, width: int, height: int) -> bool:
    """True if more than MIN_DIFF_PERCENTAGE of pixels differ."""
    differing_pixels = 0
    total_pixels = width * height
    for x in range(width):
        for y in range(height):
            offset = (y * width + x) * 3
            if image1[offset : offset + 3] != image2[offset : offset + 3]:
                differing_pixels += 1
    diff_percentage = (differing_pixels / total_pixels) * 100.0
    return diff_percentage > MIN_DIFF_PERCENTAGE
