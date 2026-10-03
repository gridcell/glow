from glow.validate.graph import _order as order
from glow.validate.graph import strongly_connected


def test_strongly_connected_finds_cycles_and_singletons() -> None:
    edges = {"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]}
    components = strongly_connected(["a", "b", "c", "d", "e"], edges)
    assert sorted(sorted(component) for component in components) == [
        ["a", "b", "c"],
        ["d"],
        ["e"],
    ]


def test_strongly_connected_handles_long_chains() -> None:
    nodes = [f"s{i}" for i in range(5000)]
    edges = {nodes[i]: [nodes[i + 1]] for i in range(len(nodes) - 1)}
    assert len(strongly_connected(nodes, edges)) == len(nodes)


def test_order_keeps_declaration_order_when_free() -> None:
    assert order(["a", "b", "c"], {"c": ["a"], "b": ["a"]}) == ["a", "b", "c"]


def test_order_puts_producers_first() -> None:
    assert order(["a", "b", "c"], {"a": ["c"]}) == ["b", "c", "a"]


def test_order_appends_steps_left_by_a_cycle() -> None:
    assert order(["a", "b", "c"], {"a": ["b"], "b": ["a"]}) == ["c", "a", "b"]
