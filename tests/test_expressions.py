import pytest

from glow.expressions import ExpressionSpan, ExpressionSyntaxError, find_expressions


def test_no_expressions() -> None:
    assert find_expressions("plain value") == []


def test_whole_value_expression() -> None:
    value = "${{ inputs.source }}"
    assert find_expressions(value) == [ExpressionSpan(0, len(value), "inputs.source")]


def test_embedded_and_multiple_expressions() -> None:
    value = "sst-${{ g.key }}-${{date(g.key, '%Y%m%d')}}.tif"
    spans = find_expressions(value)
    assert [span.inner for span in spans] == ["g.key", "date(g.key, '%Y%m%d')"]
    assert [value[span.start : span.end] for span in spans] == [
        "${{ g.key }}",
        "${{date(g.key, '%Y%m%d')}}",
    ]


def test_braces_inside_expression() -> None:
    spans = find_expressions("${{ {'a': 1}.a }}")
    assert [span.inner for span in spans] == ["{'a': 1}.a"]


def test_unterminated_expression() -> None:
    with pytest.raises(ExpressionSyntaxError, match="offset 4"):
        find_expressions("pre ${{ inputs.source ")
