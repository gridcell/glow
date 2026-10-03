"""Static validation of workflow files (plan section 5.5).

`validate` runs the schema pass from `glow.validation` first and stops there
if it fails, so the checks below always see a valid `Workflow` model:

1. every `uses` resolves, every `with` key exists and required inputs are set;
2. every expression parses as CEL within the cost limit, and every reference
   resolves in its scope (`scopes.py`);
3. references only point at earlier steps and there are no cycles (`graph.py`);
4. every expression type checks and each edge's types are compatible (`edges.py`).

A valid workflow yields the IR (`glow.ir.Workflow`). Tenant rules are not done
yet.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from glow import ir as glow_ir
from glow.builtins import BUILTINS
from glow.lock import LockError
from glow.manifests import ManifestError
from glow.models import Step, Workflow, iter_steps
from glow.registry import Registry, ResolvedTool
from glow.validate import edges, graph, scopes
from glow.validate.errors import Code, GlowError
from glow.validate.tools import check_tools, media_type_errors
from glow.validation import (
    DocumentKind,
    DocumentReadError,
    Problem,
    parse_yaml,
    read_document,
    validate_text,
)

DEFAULT_TOOLPACKS = Path("toolpacks")

__all__ = ["DEFAULT_TOOLPACKS", "Code", "GlowError", "ValidationReport", "check", "validate"]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Schema `problems` and semantic `errors`; `ir` is set only when both are empty."""

    kind: DocumentKind | None
    problems: list[Problem] = field(default_factory=list)
    errors: list[GlowError] = field(default_factory=list)
    ir: glow_ir.Workflow | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and not self.errors

    def messages(self) -> list[str]:
        return [f"error: {problem}" for problem in self.problems] + [
            error.render() for error in self.errors
        ]


def validate(path: Path, toolpacks: Path = DEFAULT_TOOLPACKS) -> ValidationReport:
    """Validate a workflow file or toolpack manifest.

    Toolpack manifests only get the schema pass. Raises `DocumentReadError`
    when the file cannot be read.
    """
    text = read_document(path)
    result = validate_text(text)
    if result.kind != "workflow" or not result.ok:
        return ValidationReport(result.kind, result.problems)
    workflow = Workflow.model_validate(parse_yaml(text))
    try:
        registry = _load_registry(workflow, toolpacks)
    except (DocumentReadError, LockError, ManifestError) as exc:
        message = f"cannot load the toolpack registry from {toolpacks}: {exc}"
        hint = "pass --toolpacks with the directory holding registry.lock.yaml"
        error = GlowError(Code.REGISTRY_UNAVAILABLE, "uses", message, hint=hint)
        return ValidationReport("workflow", errors=[error])
    return check(workflow, registry)


def _load_registry(workflow: Workflow, toolpacks: Path) -> Registry:
    # Built-ins need no registry files, so a workflow using only them loads none.
    names = {step.uses.split("@")[0] for step in iter_steps(workflow.steps) if step.uses}
    if names <= BUILTINS.keys():
        return Registry({})
    return Registry.load(toolpacks)


def check(workflow: Workflow, registry: Registry) -> ValidationReport:
    """Run the semantic checks on a schema-valid workflow."""
    tool_check = check_tools(workflow, registry)
    tools = tool_check.tools
    steps = {step.id: step for step in iter_steps(workflow.steps)}

    def outputs_of(step_id: str) -> Collection[str] | None:
        step = steps[step_id]
        if step.uses is None:
            return (step.outputs or {}).keys()
        tool = tools.get(step_id)
        return None if tool is None else tool.outputs.keys()

    errors = [*tool_check.errors, *media_type_errors(workflow, tools)]
    table = scopes.build(workflow, outputs_of)
    errors += table.errors
    dependency_graph = graph.analyze(table)
    errors += dependency_graph.errors
    edge_report, types = edges.check(workflow, table, tools, dependency_graph.bad_sites)
    errors += edge_report.errors
    if errors:
        return ValidationReport("workflow", errors=errors)
    workflow_ir = _build_ir(
        workflow, table, dependency_graph, types, edge_report, tool_check.resolved
    )
    return ValidationReport("workflow", ir=workflow_ir)


def _build_ir(
    workflow: Workflow,
    table: scopes.SymbolTable,
    dependency_graph: graph.Graph,
    types: edges.Types,
    edge_report: edges.EdgeReport,
    resolved: dict[str, ResolvedTool],
) -> glow_ir.Workflow:
    steps: list[glow_ir.Step] = []

    def add(owner: str | None) -> None:
        for step_id in dependency_graph.order[owner]:
            step = table.steps[step_id]
            steps.append(_ir_step(step, owner, dependency_graph, types, resolved.get(step_id)))
            if step.steps is not None:
                add(step_id)

    add(None)
    return glow_ir.Workflow(
        name=workflow.name,
        inputs={name: types.input_type(name) for name in workflow.inputs},
        steps=steps,
        edges=[_ir_edge(edge) for edge in edge_report.edges],
    )


def _ir_step(
    step: Step,
    parent: str | None,
    dependency_graph: graph.Graph,
    types: edges.Types,
    resolved: ResolvedTool | None,
) -> glow_ir.Step:
    loop = None
    if step.as_ is not None:
        loop = glow_ir.Loop(
            variable=step.as_,
            over=types.loop_operand(step.id),
            item=types.loop_item(step.id),
            max_parallelism=step.max_parallelism,
        )
    return glow_ir.Step(
        id=step.id,
        kind=step.kind,
        parent=parent,
        tool=_tool_identity(resolved) if resolved is not None else None,
        depends_on=dependency_graph.depends_on.get(step.id, []),
        loop=loop,
        let={name: types.let_type(step.id, name) for name in step.let or {}},
        outputs=types.outputs(step.id),
        condition=step.if_,
        staging=step.staging or "copy",
        resources=step.resources,
        timeout=step.timeout,
        retries=step.retries,
    )


def _tool_identity(resolved: ResolvedTool) -> glow_ir.ToolIdentity:
    return glow_ir.ToolIdentity(
        uses=resolved.uses,
        name=resolved.tool.name,
        builtin=resolved.builtin,
        toolpack=resolved.toolpack,
        image=resolved.image,
        digest=resolved.digest,
        manifest_sha256=resolved.manifest_sha256,
    )


def _ir_edge(edge: edges.TypedEdge) -> glow_ir.Edge:
    symbol = edge.use.symbol
    return glow_ir.Edge(
        source=edge.use.reference.text,
        source_step=symbol.step_id if isinstance(symbol, scopes.StepSymbol) else None,
        target_step=edge.site.step_id,
        target=scopes.format_field(edge.site.field),
        expression=edge.span,
        type=edge.type,
        accepts=edge.accepts,
        check=edge.check,
        reason=edge.reason,
    )
