"""Symbol tables: what each `${{ }}` reference can see and resolves to (plan section 5.2).

Scoping rules:

- `inputs.*` is visible everywhere.
- `steps.<id>` is visible to later siblings in the same step list, and to
  everything nested in those siblings.
- Inside a `for_each` step: the `as` variable, the `let` bindings, and the
  block's own earlier steps. The `for_each` operand and `if` are evaluated
  outside the loop, so they see none of these.
- Members of a block are not visible outside it. The block exposes its
  `outputs`, which are evaluated after every member has run.

One pass over the workflow records every expression site, resolves each
reference to a symbol, and collects the dependencies between siblings that
`graph.py` checks for order and cycles.
"""

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from glow.expressions import (
    Analysis,
    ExpressionSyntaxError,
    ExpressionTooCostlyError,
    Reference,
    analyze,
    find_expressions,
)
from glow.models import Step, Workflow, iter_steps
from glow.validate.errors import Code, GlowError

FieldPath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class InputSymbol:
    name: str


@dataclass(frozen=True, slots=True)
class StepSymbol:
    step_id: str


@dataclass(frozen=True, slots=True)
class LoopSymbol:
    """The `as` variable of the for_each step `step_id`."""

    step_id: str


@dataclass(frozen=True, slots=True)
class LetSymbol:
    step_id: str
    name: str


Symbol = InputSymbol | StepSymbol | LoopSymbol | LetSymbol


@dataclass(frozen=True, slots=True)
class Use:
    """A reference and the symbol it resolved to, or None after an error."""

    reference: Reference
    symbol: Symbol | None


@dataclass(frozen=True, slots=True)
class Span:
    """One `${{ ... }}` in a value. `text` is the span as written."""

    text: str
    analysis: Analysis
    uses: tuple[Use, ...]


@dataclass(frozen=True, slots=True)
class Site:
    """A string value holding expressions, such as `cog.with.source`."""

    step_id: str
    field: FieldPath
    value: str
    spans: tuple[Span, ...]

    @property
    def key(self) -> tuple[str, FieldPath]:
        return (self.step_id, self.field)

    @property
    def location(self) -> str:
        return f"{self.step_id}.{format_field(self.field)}"

    @property
    def whole_span(self) -> Span | None:
        """The span when the value is exactly one `${{ }}` and nothing else."""
        if len(self.spans) == 1 and self.value.strip() == self.spans[0].text:
            return self.spans[0]
        return None

    @property
    def resolved(self) -> bool:
        return all(use.symbol is not None for span in self.spans for use in span.uses)


@dataclass(frozen=True, slots=True)
class Dependency:
    """`consumer` uses `producer`; both are in the same step list.

    For a reference from inside a block to a step outside it, `consumer` is
    the block (or enclosing block) that is a sibling of `producer`.
    """

    consumer: str
    producer: str
    site: Site


@dataclass(slots=True)
class SymbolTable:
    steps: dict[str, Step]
    parents: dict[str, str | None]
    lists: dict[str | None, list[str]]
    sites: list[Site] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    errors: list[GlowError] = field(default_factory=list)
    _by_key: dict[tuple[str, FieldPath], Site] = field(default_factory=dict)

    def add_site(self, site: Site) -> None:
        self.sites.append(site)
        self._by_key[site.key] = site

    def site(self, step_id: str, field_path: FieldPath) -> Site | None:
        return self._by_key.get((step_id, field_path))


def format_field(path: FieldPath) -> str:
    text = ""
    for part in path:
        text += f"[{part}]" if isinstance(part, int) else f".{part}" if text else part
    return text


@dataclass(frozen=True, slots=True)
class _Frame:
    """One level of the scope chain.

    `steps` maps the ids of one step list to their index. `consumer` is the
    step of that list whose subtree holds the expression; `position` is its
    index. A frame with no consumer belongs to a block's own `let` (no member
    visible, position -1) or `outputs` (every member visible).
    """

    steps: Mapping[str, int]
    names: Mapping[str, Symbol]
    consumer: str | None
    position: int


OutputsOf = Callable[[str], Collection[str] | None]


