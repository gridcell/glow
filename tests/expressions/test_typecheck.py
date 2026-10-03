from typing import Any

import pytest

from glow.expressions import analyze
from glow.expressions.typecheck import TypeCheckError, infer, kind, schema_of
from glow.types import (
    TIMESTAMP,
    Array,
    File,
    GlowType,
    Group,
    Scalar,
    Unknown,
    member_type,
    parse_media_types,
    parse_type,
    render,
)

NETCDF = File(parse_media_types("application/x-netcdf"))


def obj(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


STEPS = obj(
    per_item=obj(
        outputs=obj(
            item={"type": "array", "items": {"type": "file", "media_type": "application/geo+json"}}
        )
    ),
    items=obj(outputs=obj(groups={"type": "array", "items": {"type": "group"}})),
    search=obj(
        outputs=obj(
            items={
                "type": "array",
                "items": obj(
                    id={"type": "string"}, assets=obj(visual=obj(href={"type": "string"}))
                ),
            }
        )
    ),
    count=obj(outputs=obj(total={"type": "integer"})),
)

# The types the validator would give each root, written as declarations.
ROOTS: dict[str, GlowType] = {
    "g": Group(parse_media_types("application/x-netcdf")),
    "steps": Scalar(STEPS),
    "inputs": Scalar(obj(dest={"type": "string"}, flag={"type": "boolean"})),
}


def lookup(root: str, path: tuple[str | int | None, ...]) -> GlowType:
    if root not in ROOTS:
        return Unknown("the reference did not resolve")
    return member_type(ROOTS[root], path)


def typed(expression: str) -> GlowType:
    analysis = analyze(expression)
    assert analysis.tree is not None
    return infer(analysis.tree, analysis.expression, lookup)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # The SST expressions.
        ("g.files[0].path", "file[application/x-netcdf]"),
        ("steps.per_item.outputs.item", "array<file[application/geo+json]>"),
        ("date(g.key, '%Y%m%d')", "timestamp"),
        ("'sst-' + g.key", "string"),
        # The plan's map example.
        ("steps.search.outputs.items.map(i, i.assets.visual.href)", "array<string>"),
        ("steps.search.outputs.items.filter(i, i.id != '')", "array<object>"),
        ("steps.search.outputs.items.exists(i, i.id.startsWith('S2'))", "boolean"),
        ("steps.search.outputs.items.all(i, has(i.id))", "boolean"),
        ("steps.search.outputs.items.exists_one(i, i.id == 'a')", "boolean"),
        ("g.captures.map(k, k + '=')", "array<string>"),
        ("steps.items.outputs.groups.map(x, x.files[0])", "array<file>"),
        (
            "steps.items.outputs.groups.map(x, x.files.filter(f, true).map(f, f.uri))",
            "array<array<file>>",
        ),
        ("[1, 2].map(x, x > 1, x * 2)", "array<integer>"),
        # Operators.
        ("steps.count.outputs.total > 0 && inputs.flag", "boolean"),
        ("!inputs.flag || 1 in [1]", "boolean"),
        ("g.key in g.captures", "boolean"),
        ("steps.count.outputs.total + 1", "integer"),
        ("steps.count.outputs.total / 2.0", "number"),
        ("-steps.count.outputs.total", "integer"),
        ("[1] + [2]", "array<integer>"),
        ("inputs.flag ? 'a' : 'b'", "string"),
        ("null == g.key", "boolean"),
        ("1 < 2.5", "boolean"),
        ("date(g.key, '%Y') < date(g.key, '%Y')", "boolean"),
        # Functions and methods.
        ("size(g.files) + g.key.size()", "integer"),
        ("int('1') + uint(1)", "integer"),
        ("string(1) + path.basename(g.files[0].uri)", "string"),
        ("media.matches(g.files[0].media_type, 'image/png')", "boolean"),
        ("media.accepts(['image/png'], g.files[0].media_type)", "boolean"),
        ("media.param(g.files[0].media_type, 'profile')", "string"),
        ("timestamp('2024-01-01T00:00:00Z').getFullYear()", "integer"),
        ("double(1)", "number"),
        ("bool('true')", "boolean"),
        ("(g.files)[0]", "file[application/x-netcdf]"),
        ("{'a': g.files}.a[0]", "file[application/x-netcdf]"),
        ("1.5", "number"),
    ],
)
def test_inferred_type(expression: str, expected: str) -> None:
    assert render(typed(expression)) == expected


