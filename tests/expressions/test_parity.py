"""The expression fixture shared with glow-exec.

The Go evaluator runs every case in glow-exec/internal/cel/parity_test.go;
`test_case_matches_go` runs the same cases through the Python evaluator.
"""

import json
from typing import Any

import pytest

from glow.expressions import Evaluator, ExpressionEvalError, find_expressions
from tests.conftest import ROOT

FIXTURE = ROOT / "tests" / "fixtures" / "expressions" / "cases.json"


def load() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_cases_are_well_formed() -> None:
    fixture = load()
    names = [case["name"] for case in fixture["cases"]]
    assert len(names) == len(set(names)), "case names must be unique"
    for case in fixture["cases"]:
        assert case["env"] in fixture["env"], case["name"]
        assert ("expression" in case) != ("template" in case), case["name"]
        assert ("result" in case) != ("error" in case), case["name"]


def test_templates_contain_expressions() -> None:
    for case in load()["cases"]:
        if "template" in case:
            assert find_expressions(case["template"]), case["name"]
        else:
            assert "${{" not in case["expression"], case["name"]


@pytest.mark.parametrize("case", load()["cases"], ids=lambda case: case["name"])
def test_case_matches_go(case: dict[str, Any]) -> None:
    evaluator = Evaluator(load()["env"][case["env"]])

    def run() -> Any:
        if "template" in case:
            return evaluator.template(case["template"])
        return evaluator.eval(case["expression"])

    if "error" in case:
        with pytest.raises(ExpressionEvalError, match=case["error"]):
            run()
    else:
        assert run() == case["result"]
