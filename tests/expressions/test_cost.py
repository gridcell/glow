import pytest

from glow.expressions import ExpressionTooCostlyError, analyze
from glow.expressions.cel import (
    MAX_EXPRESSION_LENGTH,
    MAX_MACRO_NESTING,
    MAX_NESTING,
    MAX_OPERATIONS,
)

REALISTIC = (
    "steps.search.outputs.items"
    ".filter(i, i.properties.cloud < 20 && has(i.assets.visual))"
    ".map(i, {'href': i.assets.visual.href, 'id': path.stem(i.id)})"
)


def test_realistic_expression_is_within_the_limit() -> None:
    analyze(REALISTIC)


def test_two_nested_macros_are_allowed() -> None:
    assert MAX_MACRO_NESTING == 2
    analyze("xs.map(x, x.ys.filter(y, y > 0))")


def test_chained_macros_do_not_nest() -> None:
    analyze("xs.filter(x, x > 0).map(x, x * 2).filter(x, x < 9).exists(x, x == 4)")


def test_three_nested_macros_are_too_costly() -> None:
    with pytest.raises(ExpressionTooCostlyError, match="nests 3 macros"):
        analyze("xs.map(a, xs.map(b, xs.exists(c, a + b == c)))")


def test_macro_in_receiver_position_does_not_count_as_nested() -> None:
    analyze("xs.map(a, a.ys.map(b, b).filter(c, c > 0).size())")


def test_too_many_operations() -> None:
    expression = " + ".join(["x"] * 80)
    assert len(expression) < MAX_EXPRESSION_LENGTH
    with pytest.raises(ExpressionTooCostlyError, match=f"the limit is {MAX_OPERATIONS}"):
        analyze(expression)


def test_too_deep() -> None:
    with pytest.raises(ExpressionTooCostlyError, match=f"more than {MAX_NESTING} levels"):
        analyze("(" * 100 + "x" + ")" * 100)


def test_too_long() -> None:
    with pytest.raises(ExpressionTooCostlyError, match="characters long"):
        analyze("'" + "a" * MAX_EXPRESSION_LENGTH + "'")