def test_mixed_map_literal_is_an_object_with_its_keys() -> None:
    result = typed("{'id': 'sst-' + g.key, 'n': 1, 'src': g.files[0].path, 'tags': ['a']}")
    assert isinstance(result, Scalar)
    assert result.schema["required"] == ["id", "n", "src", "tags"]
    assert member_type(result, ["id"]) == Scalar({"type": "string"})
    assert member_type(result, ["n"]) == Scalar({"type": "integer"})
    assert member_type(result, ["src"]) == NETCDF
    assert member_type(result, ["tags"]) == Array(Scalar({"type": "string"}))


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("{g.key: 1}", "computed or non-string keys"),
        ("{1: 1}", "computed or non-string keys"),
        ("lookup(g.key)", "function 'lookup' is typed at runtime"),
        ("g.key.lowerAscii()", "method 'lowerAscii' is typed at runtime"),
        ("inputs.flag ? 1 : 'a'", "different types"),
        ("[1, 'a'][0]", "different types"),
        ("[][0]", "the list is empty"),
        ("g.files[0] + 'x'", "typed at runtime"),
        ("date(g.key, '%Y') - date(g.key, '%Y')", "durations"),
        ("nope.x", "did not resolve"),
        ("b'x'", "bytes"),
        ("dyn(1)", "typed at runtime"),
        ("x.y{a: 1}", "typed at runtime"),
    ],
)
def test_unknown_with_reason(expression: str, reason: str) -> None:
    result = typed(expression)
    assert isinstance(result, Unknown)
    assert reason in result.reason


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        (
            "steps.items.outputs.groups.size() > 'x'",
            "operator '>' does not apply to integer and string",
        ),
        ("1 == 'a'", "operator '==' does not apply to integer and string"),
        ("(1 + 'a') > 2", "operator '+' does not apply to integer and string in '1 + 'a''"),
        ("'a' - 'b'", "operator '-' does not apply to string and string"),
        ("-'x'", "operator '-' does not apply to string"),
        ("!g.key", "the operand of ! must be a boolean, not string"),
        ("g.key && true", "an operand of && must be a boolean, not string"),
        ("1 ? 2 : 3", "the condition of ?: must be a boolean"),
        ("'a' in 1", "'in' needs a list or a map, not integer"),
        ("size(1)", "size does not apply to integer"),
        ("size(g.key, 1)", "size takes 1 argument"),
        ("date(1, '%Y')", "date takes strings, not (integer, string)"),
        ("date(g.key)", "date takes 2 arguments, not 1"),
        ("g.key.startsWith(1)", "startsWith takes strings"),
        ("path.basename(steps.count.outputs.total)", "path.basename takes strings"),
        ("path.join(g.key)", "path.join takes 2 arguments"),
        ("media.nope(g.key)", "there is no function media.nope"),
        ("media.accepts(1, g.key)", "media.accepts needs a string or a list, not integer"),
        ("g.files.map(f)", "map takes a variable name and an expression"),
        ("g.files.map('f', 1)", "map takes a variable name and an expression"),
        ("g.files.filter(f, f, 1)", "filter takes a variable name and an expression"),
        ("[1].map(x, x, x)", "the filter of map must be a boolean"),
        ("g.key.map(x, x)", "map needs a list or a map, not string"),
        ("[1].all(x, x)", "the predicate of all must be a boolean, not integer"),
    ],
)
def test_type_error(expression: str, message: str) -> None:
    with pytest.raises(TypeCheckError) as caught:
        typed(expression)
    assert message in str(caught.value)


def test_kind_leaves_data_kinds_to_runtime() -> None:
    assert kind(NETCDF) is None
    assert kind(Unknown("x")) is None
    assert kind(TIMESTAMP) == "timestamp"
    assert kind(Scalar({"type": ["string", "null"]})) is None


@pytest.mark.parametrize(
    "glow_type",
    [
        NETCDF,
        File(),
        Array(Scalar({"type": "integer"})),
        Array(Group(parse_media_types("image/png"))),
        Scalar({"type": "object", "properties": {"a": {"type": "string"}}}),
    ],
)
def test_schema_of_round_trips(glow_type: GlowType) -> None:
    assert parse_type(schema_of(glow_type)) == glow_type


def test_schema_of_unknown_is_untyped() -> None:
    assert schema_of(Unknown("x")) == {}
    assert schema_of(Array(Unknown("x"))) == {"type": "array"}
