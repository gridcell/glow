"""Resolve each step's `uses` and check its `with` keys against the tool."""

from collections.abc import Iterator
from dataclasses import dataclass, field

from glow.expressions.syntax import OPEN as EXPRESSION_OPEN
from glow.models import Step, Tool, Workflow, iter_steps
from glow.registry import Registry, RegistryError, ResolvedTool
from glow.types import MediaTypeError, parse_media_types
from glow.validate.errors import Code, GlowError


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
