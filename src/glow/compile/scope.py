"""What a compiled step reads from outside its own task (plan section 6.5).

An Argo template sees only its own inputs and the workflow parameters, so
the compiler threads each outer value a block uses down to the steps that
use it. `environment` computes what the pod of one step needs: the loop
variables, `let` bindings and upstream steps its expressions use. That
includes the `if` of each enclosing block, which every member evaluates, and
the references of each let it uses, because glow-exec evaluates the lets in
the pod.

These shapes are still rejected with `GLOW-E050`:

- `steps.<id>.results`;
- an `if` that is not one `${{ }}`, or that holds `{{`;
- a block output that is not one member output, written
  `${{ steps.<id>.outputs.<name> }}`;
- a step that needs two variables with the same name. This happens when a
  let or block `if` uses an outer name that an inner loop variable or let
  shadows, because glow-exec has one flat set of variables.
"""

import re
from dataclasses import dataclass

from glow import ir
from glow.expressions import ExpressionSyntaxError, find_expressions
from glow.validate.errors import Code, GlowError

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BLOCK_OUTPUT = re.compile(
    r"\s*\$\{\{\s*steps\.([A-Za-z_][A-Za-z0-9_]*)\.outputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*"
)
_RESULTS = re.compile(r"steps\.[A-Za-z_][A-Za-z0-9_]*\.results\b")
# Fields that glow-exec evaluates in the pod of their step. A for_each
# operand becomes Argo's withParam, and a block output a valueFrom.
_POD_FIELDS = ("with", "if")


@dataclass(frozen=True, slots=True)
class Environment:
    """What the pod of one step needs, each part in evaluation order.

    `loops` are the for_each steps whose loop variable is used, outermost
    first. `lets` are `(step id, name)` pairs, outermost block first and in
    definition order within one step, so each let comes after the lets it
    uses. `steps` are the producers whose outputs.resolved.json is used, in
    workflow order.
    """

    loops: tuple[str, ...]
    lets: tuple[tuple[str, str], ...]
    steps: tuple[str, ...]


def check(workflow: ir.Workflow) -> list[GlowError]:
    """Every reference the compiler cannot express yet."""
    blocks = {step.parent for step in workflow.steps if step.parent is not None}
    errors = []
    for edge in workflow.edges:
        if edge.symbol == "step" and _RESULTS.match(edge.source):
            message = f"steps.{edge.source_step}.results is not yet supported by the compiler"
            location = f"{edge.target_step}.{edge.target}"
            errors.append(
                GlowError(Code.NOT_YET_SUPPORTED, location, f"{message}, in {edge.expression}")
            )
    for step in workflow.steps:
        errors += _condition_errors(step)
        for name, value in step.block_outputs.items():
            if block_output_source(workflow, step, value) is None:
                message = (
                    "a compiled block output must be one member output, "
                    f"${{{{ steps.<id>.outputs.<name> }}}}; got {value}"
                )
                location = f"{step.id}.outputs.{name}"
                errors.append(GlowError(Code.NOT_YET_SUPPORTED, location, message))
        if step.id not in blocks:
            errors += _shadowing_errors(workflow, step, environment(workflow, step))
    return errors


def environment(workflow: ir.Workflow, step: ir.Step) -> Environment:
    """The loop variables, lets and upstream steps the pod of `step` needs."""
    pending = [edge for edge in workflow.edges_into(step.id) if _field(edge) in _POD_FIELDS]
    owner = step.parent
    while owner is not None:
        pending += [edge for edge in workflow.edges_into(owner) if _field(edge) == "if"]
        owner = workflow.step(owner).parent
    loops: set[str] = set()
    lets: set[tuple[str, str]] = set()
    steps: set[str] = set()
    while pending:
        edge = pending.pop()
        if edge.symbol == "step" and edge.source_step is not None:
            steps.add(edge.source_step)
        elif edge.symbol == "loop" and edge.binder is not None:
            loops.add(edge.binder)
        elif edge.symbol == "let" and edge.binder is not None:
            let = (edge.binder, _root(edge.source))
            if let not in lets:
                lets.add(let)
                target = f"let.{let[1]}"
                pending += [e for e in workflow.edges_into(let[0]) if e.target == target]
    order = {each.id: index for index, each in enumerate(workflow.steps)}
    depth = _depths(workflow)

    def let_order(let: tuple[str, str]) -> tuple[int, int]:
        return depth[let[0]], list(workflow.step(let[0]).let_values).index(let[1])

    return Environment(
        loops=tuple(sorted(loops, key=depth.__getitem__)),
        lets=tuple(sorted(lets, key=let_order)),
        steps=tuple(sorted(steps, key=order.__getitem__)),
    )


def block_output_source(
    workflow: ir.Workflow, block: ir.Step, value: str
) -> tuple[str, str] | None:
    """The member id and output name a block output reads, if it has the supported form."""
    match = _BLOCK_OUTPUT.fullmatch(value)
    if match is None:
        return None
    member, output = match.groups()
    if not any(step.id == member and step.parent == block.id for step in workflow.steps):
        return None
    return member, output


def condition_expression(condition: str) -> str:
    """The CEL text inside an `if` value that `check` accepted."""
    return find_expressions(condition)[0].inner


def _condition_errors(step: ir.Step) -> list[GlowError]:
    if step.condition is None:
        return []
    try:
        found = find_expressions(step.condition)
    except ExpressionSyntaxError:
        found = []
    if len(found) == 1:
        span = found[0]
        whole = step.condition.strip() == step.condition[span.start : span.end]
        if whole and "{{" not in span.inner:
            return []
    message = f"a compiled if must be one ${{{{ }}}} without '{{{{' inside; got {step.condition}"
    return [GlowError(Code.NOT_YET_SUPPORTED, f"{step.id}.if", message)]


def _shadowing_errors(workflow: ir.Workflow, step: ir.Step, needs: Environment) -> list[GlowError]:
    bound: dict[str, str] = {}
    errors = []
    variables = [
        *(
            (_loop_variable(workflow, binder), f"the loop variable of '{binder}'")
            for binder in needs.loops
        ),
        *((name, f"let '{name}' of '{binder}'") for binder, name in needs.lets),
    ]
    for name, meaning in variables:
        if name not in bound:
            bound[name] = meaning
            continue
        message = (
            f"step '{step.id}' needs both {bound[name]} and {meaning}, "
            "but a compiled step has one variable per name"
        )
        hint = "rename the inner loop variable or let"
        errors.append(GlowError(Code.NOT_YET_SUPPORTED, step.id, message, hint=hint))
    return errors


def _loop_variable(workflow: ir.Workflow, step_id: str) -> str:
    loop = workflow.step(step_id).loop
    assert loop is not None, "a loop reference names a for_each step"
    return loop.variable


def _depths(workflow: ir.Workflow) -> dict[str, int]:
    """How many blocks enclose each step. Blocks come before their members in the IR."""
    depth: dict[str, int] = {}
    for step in workflow.steps:
        depth[step.id] = 0 if step.parent is None else depth[step.parent] + 1
    return depth


def _field(edge: ir.Edge) -> str:
    return edge.target.split(".")[0].split("[")[0]


def _root(source: str) -> str:
    match = _IDENTIFIER.match(source)
    return match.group(0) if match else source
