"""Static types of whole CEL expressions over GLOW types.

`infer` walks the parsed tree. Paths get their type from a `lookup` callback,
so the validator keeps the rules for inputs, step outputs, files, groups and
loop variables in one place. On top of paths it types literals (a map
literal with constant keys is an object with those properties), indexing,
field selection, the `map`, `filter`, `all`, `exists` and `exists_one`
macros, operators, the standard functions that matter to glue code and the
GLOW functions.

cel-python has no type checker, and this one is deliberately partial:
anything it cannot type, such as an unknown function, a map with computed
keys or a file whose runtime shape is a string or a map, is `Unknown` with a
reason, and the validator turns the edge into a runtime check. An operation
that cannot succeed on the types it can see raises `TypeCheckError`.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import lark

from glow.expressions.cel import (
    arguments,
    chain,
    constant,
    is_macro,
    macro_variable,
    namespace_call,
    source_text,
    unwrap,
)
from glow.expressions.functions import SIGNATURES
from glow.types import (
    STRING,
    TIMESTAMP,
    Array,
    Bundle,
    File,
    GlowType,
    Group,
    Scalar,
    Unknown,
    member_type,
    render,
)

Lookup = Callable[[str, tuple[str | int | None, ...]], GlowType]

INTEGER: GlowType = Scalar({"type": "integer"})
NUMBER: GlowType = Scalar({"type": "number"})
BOOLEAN: GlowType = Scalar({"type": "boolean"})
NULL: GlowType = Scalar({"type": "null"})

_ORDERING = {"relation_lt": "<", "relation_le": "<=", "relation_gt": ">", "relation_ge": ">="}
_EQUALITY = {"relation_eq": "==", "relation_ne": "!="}
_ARITHMETIC = {
    "addition_add": "+",
    "addition_sub": "-",
    "multiplication_mul": "*",
    "multiplication_div": "/",
    "multiplication_mod": "%",
}
_NUMERIC = frozenset({"integer", "number"})
_CONVERSIONS: dict[str, GlowType] = {
    "int": INTEGER,
    "uint": INTEGER,
    "double": NUMBER,
    "string": STRING,
    "bool": BOOLEAN,
    "timestamp": TIMESTAMP,
}
_TIMESTAMP_METHODS = frozenset(
    {
        "getDate",
        "getDayOfMonth",
        "getDayOfWeek",
        "getDayOfYear",
        "getFullYear",
        "getHours",
        "getMilliseconds",
        "getMinutes",
        "getMonth",
        "getSeconds",
    }
)
_STRING_PREDICATES = frozenset({"startsWith", "endsWith", "contains", "matches"})
_JSON_TYPES = {
    "integer": INTEGER,
    "number": NUMBER,
    "string": STRING,
    "boolean": BOOLEAN,
    "null": NULL,
}


class TypeCheckError(ValueError):
    """An operation in the expression cannot succeed on its operands' types."""


def infer(tree: lark.Tree, expression: str, lookup: Lookup) -> GlowType:
    """The type of the whole expression. Raises `TypeCheckError`."""
    return _Checker(expression, lookup).infer(tree, {})


def kind(glow_type: GlowType) -> str | None:
    """The CEL-level kind of a type, or None when only the runtime knows it.

    Files, bundles and groups are None: a file is a path string in one
    place and a map in another.
    """
    if glow_type == TIMESTAMP:
        return "timestamp"
    if isinstance(glow_type, Array):
        return "array"
    if isinstance(glow_type, Scalar):
        declared = glow_type.schema.get("type")
        return declared if isinstance(declared, str) else None
    return None


def schema_of(glow_type: GlowType) -> dict[str, Any]:
    """A declaration that `parse_type` reads back as `glow_type`."""
    match glow_type:
        case Scalar(schema):
            return dict(schema)
        case Array(member):
            items = schema_of(member)
            return {"type": "array", "items": items} if items else {"type": "array"}
        case File() | Bundle() | Group():
            declaration: dict[str, Any] = {"type": glow_type.kind}
            if glow_type.media_types:
                declaration["media_type"] = [str(media) for media in glow_type.media_types]
            return declaration
    return {}


