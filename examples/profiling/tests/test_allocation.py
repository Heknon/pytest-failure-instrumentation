"""Scenario 4: allocation-heavy code that keeps the collector busy. Expected:
cpu_hotspot GC_PRESSURE naming this test, alongside the PYTHON_CODE hotspot
for the builder itself."""


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


def test_graph_builds():
    graph = build_graph(400_000)
    assert len(graph) == 400_000
