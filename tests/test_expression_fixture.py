"""Shape checks for the expression fixture shared with glow-exec.

The Go evaluator runs every case in glow-exec/internal/cel/parity_test.go.
The Python evaluator will run the same cases once it exists.
"""

import json
from pathlib import Path

from glow.expressions import find_expressions

FIXTURE = Path(__file__).parent / "fixtures" / "expressions" / "cases.json"


def load() -> dict:
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
