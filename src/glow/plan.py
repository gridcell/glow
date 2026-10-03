"""Text rendering of a validated workflow for `glow plan`.

Steps appear in dependency order, members indented under their block. Each
step lists its staging mode, resources and every incoming edge with its type.
Edges that are checked at runtime instead of statically are marked so an
author can tighten them.
"""

from glow.ir import Edge, Step, Workflow
from glow.models import Resources
from glow.types import render

INDENT = "  "


def render_plan(workflow: Workflow) -> str:
    runtime = sum(edge.check == "runtime_check" for edge in workflow.edges)
    lines = [
        f"workflow {workflow.name}: {len(workflow.steps)} steps, "
        f"{len(workflow.edges)} edges, {runtime} checked at runtime",
        "",
    ]
    blocks = {step.parent for step in workflow.steps}
    depth: dict[str, int] = {}
    for step in workflow.steps:
        depth[step.id] = 0 if step.parent is None else depth[step.parent] + 1
        lines.extend(_step_lines(workflow, step, INDENT * depth[step.id], step.id in blocks))
    return "\n".join(lines)


def _step_lines(workflow: Workflow, step: Step, indent: str, is_block: bool) -> list[str]:
    inner = indent + INDENT
    lines = [f"{indent}{step.id}  {_body(step, is_block=is_block)}"]
    if step.depends_on:
        lines.append(f"{inner}after: {', '.join(step.depends_on)}")
    if step.condition is not None:
        lines.append(f"{inner}condition: {step.condition}")
    lines.append(f"{inner}staging: {step.staging}, resources: {_resources(step.resources)}")
    lines.extend(f"{inner}{_edge(edge)}" for edge in workflow.edges_into(step.id))
    return lines


def _body(step: Step, *, is_block: bool) -> str:
    parts = []
    if step.loop is not None:
        parts.append(f"for_each {step.loop.variable} in {render(step.loop.over)}")
    if step.tool is not None:
        image = "built-in" if step.tool.builtin else step.tool.image
        if step.tool.digest is not None:
            image = f"{image}@{step.tool.digest}"
        parts.append(f"{step.tool.uses} ({image})")
    elif not is_block:
        parts.append("inline script" if step.kind == "for_each" else step.kind)
    return ": ".join(parts)


def _resources(resources: Resources | None) -> str:
    if resources is None:
        return "default"
    parts = []
    for name, spec in (("requests", resources.requests), ("limits", resources.limits)):
        if spec is not None:
            values = " ".join(
                f"{key}={value}" for key, value in spec.model_dump().items() if value is not None
            )
            parts.append(f"{name} {values}")
    return ", ".join(parts) or "default"


def _edge(edge: Edge) -> str:
    text = f"{edge.target}: {render(edge.type)}  <- {edge.source}"
    if edge.check == "runtime_check":
        text += f"  [runtime check: {edge.reason}]"
    return text
