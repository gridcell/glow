"""Built-in steps that run inside the engine instead of a toolpack image.

The local runner calls `run_builtin` in-process. In a cluster the engine
image runs `glow-builtin <name>` under glow-exec, which follows the tool
contract: read `/work/inputs.json`, write `/work/outputs.json`.
"""

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from glow.builtins.catalog import BUILTINS
from glow.builtins.fs import BuiltinError, glob, group

__all__ = ["BUILTINS", "BuiltinError", "check_inputs", "run_builtin", "run_in_work_dir"]

_IMPLEMENTATIONS: Mapping[str, Callable[..., dict[str, Any]]] = {
    "fs.group": group,
    "fs.glob": glob,
}

_PYTHON_TYPES: Mapping[str, type | tuple[type, ...]] = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
}


def check_inputs(name: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Apply defaults and check the inputs of built-in `name`.

    Raises `BuiltinError` naming every unknown, missing or mistyped input.
    """
    tool = BUILTINS.get(name)
    if tool is None:
        raise BuiltinError(f"{name!r} is not a built-in")
    values = {key: spec.default for key, spec in tool.inputs.items() if spec.default is not None}
    values.update({key: value for key, value in inputs.items() if value is not None})
    problems = []
    for key in sorted(values):
        spec = tool.inputs.get(key)
        if spec is None:
            problems.append(f"input {key!r} is not declared by {name}")
            continue
        expected = _PYTHON_TYPES.get(spec.type)
        value = values[key]
        if expected is not None and (
            not isinstance(value, expected) or (spec.type != "boolean" and isinstance(value, bool))
        ):
            problems.append(f"input {key!r} must be a {spec.type}, got {json.dumps(value)[:200]}")
    problems += [f"input {key!r} is required" for key in tool.required or [] if key not in values]
    if problems:
        raise BuiltinError("; ".join(problems))
    return values


def run_builtin(name: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Run built-in `name` and return its outputs."""
    values = check_inputs(name, inputs)
    return _IMPLEMENTATIONS[name](**values)


def run_in_work_dir(name: str, work_dir: Path | None = None) -> None:
    """The tool contract: read inputs.json and write outputs.json in the work directory."""
    work = work_dir or Path(os.environ.get("GLOW_WORK_DIR", "/work"))
    try:
        inputs = json.loads((work / "inputs.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuiltinError(f"cannot read {work / 'inputs.json'}: {exc}") from None
    if not isinstance(inputs, dict):
        raise BuiltinError("inputs.json must hold a JSON object")
    outputs = run_builtin(name, inputs)
    (work / "outputs.json").write_text(json.dumps(outputs), encoding="utf-8")
