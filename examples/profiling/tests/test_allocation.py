"""Scenario 4: allocation-heavy code that keeps the collector busy. Expected:
cpu_hotspot GC_PRESSURE naming these tests, alongside the PYTHON_CODE hotspot
for the builder itself. Two graphs rather than one big one: every full
collection walks everything alive, so the collector's cost is the same,
and the peak is half."""

import pytest


def build_graph(nodes: int) -> list:
    # Millions of small container objects, all reachable from each other:
    # exactly what makes generation-2 collections expensive.
    graph = []
    for index in range(nodes):
        node = {"id": index, "edges": [], "meta": {"tags": ["n", str(index)]}}
        if graph:
            graph[-1]["edges"].append(node)
        graph.append(node)
    return graph


@pytest.mark.parametrize("nodes", [600_000, 700_000])
def test_graph_builds(nodes):
    graph = build_graph(nodes)
    assert len(graph) == nodes
