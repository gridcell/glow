"""What a compiled step may reference, until scope conversion lands (issue 7).

An Argo template sees only its own inputs and the workflow parameters. The
compiler passes a block template its loop variable and run prefix, so a
member expression may use:

- `inputs.*`;
- `steps.<id>` of a sibling in the same block;
- the loop variable of its own for_each, and of the block it is in.

An outer step's output, any `let` name, a loop variable two levels up and
`steps.<id>.results` are rejected with `GLOW-E050`. A block's `if` may use
inputs and steps beside the block, whose outputs the compiler threads down
to the members. A block output must be one member output, written
`${{ steps.<id>.outputs.<name> }}`.
"""

import re

from glow import ir
from glow.expressions import ExpressionSyntaxError, find_expressions
from glow.validate.errors import Code, GlowError

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BLOCK_OUTPUT = re.compile(
    r"\s*\$\{\{\s*steps\.([A-Za-z_][A-Za-z0-9_]*)\.outputs\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}\s*"
)
_RESULTS = re.compile(r"steps\.[A-Za-z_][A-Za-z0-9_]*\.results\b")
# Fields evaluated outside the loop their step opens.
_OUTSIDE_LOOP = ("for_each", "if")

_HINT = (
    "the compiler cannot pass outer values into a block yet; read the value from a workflow "
    "input, the block's loop variable or a step inside the block"
)


def check(workflow: ir.Workflow) -> list[GlowError]:
    """Every reference the compiler cannot express yet."""
    blocks = {step.parent for step in workflow.steps if step.parent is not None}
    errors = []
    for edge in workflow.edges:
        problem = _edge_problem(workflow, blocks, edge)
        if problem is not None:
            location = f"{edge.target_step}.{edge.target}"
            message = f"{problem}, in {edge.expression}"
            errors.append(GlowError(Code.NOT_YET_SUPPORTED, location, message, hint=_HINT))
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
    return errors


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


def _edge_problem(workflow: ir.Workflow, blocks: set[str], edge: ir.Edge) -> str | None:
    target = workflow.step(edge.target_step)
    field = edge.target.split(".")[0].split("[")[0]
    if field == "let":
        # A let binding is only rejected where it is used.
        return None
    root = _root(edge.source)
    if root == "inputs":
        return None
    outside = field in _OUTSIDE_LOOP and target.loop is not None
    if root == "steps":
        return _step_problem(workflow, target, field, edge)
    return _name_problem(workflow, blocks, target, root, field, outside=outside)


def _root(source: str) -> str:
    match = _IDENTIFIER.match(source)
    return match.group(0) if match else source


def _step_problem(workflow: ir.Workflow, target: ir.Step, field: str, edge: ir.Edge) -> str | None:
    producer = edge.source_step
    if producer is None:
        return None
    if _RESULTS.match(edge.source):
        return f"steps.{producer}.results is not yet supported by the compiler"
    expected_parent = target.id if field == "outputs" else target.parent
    actual_parent = workflow.step(producer).parent
    if actual_parent == expected_parent:
        return None
    return (
        f"steps.{producer} is outside block '{target.parent}'; "
        "a compiled block can only use its own steps, its loop variable and workflow inputs"
    )


def _name_problem(
    workflow: ir.Workflow,
    blocks: set[str],
    target: ir.Step,
    name: str,
    field: str,
    *,
    outside: bool,
) -> str | None:
    owner = target.parent if outside else target.id
    while owner is not None:
        step = workflow.step(owner)
        if name in step.let:
            return f"let binding '{name}' of '{owner}' is not yet supported by the compiler"
        if step.loop is not None and step.loop.variable == name:
            break
        owner = step.parent
    if owner is None:
        return None
    if field == "if" and target.id in blocks:
        return (
            f"the if of block '{target.id}' uses loop variable '{name}'; "
            "a compiled block if can only use workflow inputs and steps beside the block"
        )
    if owner in (target.id, target.parent):
        return None
    return (
        f"loop variable '{name}' belongs to block '{owner}', which encloses '{target.parent}'; "
        "a compiled block only sees its own loop variable"
    )
