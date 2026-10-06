"""Each compiled template declares exactly the inputs it uses and reaches only its own tasks.

Checked on every golden workflow, and on workflows that Hypothesis builds
from random nestings of blocks, lets, `if` and references to outer steps,
loop variables and lets.
"""

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from glow.compile import compile_workflow
from tests.compile.conftest import COMPILED, OPTIONS, compiled, load_ir

_INPUT = re.compile(r"inputs\.parameters(?:\.([A-Za-z0-9_-]+)|\['([A-Za-z0-9_-]+)'\])")
_TASK = re.compile(r"tasks\.([a-z0-9-]+)\.")


def problems(workflow: dict[str, Any]) -> list[str]:
    """Unused or undeclared template inputs, out-of-reach tasks and mismatched arguments."""
    templates = {t["name"]: t for t in workflow["spec"]["templates"]}
    found = []
    for name, template in templates.items():
        declared = {p["name"] for p in template.get("inputs", {}).get("parameters", [])}
        body = json.dumps({key: value for key, value in template.items() if key != "inputs"})
        used = {dotted or bracketed for dotted, bracketed in _INPUT.findall(body)}
        found += [f"{name}: input {unused} is unused" for unused in sorted(declared - used)]
        found += [f"{name}: input {missing} is undeclared" for missing in sorted(used - declared)]
        if "dag" not in template:
            continue
        tasks = {task["name"] for task in template["dag"]["tasks"]}
        found += [
            f"{name}: task {t} is out of reach" for t in sorted(set(_TASK.findall(body)) - tasks)
        ]
        for task in template["dag"]["tasks"]:
            found += _argument_problems(name, task, templates[task["template"]])
    return found


def _argument_problems(name: str, task: dict[str, Any], callee: dict[str, Any]) -> list[str]:
    parameters = callee.get("inputs", {}).get("parameters", [])
    accepted = {p["name"] for p in parameters}
    required = {p["name"] for p in parameters if "default" not in p}
    passed = {p["name"] for p in task.get("arguments", {}).get("parameters", [])}
    where = f"{name}/{task['name']}"
    return [
        *(f"{where}: argument {extra} is not an input" for extra in sorted(passed - accepted)),
        *(f"{where}: input {missing} is not passed" for missing in sorted(required - passed)),
    ]


@pytest.mark.parametrize("name", sorted(COMPILED))
def test_golden_workflows_hold_the_invariants(name: str) -> None:
    assert problems(compiled(COMPILED[name])) == []


def test_the_check_finds_each_kind_of_problem() -> None:
    workflow = compiled(COMPILED["block-outer-let"])
    block = next(t for t in workflow["spec"]["templates"] if t["name"] == "per-band-block")
    block["inputs"]["parameters"].append({"name": "loop-stale"})
    block["dag"]["tasks"][0]["arguments"]["parameters"][0]["value"] = "{{tasks.cog.outputs.x}}"
    assert sorted(problems(workflow)) == [
        "per-band-block: input loop-stale is unused",
        "per-band-block: task cog is out of reach",
        "per-source-block/per-band: input loop-stale is not passed",
    ]


class _Builder:
    """Draws a random nesting of blocks and run steps, each referring to what it can see."""

    def __init__(self, data: st.DataObject) -> None:
        self.data = data
        self.count = 0

    def fresh(self, prefix: str) -> str:
        self.count += 1
        return f"{prefix}{self.count}"

    def refs(self, visible: list[str], most: int) -> list[str]:
        return self.data.draw(st.lists(st.sampled_from(visible), max_size=most, unique=True))

    def steps(self, visible: list[str], loops: list[str], depth: int) -> list[dict[str, Any]]:
        steps = []
        visible = list(visible)
        for _ in range(self.data.draw(st.integers(1, 3))):
            if depth < 3 and self.data.draw(st.booleans()):
                step, output = self.block(visible, loops, depth)
            else:
                step, output = self.run_step(visible)
            steps.append(step)
            visible.append(output)
        return steps

    def run_step(self, visible: list[str]) -> tuple[dict[str, Any], str]:
        step_id = self.fresh("s")
        step: dict[str, Any] = {
            "id": step_id,
            "run": "true",
            "with": {f"a{i}": f"${{{{ {ref} }}}}" for i, ref in enumerate(self.refs(visible, 3))},
            "outputs": {"v": {"type": "string"}},
        }
        if self.data.draw(st.booleans()):
            (ref,) = self.refs(visible, 1) or ["inputs.flag"]
            step["if"] = f"${{{{ {ref} != null }}}}"
        return step, f"steps.{step_id}.outputs.v"

    def block(self, visible: list[str], loops: list[str], depth: int) -> tuple[dict[str, Any], str]:
        step_id, variable = self.fresh("b"), self.fresh("x")
        operand = self.data.draw(st.sampled_from(["inputs.items", *loops]))
        step: dict[str, Any] = {"id": step_id, "for_each": f"${{{{ {operand} }}}}", "as": variable}
        if self.data.draw(st.booleans()):
            (ref,) = self.refs(visible, 1) or ["inputs.flag"]
            step["if"] = f"${{{{ {ref} != null }}}}"
        inner = [*visible, variable]
        lets = {}
        for _ in range(self.data.draw(st.integers(0, 2))):
            name = self.fresh("l")
            lets[name] = " ".join(f"${{{{ {ref} }}}}" for ref in self.refs(inner, 2)) or "constant"
            inner.append(name)
        if lets:
            step["let"] = lets
        step["steps"] = self.steps(inner, [*loops, variable], depth + 1)
        last = step["steps"][-1]
        output = "v" if "run" in last else "o"
        step["outputs"] = {"o": f"${{{{ steps.{last['id']}.outputs.{output} }}}}"}
        return step, f"steps.{step_id}.outputs.o"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.data())
def test_random_nestings_hold_the_invariants(data: st.DataObject) -> None:
    builder = _Builder(data)
    document = {
        "name": "random",
        "inputs": {"items": {"type": "array"}, "flag": {"type": "boolean"}},
        "steps": builder.steps(["inputs.items", "inputs.flag"], [], 0),
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "workflow.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        workflow = yaml.safe_load(compile_workflow(load_ir(path), OPTIONS).to_yaml())
    assert problems(workflow) == []
