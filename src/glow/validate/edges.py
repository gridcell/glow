"""The type of every producer and consumer, and the check on each edge (plan section 5.5).

Producers are workflow inputs, tool outputs (from the manifest, with
`media_type_from` resolved against the step's constants), the typed outputs
of `run` and `script` steps, block outputs (`array<T>` after fan-in), loop
variables and `let` names. Consumers are tool inputs, reached through
`properties`, `additionalProperties` and `items` for nested `with` values.

An expression that is exactly one path has the type of that path. Any other
expression is typed by the CEL type checker (`glow.expressions.typecheck`);
what it cannot type is `unknown`, which turns the edge into a runtime check.
An expression whose operations cannot succeed is a `GLOW-E033` error. Text
around an expression makes the value a string.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from glow.expressions.syntax import OPEN as EXPRESSION_OPEN
from glow.expressions.typecheck import TypeCheckError, infer
from glow.models import InputSpec, Tool, ToolInput, ToolOutput, Workflow
from glow.types import (
    STRING,
    Array,
    GlowType,
    Group,
    MediaTypeError,
    Scalar,
    Unknown,
    is_assignable,
    member_type,
    parse_media_types,
    parse_type_or_unknown,
    render,
    resolve_output_type,
)
from glow.validate.errors import Code, GlowError
from glow.validate.scopes import (
    FieldPath,
    InputSymbol,
    LetSymbol,
    LoopSymbol,
    Site,
    Span,
    StepSymbol,
    SymbolTable,
    Use,
)

ARRAY_HINT = "for_each over the array, or .map(...) to extract one value per member"
TYPE_ERROR = "the expression has a type error"
UNRESOLVED = "the reference did not resolve"

Check = Literal["ok", "runtime_check", "unchecked"]


@dataclass(frozen=True, slots=True)
class TypedEdge:
    """One reference feeding one field of a step.

    `type` is the type of the referenced path. `check` compares the type of
    the whole value with `accepts`; `unchecked` means the field declares no
    type, as for `run` and `script` inputs and block outputs.
    """

    site: Site
    use: Use
    span: str
    type: GlowType
    accepts: GlowType | None
    check: Check
    reason: str | None = None


@dataclass(slots=True)
class EdgeReport:
    edges: list[TypedEdge] = field(default_factory=list)
    errors: list[GlowError] = field(default_factory=list)


def check(
    workflow: Workflow,
    table: SymbolTable,
    tools: Mapping[str, Tool],
    skip: frozenset[tuple[str, FieldPath]],
) -> tuple[EdgeReport, "Types"]:
    """Type every edge. Sites in `skip`, or with an unresolved reference, get no errors."""
    types = Types(workflow, table, tools)
    report = EdgeReport()
    for site in table.sites:
        _check_site(site, types, report, checked=site.key not in skip and site.resolved)
    return report, types


def _check_site(site: Site, types: "Types", report: EdgeReport, *, checked: bool) -> None:
    type_errors = types.type_errors(site)
    value_type = types.site_type(site)
    accepts = types.accepted(site)
    status, reason, error = _verdict(site, value_type, accepts)
    if checked:
        report.errors.extend(type_errors)
        # A span with a type error is `unknown`; a shape error would only repeat it.
        if error is not None and not type_errors:
            report.errors.append(error)
    for span in site.spans:
        for use in span.uses:
            if use.symbol is not None:
                edge_type = types.use_type(use)
                report.edges.append(
                    TypedEdge(site, use, span.text, edge_type, accepts, status, reason)
                )


def _verdict(
    site: Site, value_type: GlowType, accepts: GlowType | None
) -> tuple[Check, str | None, GlowError | None]:
    unknown_reason = value_type.reason if isinstance(value_type, Unknown) else None
    if site.field == ("for_each",):
        if isinstance(value_type, Array | Unknown):
            return _known_or_runtime(unknown_reason)
        return "ok", None, _shape_error(Code.FOR_EACH_NOT_ARRAY, site, "array", value_type)
    if site.field == ("if",):
        if _is_boolean(value_type) or unknown_reason is not None:
            return _known_or_runtime(unknown_reason)
        return "ok", None, _shape_error(Code.IF_NOT_BOOLEAN, site, "boolean", value_type)
    if accepts is None:
        return "unchecked", None, None
    result = is_assignable(value_type, accepts)
    if result.status != "mismatch":
        return result.status, result.reason, None
    return "ok", None, _mismatch_error(site, value_type, accepts, result.reason, result.hint)


def _known_or_runtime(reason: str | None) -> tuple[Check, str | None, None]:
    return ("ok", None, None) if reason is None else ("runtime_check", reason, None)


def _is_boolean(glow_type: GlowType) -> bool:
    return isinstance(glow_type, Scalar) and glow_type.schema.get("type") == "boolean"


def _source(site: Site) -> str:
    span = site.whole_span
    return span.text if span is not None else site.value


def _shape_error(code: Code, site: Site, expects: str, got: GlowType) -> GlowError:
    hint = None
    if code is Code.FOR_EACH_NOT_ARRAY:
        hint = "for_each needs an array, such as the output of fs.glob or fs.group"
    return GlowError(
        code, site.location, expects=expects, got=render(got), source=_source(site), hint=hint
    )


def _mismatch_error(
    site: Site, got: GlowType, expects: GlowType, reason: str | None, hint: str | None
) -> GlowError:
    # `reason` starts with the expects/got lines, rendered here with the source.
    detail = "\n".join((reason or "").splitlines()[2:]) or None
    if isinstance(got, Array) and not isinstance(expects, Array):
        hint = ARRAY_HINT
    return GlowError(
        Code.TYPE_MISMATCH,
        site.location,
        detail,
        expects=render(expects),
        got=render(got),
        source=_source(site),
        hint=hint,
    )


class Types:
    """Types of inputs, step outputs, loop variables and lets, computed on demand.

    Results are cached. A value that depends on itself (only possible in a
    workflow that already has a cycle error) is `unknown`.
    """

    def __init__(self, workflow: Workflow, table: SymbolTable, tools: Mapping[str, Tool]) -> None:
        self.workflow = workflow
        self.table = table
        self.tools = tools
        self._cache: dict[tuple[Any, ...], GlowType] = {}
        self._active: set[tuple[Any, ...]] = set()
        self._type_errors: dict[tuple[Any, ...], GlowError] = {}

    def _cached(self, key: tuple[Any, ...], compute: Callable[[], GlowType]) -> GlowType:
        if key in self._cache:
            return self._cache[key]
        if key in self._active:
            return Unknown("the value depends on itself")
        self._active.add(key)
        try:
            result = compute()
        finally:
            self._active.discard(key)
        self._cache[key] = result
        return result

    def input_type(self, name: str) -> GlowType:
        return input_type(self.workflow.inputs[name])

    def outputs(self, step_id: str) -> dict[str, GlowType]:
        """Every output of a step, as seen by later steps."""
        step = self.table.steps[step_id]
        if step.steps is not None or step.run is not None or step.script is not None:
            names: Iterable[str] = (step.outputs or {}).keys()
        elif step_id in self.tools:
            names = self.tools[step_id].outputs.keys()
        else:
            names = ()
        return {name: self.output_type(step_id, name) for name in names}

    def output_type(self, step_id: str, name: str) -> GlowType:
        """The type of `steps.<step_id>.outputs.<name>`: `array<T>` after a fan-out."""
        step = self.table.steps[step_id]
        inner = self._cached(("output", step_id, name), lambda: self._body_output(step_id, name))
        return Array(inner) if step.for_each is not None else inner

    def _body_output(self, step_id: str, name: str) -> GlowType:
        step = self.table.steps[step_id]
        if step.steps is not None:
            site = self.table.site(step_id, ("outputs", name))
            return self.site_type(site) if site is not None else Unknown("no such output")
        if step.run is not None or step.script is not None:
            declared = (step.outputs or {}).get(name)
            if declared is None or isinstance(declared, str):
                return Unknown("no such output")
            return parse_type_or_unknown({"type": declared.type, "media_type": declared.media_type})
        tool = self.tools.get(step_id)
        if tool is None:
            return Unknown("the step's tool did not resolve")
        declaration = tool.outputs.get(name)
        if declaration is None:
            return Unknown("no such output")
        return tool_output_type(tool, name, declaration, step.with_ or {})

    def loop_operand(self, step_id: str) -> GlowType:
        step = self.table.steps[step_id]
        if isinstance(step.for_each, list):
            return Array(Unknown("the members of a literal list are typed at runtime"))
        site = self.table.site(step_id, ("for_each",))
        return self.site_type(site) if site is not None else Unknown(UNRESOLVED)

    def loop_item(self, step_id: str) -> GlowType:
        operand = self.loop_operand(step_id)
        if isinstance(operand, Array):
            return operand.member
        if isinstance(operand, Unknown):
            return operand
        return Unknown("for_each is not over an array")

    def let_type(self, step_id: str, name: str) -> GlowType:
        site = self.table.site(step_id, ("let", name))
        return self.site_type(site) if site is not None else Unknown(UNRESOLVED)

    def site_type(self, site: Site) -> GlowType:
        """The type of a whole value, such as the value of `cog.with.source`."""
        return self._cached(("site", site.key), lambda: self._site_type(site))

    def _site_type(self, site: Site) -> GlowType:
        span = site.whole_span
        if span is None:
            return STRING
        return self.span_type(site, 0)

    def span_type(self, site: Site, index: int) -> GlowType:
        """The type of the expression in one `${{ }}` of a value."""
        key = ("span", site.key, index)
        return self._cached(key, lambda: self._span_type(site, site.spans[index], key))

    def _span_type(self, site: Site, span: Span, key: tuple[Any, ...]) -> GlowType:
        analysis = span.analysis
        if analysis.whole_path is not None:
            use = next(use for use in span.uses if use.reference == analysis.whole_path)
            return self.use_type(use)
        if analysis.tree is None:
            return Unknown(UNRESOLVED)
        uses = {(use.reference.root, use.reference.path): use for use in span.uses}

        def lookup(root: str, path: tuple[Any, ...]) -> GlowType:
            use = uses.get((root, path))
            return self.use_type(use) if use is not None else Unknown(UNRESOLVED)

        try:
            return infer(analysis.tree, analysis.expression, lookup)
        except TypeCheckError as exc:
            self._type_errors[key] = GlowError(
                Code.EXPRESSION_TYPE_ERROR, site.location, f"{exc}, in {span.text}"
            )
            return Unknown(TYPE_ERROR)

    def type_errors(self, site: Site) -> list[GlowError]:
        """Type every expression in the value and return the type errors found."""
        errors = []
        for index in range(len(site.spans)):
            self.span_type(site, index)
            error = self._type_errors.get(("span", site.key, index))
            if error is not None:
                errors.append(error)
        return errors

    def use_type(self, use: Use) -> GlowType:
        """The type of one reference path."""
        path = use.reference.path
        match use.symbol:
            case InputSymbol(name):
                return member_type(self.input_type(name), path[1:])
            case StepSymbol() if path[1] == "results":
                return member_type(Array(Unknown("results are typed at runtime")), path[2:])
            case StepSymbol(step_id):
                return member_type(self.output_type(step_id, str(path[2])), path[3:])
            case LoopSymbol(step_id):
                return member_type(self.loop_item(step_id), path)
            case LetSymbol(step_id, name):
                return member_type(self.let_type(step_id, name), path)
        return Unknown(UNRESOLVED)

    def accepted(self, site: Site) -> GlowType | None:
        """The type the field accepts, or None when the field declares no type."""
        tool = self.tools.get(site.step_id)
        if tool is None or len(site.field) < 2 or site.field[0] != "with":
            return None
        declaration = tool.inputs.get(str(site.field[1]))
        if declaration is None:
            return None
        return accepted_type(declaration, site.field[2:])


def input_type(spec: InputSpec) -> GlowType:
    if spec.type == "uri":
        return Scalar({"type": "string", "format": "uri"})
    return parse_type_or_unknown({"type": spec.type, "media_type": spec.media_type})


def tool_output_type(
    tool: Tool, name: str, declaration: ToolOutput, with_values: Mapping[str, Any]
) -> GlowType:
    if tool.name == "fs.group" and name == "groups":
        return Array(fs_group_type(with_values.get("media_type")))
    defaults = {key: spec.default for key, spec in tool.inputs.items() if spec.default is not None}
    try:
        return resolve_output_type(declaration, with_values, defaults)
    except MediaTypeError as exc:
        return Unknown(str(exc))


def fs_group_type(media_type: Any) -> Group:
    """One fs.group group. Its files take the media type given to the step."""
    if media_type is None:
        return Group()
    if not isinstance(media_type, str) or EXPRESSION_OPEN in media_type:
        return Group(unknown_reason="the media type is not given as a constant")
    try:
        return Group(parse_media_types(media_type))
    except MediaTypeError as exc:
        return Group(unknown_reason=str(exc))


def accepted_type(declaration: ToolInput, path: tuple[str | int, ...]) -> GlowType:
    """The type accepted at `path` below a tool input, for nested `with` values."""
    current: ToolInput | None = declaration
    for part in path:
        current = _child(current, part) if current is not None else None
    if current is None:
        return Unknown("the tool does not declare a type for this field")
    return parse_type_or_unknown(current)


def _child(declaration: ToolInput, part: str | int) -> ToolInput | None:
    if isinstance(part, int):
        return declaration.items
    properties = declaration.properties or {}
    if part in properties:
        return properties[part]
    extra = declaration.additional_properties
    return extra if isinstance(extra, ToolInput) else None
