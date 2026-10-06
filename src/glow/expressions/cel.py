"""CEL parsing, reference collection, the cost limit and evaluation.

Expressions are parsed with cel-python. The validator only needs the paths
an expression refers to (`collect`); the type checker in `typecheck.py`
walks the same tree. `Evaluator` evaluates expressions against plain JSON
values the way glow-exec does (`glow-exec/internal/cel`), for the local
runner and the tests.

cel-python has no cost limit, so `parse` bounds the expression instead: its
length, its number of operations, its nesting and how deeply comprehension
macros such as `map` nest, since each level multiplies the work by the
length of a list.
"""

import datetime
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import celpy
import lark
from celpy import celtypes
from celpy.evaluation import CELEvalError, CELUnsupportedError, celstr

from glow.expressions.functions import FUNCTIONS, NAMESPACES, Namespace
from glow.expressions.syntax import ExpressionSyntaxError, find_expressions

# A path segment: a field name, a constant index, or None for an index that
# is only known at runtime, such as `xs[i]`.
Segment = str | int | None

MAX_EXPRESSION_LENGTH = 2000
MAX_OPERATIONS = 200
MAX_NESTING = 300
MAX_MACRO_NESTING = 2

# CEL macros whose first argument names a variable local to the call.
MACROS = frozenset({"all", "exists", "exists_one", "map", "filter"})

