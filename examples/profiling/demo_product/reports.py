"""A report renderer whose cost is in the library it calls, not its own lines.

``json.dumps`` with ``indent`` takes the pure-Python encoder rather than the C
one, so a large document costs Python frames in ``json/encoder.py``. The
profile charges the time to ``render_report`` - the product function that
made the call - and says which library it is under.
"""

import json


def build_document(rows: int) -> dict:
    return {
        "rows": [
            {"id": index, "name": f"row-{index}", "tags": ["a", "b", "c"], "value": index * 1.5}
            for index in range(rows)
        ]
    }


def render_report(document: dict) -> str:
    return json.dumps(document, indent=2, sort_keys=True)
