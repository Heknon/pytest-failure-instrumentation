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


def build_index(document: dict) -> dict:
    """An inverted index of every word in every row, built one row at a time.

    Pure Python over a large document: a couple of seconds of a core in the
    middle of a test that otherwise waits. Not a share of the run worth
    naming - the suite is I/O - but a burst, with a start and a length.
    """
    index: dict[str, int] = {}
    for row in document["rows"]:
        words = row["name"].replace("-", " ").split() + [str(row["id"]), str(row["value"])]
        for word in words:
            index[word] = index.get(word, 0) + 1
            # And every substring of every word, so the search box can
            # match the middle of a name, an id or a value.
            for start in range(len(word)):
                for end in range(start + 2, len(word) + 1):
                    gram = word[start:end]
                    index[gram] = index.get(gram, 0) + 1
        for tag in row["tags"]:
            index[tag] = index.get(tag, 0) + 1
    return index
