"""Resolve each step's `uses` and check its `with` keys and constant values against the tool."""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from glow.expressions.syntax import OPEN as EXPRESSION_OPEN
from glow.models import Step, Tool, ToolInput, Workflow, iter_steps
from glow.registry import Registry, RegistryError, ResolvedTool
from glow.types import MediaTypeError, parse_media_types
from glow.validate.errors import Code, GlowError
from glow.validate.scopes import format_field


@dataclass(slots=True)
class ToolCheck:
    resolved: dict[str, ResolvedTool] = field(default_factory=dict)
    errors: list[GlowError] = field(default_factory=list)

    @property
    def tools(self) -> dict[str, Tool]:
        return {step_id: resolved.tool for step_id, resolved in self.resolved.items()}


def check_tools(workflow: Workflow, registry: Registry) -> ToolCheck:
    """Resolve every `uses`. A step whose tool does not resolve gets no `with` checks."""
    result = ToolCheck()
    for step in iter_steps(workflow.steps):
        if step.uses is None:
            continue
        try:
            resolved = registry.resolve(step.uses)
        except RegistryError as exc:
            result.errors.append(GlowError(Code.UNKNOWN_TOOL, f"{step.id}.uses", str(exc)))
            continue
        result.resolved[step.id] = resolved
        result.errors.extend(_with_errors(step, resolved.tool))
    return result


def _with_errors(step: Step, tool: Tool) -> Iterator[GlowError]:
    given = step.with_ or {}
    known = ", ".join(tool.inputs) or "none"
    for key in given:
        if key not in tool.inputs:
            yield GlowError(
                Code.UNKNOWN_WITH_KEY,
                f"{step.id}.with.{key}",
                f"{tool.name} has no input '{key}'",
                hint=f"inputs of {tool.name}: {known}",
            )
    for name in tool.required:
        if name not in given and tool.inputs[name].default is None:
            yield GlowError(
                Code.MISSING_REQUIRED_INPUT,
                f"{step.id}.with.{name}",
                f"{tool.name} requires input '{name}'",
                hint=f"set with.{name}",
            )
    for name, value in given.items():
        if name in tool.inputs and value is not None:
            yield from _value_errors(step.id, name, tool.inputs[name], value)


# Keywords whose result on an object or array does not depend on the values
# inside it, so they still apply when a member is an expression.
_SHAPE_KEYWORDS = frozenset({"type", "required", "additionalProperties", "minItems", "maxItems"})


def _value_errors(
    step_id: str, name: str, declaration: ToolInput, value: Any
) -> Iterator[GlowError]:
    """Check the constant parts of a `with` value as glow-exec does when it stages.

    A value that holds a `${{ }}` is only known at run time; the edge checks
    type it instead.
    """
    validator = Draft202012Validator(input_schema(declaration))
    for error in validator.iter_errors(value):
        instance = error.instance
        if _has_expression(instance) and (
            isinstance(instance, str) or error.validator not in _SHAPE_KEYWORDS
        ):
            continue
        yield GlowError(
            Code.INVALID_WITH_VALUE,
            f"{step_id}.{format_field(('with', name, *error.absolute_path))}",
            error.message,
        )


def input_schema(declaration: ToolInput) -> dict[str, Any]:
    """The JSON Schema glow-exec checks a resolved input value against.

    Mirrors glow-exec's manifest.toSchema: data kinds become file or group
    references, and the manifest-only annotations are dropped.
    """
    return _to_schema(declaration.model_dump(by_alias=True, exclude_none=True))


def _to_schema(declaration: dict[str, Any]) -> dict[str, Any]:
    kind = declaration.get("type")
    if kind in ("file", "bundle"):
        return _FILE_REF
    if kind == "group":
        return _GROUP_REF
    schema: dict[str, Any] = {}
    for key, value in declaration.items():
        if key in ("media_type", "category", "remote"):
            continue
        if key in ("items", "additionalProperties") and isinstance(value, dict):
            value = _to_schema(value)
        elif key == "properties":
            value = {prop: _to_schema(nested) for prop, nested in value.items()}
        schema[key] = value
    return schema


_FILE_REF: dict[str, Any] = {
    "anyOf": [
        {"type": "string", "minLength": 1},
        {
            "type": "object",
            "required": ["uri"],
            "properties": {
                "uri": {"type": "string", "minLength": 1},
                "media_type": {"type": "string"},
                "kind": {"type": "string"},
            },
        },
    ]
}
_FILES = {"type": "array", "items": _FILE_REF}
_GROUP_REF: dict[str, Any] = {
    "anyOf": [
        _FILES,
        {"type": "object", "required": ["files"], "properties": {"files": _FILES}},
    ]
}


def _has_expression(value: Any) -> bool:
    if isinstance(value, str):
        return EXPRESSION_OPEN in value
    if isinstance(value, dict):
        return any(_has_expression(member) for member in value.values())
    if isinstance(value, list):
        return any(_has_expression(member) for member in value)
    return False


def media_type_errors(workflow: Workflow, tools: dict[str, Tool]) -> Iterator[GlowError]:
    """Media types written in the workflow file must parse.

    That covers input declarations, `run` and `script` outputs, and the
    constant `media_type` given to fs.group.
    """
    for name, spec in workflow.inputs.items():
        yield from _parse_error(f"inputs.{name}.media_type", spec.media_type)
    for step in iter_steps(workflow.steps):
        for name, declared in (step.outputs or {}).items():
            if not isinstance(declared, str):
                yield from _parse_error(f"{step.id}.outputs.{name}.media_type", declared.media_type)
        tool = tools.get(step.id)
        value = (step.with_ or {}).get("media_type")
        is_constant = isinstance(value, str) and EXPRESSION_OPEN not in value
        if tool is not None and tool.name == "fs.group" and is_constant:
            yield from _parse_error(f"{step.id}.with.media_type", value)


def _parse_error(location: str, value: str | None) -> Iterator[GlowError]:
    try:
        parse_media_types(value)
    except MediaTypeError as exc:
        yield GlowError(Code.INVALID_MEDIA_TYPE, location, str(exc))
