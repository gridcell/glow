import pytest

from glow.expressions import Reference, analyze


def paths(expression: str) -> list[str]:
    return [reference.text for reference in analyze(expression).references]


@pytest.mark.parametrize(
    ("expression", "root", "path"),
    [
        ("inputs.source", "inputs", ("source",)),
        ("steps.cog.outputs.result", "steps", ("cog", "outputs", "result")),
        ("g.files[0].path", "g", ("files", 0, "path")),
        ("steps.items.results", "steps", ("items", "results")),
        ("scene", "scene", ()),
        ("  g.captures['date']  ", "g", ("captures", "date")),
    ],
)
def test_single_path_is_whole(expression: str, root: str, path: tuple[object, ...]) -> None:
    analysis = analyze(expression)
    assert analysis.whole_path == Reference(root, path, expression.strip())
    assert analysis.references == (analysis.whole_path,)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("steps.count.outputs.total > 0", ["steps.count.outputs.total"]),
        ("date(g.key, '%Y%m%d')", ["g.key"]),
        ("'inputs.x' + \"steps.y\" + r'\\' + name", ["name"]),
        ("'''a ' b''' + x", ["x"]),
        ("size(xs) > 0 && true || null == x", ["xs", "x"]),
        ("steps.s.outputs.items.map(i, i.assets.data.href)", ["steps.s.outputs.items"]),
        ("xs.filter(x, x > limit)", ["xs", "limit"]),
        ("1.5 + n", ["n"]),
        ("a in b", ["a", "b"]),
    ],
)
def test_references_in_larger_expressions(expression: str, expected: list[str]) -> None:
    analysis = analyze(expression)
    assert paths(expression) == expected
    assert analysis.whole_path is None


def test_dynamic_index_ends_the_path_and_scans_the_index() -> None:
    analysis = analyze("xs[inputs.i].path")
    assert analysis.references == (
        Reference("xs", (None,), "xs"),
        Reference("inputs", ("i",), "inputs.i"),
    )
    assert analysis.whole_path is None


def test_member_after_call_is_not_a_root() -> None:
    assert paths("f(x).y") == ["x"]


def test_unterminated_string_stops_scanning() -> None:
    assert paths("a + 'b + c") == ["a"]