# Grammar rules that only encode precedence. With one child they are not an
# operation, so the walkers skip through them.
_WRAPPERS = frozenset(
    {
        "expr",
        "conditionalor",
        "conditionaland",
        "relation",
        "addition",
        "multiplication",
        "unary",
        "member",
        "primary",
        "paren_expr",
    }
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Parsing builds the lark grammar once; every call reuses it.
_ENVIRONMENT = celpy.Environment()


class ExpressionTooCostlyError(ValueError):
    """Raised when an expression exceeds the cost limit."""


class ExpressionEvalError(ValueError):
    """Raised when evaluating an expression fails."""


@dataclass(frozen=True, slots=True)
class Reference:
    """One path such as `steps.cog.outputs.result`.

    `root` is the first identifier (`inputs`, `steps`, or a loop or `let`
    name) and `path` the segments after it. `text` is the path as written.
    """

    root: str
    path: tuple[Segment, ...]
    text: str


def parse(expression: str) -> lark.Tree:
    """Parse the text inside one `${{ ... }}` span and apply the cost limit.

    Raises `ExpressionSyntaxError` or `ExpressionTooCostlyError`.
    """
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ExpressionTooCostlyError(
            f"the expression is {len(expression)} characters long; "
            f"the limit is {MAX_EXPRESSION_LENGTH}"
        )
    try:
        tree = _ENVIRONMENT.compile(expression)
    except celpy.CELParseError as exc:
        where = f" at column {exc.column}" if exc.column is not None else ""
        raise ExpressionSyntaxError(f"the expression is not valid CEL{where}") from None
    check_cost(tree)
    return tree


def check_cost(tree: lark.Tree) -> None:
    """Raise `ExpressionTooCostlyError` when the tree is over any limit."""
    operations = 0
    stack: list[tuple[lark.Tree, int, int]] = [(tree, 1, 0)]
    while stack:
        node, depth, macros = stack.pop()
        if depth > MAX_NESTING:
            raise ExpressionTooCostlyError(
                f"the expression nests more than {MAX_NESTING} levels deep"
            )
        if node.data not in _WRAPPERS or len(node.children) > 1:
            operations += 1
        inner = macros
        if is_macro(node):
            inner = macros + 1
            if inner > MAX_MACRO_NESTING:
                raise ExpressionTooCostlyError(
                    f"the expression nests {inner} macros such as map or filter; "
                    f"the limit is {MAX_MACRO_NESTING}"
                )
        for index, child in enumerate(node.children):
            if isinstance(child, lark.Tree):
                # A macro's receiver is evaluated once, outside the loop.
                stack.append((child, depth + 1, macros if index == 0 else inner))
    if operations > MAX_OPERATIONS:
        raise ExpressionTooCostlyError(
            f"the expression has {operations} operations; the limit is {MAX_OPERATIONS}"
        )


# Tree helpers shared with the type checker.


def unwrap(node: lark.Tree) -> lark.Tree:
    """Skip precedence-only rules down to the node that does the work."""
    while (
        node.data in _WRAPPERS
        and len(node.children) == 1
        and isinstance(node.children[0], lark.Tree)
    ):
        node = node.children[0]
    return node


def arguments(node: lark.Tree) -> list[lark.Tree]:
    """The argument expressions of a call or list literal; none when it is empty."""
    last = node.children[-1] if node.children else None
    if isinstance(last, lark.Tree) and last.data == "exprlist":
        return [child for child in last.children if isinstance(child, lark.Tree)]
    return []


def is_macro(node: lark.Tree) -> bool:
    return node.data == "member_dot_arg" and str(node.children[1]) in MACROS


def macro_variable(node: lark.Tree) -> str | None:
    """The variable a macro call binds, such as `i` in `xs.map(i, i.name)`."""
    args = arguments(node)
    if len(args) < 2:
        return None
    first = unwrap(args[0])
    if first.data != "ident":
        return None
    return str(first.children[0])


def namespace_call(node: lark.Tree) -> Namespace | None:
    """The namespace of a call such as `path.basename(x)`, or None."""
    if node.data != "member_dot_arg":
        return None
    receiver = unwrap(node.children[0])
    if receiver.data != "ident":
        return None
    return NAMESPACES.get(str(receiver.children[0]))


def constant(node: lark.Tree) -> Segment:
    """The value of an int or string literal index, or None for anything else."""
    node = unwrap(node)
    if node.data != "literal":
        return None
    token = node.children[0]
    if not isinstance(token, lark.Token):
        return None
    if token.type == "INT_LIT":
        return int(celtypes.IntType(token.value))
    if token.type in ("STRING_LIT", "MLSTRING_LIT"):
        return str(celstr(token))
    return None


@dataclass(frozen=True, slots=True)
class Chain:
    """A path from an identifier: `root`, then field and index segments.

    `dynamic` holds the index expressions that are not constants, whose own
    references are collected separately.
    """

    root: str
    segments: tuple[Segment, ...]
    dynamic: tuple[lark.Tree, ...]
    node: lark.Tree


def chain(node: lark.Tree) -> Chain | None:
    """Decompose `a.b[0]['c']` into a `Chain`, or None when `node` is not a path."""
    top = node = unwrap(node)
    segments: list[Segment] = []
    dynamic: list[lark.Tree] = []
    while True:
        if node.data == "member_dot":
            segments.append(str(node.children[1]))
        elif node.data == "member_index":
            index = node.children[1]
            value = constant(index)
            segments.append(value)
            if value is None:
                dynamic.append(index)
        elif node.data in ("ident", "dot_ident"):
            segments.reverse()
            dynamic.reverse()
            return Chain(str(node.children[0]), tuple(segments), tuple(dynamic), top)
        else:
            return None
        node = unwrap(node.children[0])


def source_text(node: lark.Tree, expression: str) -> str:
    meta = node.meta
    if meta.empty:
        return expression
    return expression[meta.start_pos : meta.end_pos]


def collect(tree: lark.Tree, expression: str) -> list[Reference]:
    """Every path in the expression rooted at a name the expression does not bind."""
    return list(_references(tree, expression, frozenset()))


def _references(node: lark.Tree, expression: str, bound: frozenset[str]) -> Iterator[Reference]:
    found = chain(node)
    if found is not None:
        if found.root not in bound:
            text = source_text(found.node, expression)
            yield Reference(found.root, found.segments, text)
        for index in found.dynamic:
            yield from _references(index, expression, bound)
        return
    node = unwrap(node)
    children = [child for child in node.children if isinstance(child, lark.Tree)]
    if namespace_call(node) is not None:
        children = arguments(node)
    elif is_macro(node) and (variable := macro_variable(node)) is not None:
        receiver, _, *body = children[:1] + arguments(node)
        yield from _references(receiver, expression, bound)
        for child in body:
            yield from _references(child, expression, bound | {variable})
        return
    for child in children:
        yield from _references(child, expression, bound)


# Evaluation.


class Evaluator:
    """Evaluates expressions against fixed top-level variables.

    Values must be JSON-like: dicts with string keys, lists, strings, bools,
    ints, floats and None. Results are converted back to the same shapes,
    with timestamps as RFC 3339 strings in UTC.
    """

    def __init__(self, variables: Mapping[str, Any]) -> None:
        activation: dict[str, Any] = {}
        for name, value in variables.items():
            if not _IDENTIFIER.fullmatch(name) or name in NAMESPACES:
                raise ValueError(f"{name!r} cannot be used as an expression variable")
            activation[name] = celpy.json_to_cel(value)
        activation.update(NAMESPACES)
        self._activation = activation

    def eval(self, expression: str) -> Any:
        tree = parse(expression)
        program = _ENVIRONMENT.program(tree, functions=FUNCTIONS)
        try:
            result = program.evaluate(self._activation)
        except (CELEvalError, CELUnsupportedError) as exc:
            raise ExpressionEvalError(_eval_message(exc)) from None
        except Exception as exc:
            # cel-python can fail inside its own error handling on some
            # ill-typed input; report that as a failed evaluation too.
            message = f"the expression cannot be evaluated ({type(exc).__name__})"
            raise ExpressionEvalError(message) from exc
        if isinstance(result, CELEvalError):
            raise ExpressionEvalError(_eval_message(result))
        return to_json(result)

    def template(self, value: str) -> Any:
        """Evaluate the `${{ }}` spans in one string.

        A string that is one span and nothing else keeps the expression's
        type; otherwise each result is rendered as text (strings as they are,
        anything else as JSON) and spliced in.
        """
        spans = find_expressions(value)
        if not spans:
            return value
        if len(spans) == 1 and spans[0].start == 0 and spans[0].end == len(value):
            return self._span(spans[0].inner)
        parts: list[str] = []
        last = 0
        for span in spans:
            parts.append(value[last : span.start])
            parts.append(_render(self._span(span.inner)))
            last = span.end
        parts.append(value[last:])
        return "".join(parts)

    def substitute(self, location: str, value: Any) -> Any:
        """Evaluate every expression in a JSON-like value. Keys are not evaluated."""
        if isinstance(value, str):
            try:
                return self.template(value)
            except (ExpressionEvalError, ExpressionSyntaxError, ExpressionTooCostlyError) as exc:
                raise type(exc)(f"{location}: {exc}") from None
        if isinstance(value, dict):
            # Sorted so that the first error reported is deterministic.
            return {key: self.substitute(f"{location}.{key}", value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [
                self.substitute(f"{location}[{index}]", item) for index, item in enumerate(value)
            ]
        return value

    def _span(self, inner: str) -> Any:
        try:
            return self.eval(inner)
        except ExpressionEvalError as exc:
            raise ExpressionEvalError(f"evaluating {json.dumps(inner)}: {exc}") from None


def _eval_message(error: Exception) -> str:
    # celpy appends the whole activation to some messages; keep the first part.
    message = str(error.args[0]) if error.args else type(error).__name__
    message = message.split(" (in activation", 1)[0]
    if message.startswith("no such member"):
        key = error.args[2] if len(error.args) > 2 else None
        detail = f": {key[0]!r}" if isinstance(key, tuple) and key else ""
        return f"no such key{detail}"
    return message


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def to_json(value: Any) -> Any:
    """Convert a CEL value to JSON-like Python data."""
    match value:
        case None | celtypes.NullType():
            return None
        case celtypes.BoolType() | bool():
            return bool(value)
        case celtypes.DoubleType() | float():
            if math.isnan(value) or math.isinf(value):
                raise ExpressionEvalError(f"result {value} is not a JSON number")
            return float(value)
        case int():
            return int(value)
        case str():
            return str(value)
        case datetime.datetime():
            return format_timestamp(value)
        case datetime.timedelta():
            return _format_duration(value)
        case dict():
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ExpressionEvalError(f"map key {key} is not a string")
                result[str(key)] = to_json(item)
            return result
        case list():
            return [to_json(item) for item in value]
    raise ExpressionEvalError(f"a result of type {type(value).__name__} is not JSON")


def format_timestamp(value: datetime.datetime) -> str:
    """RFC 3339 in UTC with a `Z`, and fractional seconds only when present."""
    utc = value.astimezone(datetime.UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}".rstrip("0")
    return f"{text}Z"


def _format_duration(value: datetime.timedelta) -> str:
    seconds = value.total_seconds()
    return f"{int(seconds)}s" if seconds.is_integer() else f"{seconds}s"
