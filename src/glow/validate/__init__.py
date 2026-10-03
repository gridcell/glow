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
from typing import Any

from glow import ir as glow_ir
from glow.builtins import BUILTINS
from glow.lock import LockError
from glow.manifests import ManifestError
from glow.models import Step, Workflow, iter_steps
from glow.registry import Registry, ResolvedTool
from glow.types import Array, Bundle, File, GlowType, Group, Scalar
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

# Names of the tool specs synthesized for inline `run` and `script` steps.
RUN_TOOL = "glow.run"
SCRIPT_TOOL = "glow.script"

_JSON_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array"})

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
        defaults={
            name: spec.default for name, spec in workflow.inputs.items() if spec.default is not None
        },
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
            operand=step.for_each if step.for_each is not None else [],
            over=types.loop_operand(step.id),
            item=types.loop_item(step.id),
            max_parallelism=step.max_parallelism,
        )
    return glow_ir.Step(
        id=step.id,
        kind=step.kind,
        parent=parent,
        tool=_tool_identity(resolved) if resolved is not None else None,
        tool_spec=_tool_spec(step, types, resolved),
        raw_with=step.with_ or {},
        run=step.run,
        script=step.script,
        depends_on=dependency_graph.depends_on.get(step.id, []),
        loop=loop,
        let={name: types.let_type(step.id, name) for name in step.let or {}},
        let_values=dict(step.let or {}),
        outputs=types.outputs(step.id),
        block_outputs={
            name: value
            for name, value in (step.outputs or {}).items()
            if step.steps is not None and isinstance(value, str)
        },
        condition=step.if_,
        staging=step.staging or "copy",
        resources=step.resources,
        timeout=step.timeout,
        retries=step.retries,
        secrets=step.secrets or [],
    )


def _tool_spec(
    step: Step, types: edges.Types, resolved: ResolvedTool | None
) -> dict[str, Any] | None:
    if resolved is not None:
        return resolved.tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    if step.run is None and step.script is None:
        return None
    inputs = {}
    for key, value in (step.with_ or {}).items():
        declaration = _with_declaration(step.id, key, value, types)
        if declaration is not None:
            inputs[key] = declaration
    outputs = {
        name: declaration.model_dump(mode="json", exclude_none=True)
        for name, declaration in (step.outputs or {}).items()
        if not isinstance(declaration, str)
    }
    name = RUN_TOOL if step.run is not None else SCRIPT_TOOL
    return {"name": name, "inputs": inputs, "outputs": outputs}


def _with_declaration(
    step_id: str, key: str, value: Any, types: edges.Types
) -> dict[str, Any] | None:
    """The input declaration of one `with` key of a run or script step.

    An empty declaration accepts any value; glow-exec checks it at runtime.
    """
    if value is None:
        return None
    site = types.table.site(step_id, ("with", key))
    if site is not None:
        return _declaration(types.site_type(site))
    for python_type, json_type in _LITERAL_TYPES:
        if isinstance(value, python_type):
            return {"type": json_type}
    return {}


# bool before int: a bool is an int in Python.
_LITERAL_TYPES: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (list, "array"),
    (dict, "object"),
)


def _declaration(glow_type: GlowType) -> dict[str, Any]:
    match glow_type:
        case File() | Bundle() | Group():
            return {"type": glow_type.kind}
        case Array():
            return {"type": "array"}
        case Scalar(schema) if str(schema.get("type")) in _JSON_TYPES:
            return {"type": schema["type"]}
    return {}


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


_SYMBOL_KINDS: dict[type[scopes.Symbol], glow_ir.SymbolKind] = {
    scopes.InputSymbol: "input",
    scopes.StepSymbol: "step",
    scopes.LoopSymbol: "loop",
    scopes.LetSymbol: "let",
}


def _ir_edge(edge: edges.TypedEdge) -> glow_ir.Edge:
    symbol = edge.use.symbol
    assert symbol is not None, "edges are only built for resolved references"
    binds = isinstance(symbol, scopes.LoopSymbol | scopes.LetSymbol)
    return glow_ir.Edge(
        source=edge.use.reference.text,
        source_step=symbol.step_id if isinstance(symbol, scopes.StepSymbol) else None,
        symbol=_SYMBOL_KINDS[type(symbol)],
        binder=symbol.step_id if binds else None,
        target_step=edge.site.step_id,
        target=scopes.format_field(edge.site.field),
        expression=edge.span,
        type=edge.type,
        accepts=edge.accepts,
        check=edge.check,
        reason=edge.reason,
    )
