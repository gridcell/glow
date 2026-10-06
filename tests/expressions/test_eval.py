from typing import Any

import pytest

from glow.expressions import (
    Evaluator,
    ExpressionEvalError,
    ExpressionSyntaxError,
    ExpressionTooCostlyError,
)

# The SST environment of one per_item iteration, built by hand.
SST = {
    "inputs": {"source": "s3://bucket/raw/", "dest": "s3://bucket/stac/"},
    "g": {
        "key": "20240105",
        "captures": {"date": "20240105"},
        "files": [{"path": "/work/in/sst_20240105.nc", "media_type": "application/x-netcdf"}],
    },
    "steps": {"per_item": {"outputs": {"item": ["/work/a.json", "/work/b.json"]}}},
}


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("g.files[0].path", "/work/in/sst_20240105.nc"),
        ("steps.per_item.outputs.item", ["/work/a.json", "/work/b.json"]),
        ("date(g.key, '%Y%m%d')", "2024-01-05T00:00:00Z"),
        ("g.captures['date'] == g.key", True),
        ("g.files.map(f, path.basename(f.path))", ["sst_20240105.nc"]),
        (
            "{'id': 'sst-' + g.key, 'n': size(g.files), 'x': 1.5}",
            {"id": "sst-20240105", "n": 1, "x": 1.5},
        ),
        ("null", None),
        ("duration('90s')", "90s"),
        ("duration('1.5s')", "1.5s"),
        ("timestamp('2024-01-05T00:00:00.250Z')", "2024-01-05T00:00:00.25Z"),
    ],
)
def test_sst_expressions(expression: str, expected: Any) -> None:
    assert Evaluator(SST).eval(expression) == expected


def test_substitute_walks_values() -> None:
    value = {
        "id": "sst-${{ g.key }}",
        "assets": [{"href": "${{ g.files[0].path }}"}],
        "count": "${{ size(steps.per_item.outputs.item) }}",
        "items": "n=${{ steps.per_item.outputs.item }}",
        "plain": 4,
    }
    assert Evaluator(SST).substitute("with", value) == {
        "id": "sst-20240105",
        "assets": [{"href": "/work/in/sst_20240105.nc"}],
        "count": 2,
        "items": 'n=["/work/a.json","/work/b.json"]',
        "plain": 4,
    }


def test_substitute_names_the_location() -> None:
    with pytest.raises(ExpressionEvalError, match=r"^with\.a\[1\]: evaluating \"g\.nope\""):
        Evaluator(SST).substitute("with", {"a": ["ok", "${{ g.nope }}"]})


def test_undeclared_variable_does_not_leak_the_environment() -> None:
    with pytest.raises(ExpressionEvalError) as caught:
        Evaluator(SST).eval("secret")
    assert str(caught.value) == "undeclared reference to 'secret'"


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        ("{1: 'a'}", "map key 1 is not a string"),
        ("b'x'", "is not JSON"),
        ("1.0 / 0.0", "is not a JSON number"),
        ("path", "is not JSON"),
    ],
)
def test_results_must_be_json(expression: str, error: str) -> None:
    with pytest.raises(ExpressionEvalError, match=error):
        Evaluator({}).eval(expression)


@pytest.mark.parametrize("name", ["path", "media", "not-an-identifier", "1x"])
def test_reserved_and_invalid_variable_names(name: str) -> None:
    with pytest.raises(ValueError, match="cannot be used as an expression variable"):
        Evaluator({name: 1})


def test_syntax_and_cost_errors_are_raised_before_evaluation() -> None:
    evaluator = Evaluator(SST)
    with pytest.raises(ExpressionSyntaxError):
        evaluator.eval("g.key +")
    with pytest.raises(ExpressionTooCostlyError):
        evaluator.eval("x" * 5000)


def test_evaluator_failures_are_eval_errors() -> None:
    # cel-python fails inside its own error handling here.
    with pytest.raises(ExpressionEvalError, match="cannot be evaluated"):
        Evaluator({}).eval("[] == {} || matches")