def build(workflow: Workflow, outputs_of: OutputsOf) -> SymbolTable:
    """Resolve every reference in the workflow.

    `outputs_of(step_id)` gives a step's output names, or None when they are
    unknown because its tool did not resolve; then output names are not checked.
    """
    builder = _Builder(workflow, outputs_of)
    builder.walk(workflow.steps, owner=None, names={}, outer=())
    return builder.table


class _Builder:
    def __init__(self, workflow: Workflow, outputs_of: OutputsOf) -> None:
        self.inputs = workflow.inputs
        self.outputs_of = outputs_of
        steps = {step.id: step for step in iter_steps(workflow.steps)}
        parents: dict[str, str | None] = dict.fromkeys((step.id for step in workflow.steps), None)
        for step in steps.values():
            parents.update(dict.fromkeys((member.id for member in step.steps or []), step.id))
        self.table = SymbolTable(steps=steps, parents=parents, lists={})
        self._pending: list[tuple[str, str]] = []

    def walk(
        self,
        steps: list[Step],
        owner: str | None,
        names: Mapping[str, Symbol],
        outer: tuple[_Frame, ...],
    ) -> None:
        index = {step.id: position for position, step in enumerate(steps)}
        self.table.lists[owner] = list(index)
        for position, step in enumerate(steps):
            self._step(step, (_Frame(index, names, step.id, position), *outer))

    def _step(self, step: Step, here: tuple[_Frame, ...]) -> None:
        if step.for_each is not None:
            self._value(step.id, ("for_each",), step.for_each, here)
        if step.if_ is not None:
            self._site(step.id, ("if",), step.if_, here)
        members = {member.id: position for position, member in enumerate(step.steps or [])}
        names: dict[str, Symbol] = {}
        if step.as_ is not None:
            names[step.as_] = LoopSymbol(step.id)
        for name, expression in (step.let or {}).items():
            before_members = _Frame(members, dict(names), None, -1)
            self._site(step.id, ("let", name), expression, (before_members, *here))
            names[name] = LetSymbol(step.id, name)
        local = (_Frame({}, names, None, 0), *here)
        self._value(step.id, ("with",), step.with_ or {}, local)
        if step.steps is None:
            return
        self.walk(step.steps, owner=step.id, names=names, outer=here)
        after_members = (_Frame(members, names, None, len(members)), *here)
        for name, expression in (step.outputs or {}).items():
            if isinstance(expression, str):
                self._site(step.id, ("outputs", name), expression, after_members)

    def _value(self, step_id: str, path: FieldPath, value: Any, frames: tuple[_Frame, ...]) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                self._value(step_id, (*path, key), item, frames)
        elif isinstance(value, list):
            for position, item in enumerate(value):
                self._value(step_id, (*path, position), item, frames)
        elif isinstance(value, str):
            self._site(step_id, path, value, frames)

    def _site(self, step_id: str, path: FieldPath, value: str, frames: tuple[_Frame, ...]) -> None:
        location = f"{step_id}.{format_field(path)}"
        try:
            found = find_expressions(value)
        except ExpressionSyntaxError as exc:
            self._error(Code.MALFORMED_EXPRESSION, location, str(exc), source=value)
            return
        if not found:
            return
        analyses = []
        for expression in found:
            text = value[expression.start : expression.end]
            analysis = self._analyze(expression.inner, location, text)
            if analysis is None:
                return
            analyses.append((text, analysis))
        spans = []
        for text, analysis in analyses:
            uses = tuple(
                Use(reference, self._resolve(reference, frames, location, text))
                for reference in analysis.references
            )
            spans.append(Span(text, analysis, uses))
        site = Site(step_id, path, value, tuple(spans))
        self.table.add_site(site)
        for consumer, producer in self._pending:
            self.table.dependencies.append(Dependency(consumer, producer, site))
        self._pending.clear()

    def _analyze(self, inner: str, location: str, source: str) -> Analysis | None:
        try:
            return analyze(inner)
        except ExpressionSyntaxError as exc:
            self._error(Code.MALFORMED_EXPRESSION, location, str(exc), source=source)
        except ExpressionTooCostlyError as exc:
            hint = "move the work into a run or script step"
            self._error(Code.EXPRESSION_TOO_COSTLY, location, str(exc), source=source, hint=hint)
        return None

    def _resolve(
        self, reference: Reference, frames: tuple[_Frame, ...], location: str, source: str
    ) -> Symbol | None:
        if reference.root == "inputs":
            return self._input(reference, location, source)
        if reference.root == "steps":
            return self._step_reference(reference, frames, location, source)
        for frame in frames:
            if reference.root in frame.names:
                return frame.names[reference.root]
        visible = ["inputs", "steps", *sorted({name for frame in frames for name in frame.names})]
        hint = f"names in scope: {', '.join(visible)}"
        self._error(
            Code.UNDEFINED_NAME,
            location,
            f"'{reference.root}' is not an input, a step, a loop variable or a let name",
            source=source,
            hint=hint,
        )
        return None

    def _input(self, reference: Reference, location: str, source: str) -> Symbol | None:
        name = reference.path[0] if reference.path else None
        if isinstance(name, str) and name in self.inputs:
            return InputSymbol(name)
        declared = ", ".join(self.inputs) or "none"
        self._error(
            Code.UNDEFINED_NAME,
            location,
            f"'{reference.text}' is not a declared input",
            source=source,
            hint=f"declared inputs: {declared}",
        )
        return None

    def _step_reference(
        self, reference: Reference, frames: tuple[_Frame, ...], location: str, source: str
    ) -> Symbol | None:
        step_id = reference.path[0] if reference.path else None
        if not isinstance(step_id, str):
            self._error(
                Code.UNDEFINED_NAME,
                location,
                "write steps.<id>.outputs.<name>",
                source=source,
            )
            return None
        frame = next((frame for frame in frames if step_id in frame.steps), None)
        if frame is None:
            self._not_visible(step_id, location, source)
            return None
        if not self._member_ok(reference, step_id, location, source):
            return None
        if frame.consumer is not None:
            self._pending.append((frame.consumer, step_id))
        elif frame.steps[step_id] >= frame.position:
            self._error(
                Code.LATER_STEP,
                location,
                f"steps.{step_id} has not run when let bindings are evaluated",
                source=source,
                hint="use the step in a later step, or in the block's outputs",
            )
            return None
        return StepSymbol(step_id)

    def _not_visible(self, step_id: str, location: str, source: str) -> None:
        if step_id not in self.table.steps:
            self._error(
                Code.UNDEFINED_NAME, location, f"there is no step '{step_id}'", source=source
            )
            return
        block = self.table.parents[step_id]
        self._error(
            Code.BLOCK_MEMBER,
            location,
            f"steps.{step_id} is inside for_each block '{block}' and is not visible here",
            source=source,
            hint=f"add it to the outputs of '{block}' and use steps.{block}.outputs.<name>",
        )

    def _member_ok(self, reference: Reference, step_id: str, location: str, source: str) -> bool:
        rest = reference.path[1:]
        step = self.table.steps[step_id]
        if rest[:1] == ("results",):
            if step.for_each is not None:
                return True
            message = f"steps.{step_id}.results exists only on for_each steps"
            self._error(Code.UNDECLARED_OUTPUT, location, message, source=source)
            return False
        if len(rest) < 2 or rest[0] != "outputs" or not isinstance(rest[1], str):
            message = f"'{reference.text}' does not name an output"
            hint = f"write steps.{step_id}.outputs.<name>"
            self._error(Code.UNDECLARED_OUTPUT, location, message, source=source, hint=hint)
            return False
        outputs = self.outputs_of(step_id)
        if outputs is None or rest[1] in outputs:
            return True
        declared = ", ".join(outputs) or "none"
        self._error(
            Code.UNDECLARED_OUTPUT,
            location,
            f"step '{step_id}' has no output '{rest[1]}'",
            source=source,
            hint=f"outputs of {step_id}: {declared}",
        )
        return False

    def _error(
        self,
        code: Code,
        location: str,
        message: str,
        *,
        source: str | None = None,
        hint: str | None = None,
    ) -> None:
        if source is not None:
            message = f"{message}, in {source}"
        self.table.errors.append(GlowError(code, location, message, hint=hint))
