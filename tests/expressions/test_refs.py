import pytest

from glow.expressions import ExpressionSyntaxError, Reference, analyze


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


def test_dynamic_index_is_none_and_its_references_are_collected() -> None:
    analysis = analyze("xs[inputs.i].path")
    assert analysis.references == (
        Reference("xs", (None, "path"), "xs[inputs.i].path"),
        Reference("inputs", ("i",), "inputs.i"),
    )
    assert analysis.whole_path is None


def test_member_after_call_is_not_a_root() -> None:
    assert paths("f(x).y") == ["x"]


def test_function_namespaces_are_not_references() -> None:
    assert paths("path.basename(g.files[0].uri) + media.ext(x)") == ["g.files[0].uri", "x"]


def test_namespace_without_a_call_is_a_reference() -> None:
    assert paths("path.sep") == ["path.sep"]


def test_macro_variable_is_bound_only_in_its_body() -> None:
    assert paths("xs.map(x, x.a) + [x]") == ["xs", "x"]


def test_nested_macros_bind_both_variables() -> None:
    assert paths("xs.exists(x, x.ys.all(y, y > x.min))") == ["xs"]


def test_parenthesized_path_is_whole() -> None:
    analysis = analyze("(inputs.source)")
    assert analysis.whole_path == Reference("inputs", ("source",), "inputs.source")


def test_dot_qualified_identifier() -> None:
    assert paths(".inputs.source") == [".inputs.source"]


@pytest.mark.parametrize("expression", ["a + 'b + c", "a +", "(a", "a ? b", "}"])
def test_invalid_cel_is_a_syntax_error(expression: str) -> None:
    with pytest.raises(ExpressionSyntaxError, match="not valid CEL"):
        analyze(expression)
