"""Scenario 2: the cost is in a library the product calls. Expected:
cpu_hotspot LIBRARY_CALL blamed on reports.py in render_report, below
json/encoder.py."""

from demo_product.reports import build_document, render_report


def test_report_renders():
    document = build_document(60_000)
    rendered = render_report(document)
    assert rendered.startswith("{")


def test_report_is_stable():
    document = build_document(60_000)
    assert render_report(document) == render_report(document)