class _Checker:
    def __init__(self, expression: str, lookup: Lookup) -> None:
        self.expression = expression
        self.lookup = lookup

    def error(self, node: lark.Tree, message: str) -> TypeCheckError:
        part = source_text(node, self.expression)
        where = f" in '{part}'" if part != self.expression else ""
        return TypeCheckError(f"{message}{where}")

    def infer(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        found = chain(node)
        if found is not None:
            for index in found.dynamic:
                self.infer(index, scope)
            if found.root in scope:
                return member_type(scope[found.root], found.segments)
            return self.lookup(found.root, found.segments)
        node = unwrap(node)
        handler = getattr(self, f"_{node.data}", None)
        if handler is None:
            return Unknown(f"'{source_text(node, self.expression)}' is typed at runtime")
        return handler(node, scope)

    # Precedence rules with more than one child are operators.

    def _expr(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        condition, then, otherwise = (self.infer(child, scope) for child in node.children)
        self.expect_boolean(node.children[0], condition, "the condition of ?:")
        if then == otherwise:
            return then
        return Unknown("the branches of ?: have different types")

    def _conditionalor(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        return self.logical(node, scope, "||")

    def _conditionaland(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        return self.logical(node, scope, "&&")

    def logical(self, node: lark.Tree, scope: Mapping[str, GlowType], operator: str) -> GlowType:
        for child in node.children:
            self.expect_boolean(child, self.infer(child, scope), f"an operand of {operator}")
        return BOOLEAN

    def _relation(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        operation, right_node = node.children
        left = self.infer(operation.children[0], scope)
        right = self.infer(right_node, scope)
        if operation.data == "relation_in":
            if kind(right) not in (None, "array", "object"):
                raise self.error(node, f"'in' needs a list or a map, not {render(right)}")
            return BOOLEAN
        if operation.data in _ORDERING:
            self.expect_ordered(node, _ORDERING[operation.data], left, right)
        else:
            self.expect_comparable(node, _EQUALITY[operation.data], left, right)
        return BOOLEAN

    def expect_ordered(
        self, node: lark.Tree, operator: str, left: GlowType, right: GlowType
    ) -> None:
        kinds = {kind(left), kind(right)}
        if None in kinds:
            return
        if kinds <= _NUMERIC or (len(kinds) == 1 and kinds & {"string", "timestamp", "boolean"}):
            return
        raise self.error(node, self.operands(operator, left, right))

    def expect_comparable(
        self, node: lark.Tree, operator: str, left: GlowType, right: GlowType
    ) -> None:
        kinds = {kind(left), kind(right)}
        if None in kinds or "null" in kinds or len(kinds) == 1 or kinds <= _NUMERIC:
            return
        raise self.error(node, self.operands(operator, left, right))

    def operands(self, operator: str, left: GlowType, right: GlowType) -> str:
        return f"operator '{operator}' does not apply to {render(left)} and {render(right)}"

    def _addition(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        return self.arithmetic(node, scope)

    def _multiplication(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        return self.arithmetic(node, scope)

    def arithmetic(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        operation, right_node = node.children
        operator = _ARITHMETIC[operation.data]
        left = self.infer(operation.children[0], scope)
        right = self.infer(right_node, scope)
        kinds = (kind(left), kind(right))
        if None in kinds:
            return Unknown(f"an operand of '{operator}' is typed at runtime")
        if set(kinds) <= _NUMERIC:
            return INTEGER if kinds == ("integer", "integer") else NUMBER
        if operator == "+" and kinds == ("string", "string"):
            return STRING
        if operator == "+" and kinds == ("array", "array"):
            assert isinstance(left, Array) and isinstance(right, Array)
            return left if left == right else Array(Unknown("the lists have different types"))
        if operator == "-" and kinds == ("timestamp", "timestamp"):
            return Unknown("durations are typed at runtime")
        raise self.error(node, self.operands(operator, left, right))

    def _unary(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        operation, operand_node = node.children
        operand = self.infer(operand_node, scope)
        if operation.data == "unary_not":
            self.expect_boolean(operand_node, operand, "the operand of !")
            return BOOLEAN
        if kind(operand) not in (None, *_NUMERIC):
            raise self.error(node, f"operator '-' does not apply to {render(operand)}")
        return operand

    def expect_boolean(self, node: lark.Tree, glow_type: GlowType, what: str) -> None:
        if kind(glow_type) not in (None, "boolean"):
            raise self.error(node, f"{what} must be a boolean, not {render(glow_type)}")

    # Literals.

    def _literal(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        token = node.children[0]
        assert isinstance(token, lark.Token)
        match token.type:
            case "INT_LIT" | "UINT_LIT":
                return INTEGER
            case "FLOAT_LIT":
                return NUMBER
            case "STRING_LIT" | "MLSTRING_LIT":
                return STRING
            case "BOOL_LIT":
                return BOOLEAN
            case "NULL_LIT":
                return NULL
        return Unknown("bytes are typed at runtime")

    def _list_lit(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        members = [self.infer(child, scope) for child in arguments(node)]
        if not members:
            return Array(Unknown("the list is empty"))
        if all(member == members[0] for member in members):
            return Array(members[0])
        return Array(Unknown("the list members have different types"))

    def _map_lit(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        entries = node.children[0].children if node.children else []
        keys = entries[0::2]
        values = [self.infer(value, scope) for value in entries[1::2]]
        names = [constant(key) for key in keys]
        for key in keys:
            self.infer(key, scope)
        if not all(isinstance(name, str) for name in names):
            return Unknown("a map literal with computed or non-string keys")
        properties = {
            str(name): schema_of(value) for name, value in zip(names, values, strict=True)
        }
        return Scalar({"type": "object", "properties": properties, "required": list(properties)})

    # Selection on something that is not a plain path, such as a call result.

    def _member_dot(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        return member_type(self.infer(node.children[0], scope), [str(node.children[1])])

    def _member_index(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        base = self.infer(node.children[0], scope)
        self.infer(node.children[1], scope)
        return member_type(base, [constant(node.children[1])])

    # Calls.

    def _ident_arg(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        name = str(node.children[0])
        args = [self.infer(arg, scope) for arg in arguments(node)]
        if name == "date":
            self.expect_arity(node, "date", args, 2)
            self.expect_strings(node, "date", args)
            return TIMESTAMP
        if name == "size":
            self.expect_sized(node, args)
            return INTEGER
        if name == "has":
            return BOOLEAN
        if name in _CONVERSIONS:
            return _CONVERSIONS[name]
        return Unknown(f"function '{name}' is typed at runtime")

    def _member_dot_arg(self, node: lark.Tree, scope: Mapping[str, GlowType]) -> GlowType:
        method = str(node.children[1])
        namespace = namespace_call(node)
        if namespace is not None:
            return self.namespaced(node, namespace.name, method, scope)
        if is_macro(node):
            return self.macro(node, method, scope)
        receiver = self.infer(node.children[0], scope)
        args = [self.infer(arg, scope) for arg in arguments(node)]
        if method == "size":
            self.expect_sized(node, [receiver, *args])
            return INTEGER
        if method in _STRING_PREDICATES:
            self.expect_arity(node, method, [receiver, *args], 2)
            self.expect_strings(node, method, [receiver, *args])
            return BOOLEAN
        if method in _TIMESTAMP_METHODS and kind(receiver) in (None, "timestamp"):
            return INTEGER
        return Unknown(f"method '{method}' is typed at runtime")

    def namespaced(
        self, node: lark.Tree, namespace: str, method: str, scope: Mapping[str, GlowType]
    ) -> GlowType:
        name = f"{namespace}.{method}"
        signature = SIGNATURES.get((namespace, method))
        if signature is None:
            raise self.error(node, f"there is no function {name}")
        count, result = signature
        args = [self.infer(arg, scope) for arg in arguments(node)]
        self.expect_arity(node, name, args, count)
        if name == "media.accepts":
            if kind(args[0]) not in (None, "string", "array"):
                raise self.error(node, f"{name} needs a string or a list, not {render(args[0])}")
            accepted = args[0].member if isinstance(args[0], Array) else args[0]
            self.expect_strings(node, name, [accepted, *args[1:]])
        else:
            self.expect_strings(node, name, args)
        return _JSON_TYPES[result]

    def macro(self, node: lark.Tree, method: str, scope: Mapping[str, GlowType]) -> GlowType:
        variable = macro_variable(node)
        args = arguments(node)
        if variable is None or len(args) not in (2, 3):
            raise self.error(node, f"{method} takes a variable name and an expression")
        receiver = self.infer(node.children[0], scope)
        if kind(receiver) not in (None, "array", "object"):
            raise self.error(node, f"{method} needs a list or a map, not {render(receiver)}")
        member: GlowType
        if isinstance(receiver, Array):
            member = receiver.member
        elif kind(receiver) == "object":
            member = STRING
        else:
            member = Unknown("the members are typed at runtime")
        inner = {**scope, variable: member}
        if len(args) == 3 and method != "map":
            raise self.error(node, f"{method} takes a variable name and an expression")
        *conditions, body = args[1:]
        for condition in conditions:
            self.expect_boolean(condition, self.infer(condition, inner), f"the filter of {method}")
        result = self.infer(body, inner)
        if method == "map":
            return Array(result)
        self.expect_boolean(body, result, f"the predicate of {method}")
        return receiver if method == "filter" else BOOLEAN

    def expect_arity(
        self, node: lark.Tree, name: str, args: Sequence[GlowType], count: int
    ) -> None:
        if len(args) != count:
            raise self.error(node, f"{name} takes {count} arguments, not {len(args)}")

    def expect_strings(self, node: lark.Tree, name: str, args: Sequence[GlowType]) -> None:
        if any(kind(arg) not in (None, "string") for arg in args):
            kinds = ", ".join(render(arg) for arg in args)
            raise self.error(node, f"{name} takes strings, not ({kinds})")

    def expect_sized(self, node: lark.Tree, args: Sequence[GlowType]) -> None:
        self.expect_arity(node, "size", args, 1)
        if kind(args[0]) not in (None, "string", "array", "object"):
            raise self.error(node, f"size does not apply to {render(args[0])}")
