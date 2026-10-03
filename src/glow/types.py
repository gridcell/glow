"""Edge types and media-type matching (plan section 5.3).

Scalars and JSON objects are JSON Schema. Data on disk is one of three kinds:
`file`, `bundle` or `group`, each carrying the media types it declares. An
output declares the single most specific media type it produces; an input
declares the set it accepts. They match when the base type and every
parameter named by the accepted type agree, so `image/tiff; application=geotiff`
accepts `image/tiff; application=geotiff; profile=cloud-optimized`. There is
no type tree; `*` accepts anything.

`is_assignable` decides one edge as `ok`, `runtime_check` or `mismatch`. The
cases section 5.5 says downgrade to runtime (a group file without a media
type, an output whose media type comes from an input given as an expression,
and shapes that cannot be known statically) return `runtime_check`.

This module is pure: it does not read files and does not report errors to the
user. The validator turns a `Compatibility` into a message.
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from glow.expressions.syntax import OPEN as EXPRESSION_OPEN
from glow.models import ToolInput, ToolOutput

MAX_MEDIA_TYPE_LENGTH = 255

# RFC 6838 restricted-name for type, subtype and parameter names.
_NAME = r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
_TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_BASE_RE = re.compile(rf"\s*({_NAME})/({_NAME})\s*")
_PARAM_RE = re.compile(rf";\s*({_NAME})\s*=\s*({_TOKEN}|{_QUOTED})\s*")
_TOKEN_RE = re.compile(_TOKEN)
_WILDCARDS = frozenset({"*", "*/*"})


class MediaTypeError(ValueError):
    """Raised when a string is not a valid media type."""


@dataclass(frozen=True, slots=True)
class MediaType:
    """A parsed media type such as `image/tiff; application=geotiff`.

    Type, subtype, parameter names and parameter values are lowercase and the
    parameters are sorted by name, so equal media types compare equal and
    `str()` gives one canonical form. The wildcard `*` has type and subtype
    `*` and no parameters.
    """

    type: str
    subtype: str
    params: tuple[tuple[str, str], ...] = ()

    @property
    def is_wildcard(self) -> bool:
        return self.type == "*"

    @classmethod
    def parse(cls, text: str) -> "MediaType":
        if len(text) > MAX_MEDIA_TYPE_LENGTH:
            raise MediaTypeError(f"media type is longer than {MAX_MEDIA_TYPE_LENGTH} characters")
        if text.strip() in _WILDCARDS:
            return WILDCARD
        base = _BASE_RE.match(text)
        if base is None:
            # Partial wildcards such as `image/*` land here too: there is no type tree.
            raise MediaTypeError(f"'{text}' is not a media type of the form type/subtype")
        params: dict[str, str] = {}
        position = base.end()
        while position < len(text):
            param = _PARAM_RE.match(text, position)
            if param is None:
                raise MediaTypeError(f"'{text}' has a malformed parameter at offset {position}")
            name = param.group(1).lower()
            if name in params:
                raise MediaTypeError(f"'{text}' repeats parameter '{name}'")
            params[name] = _unquote(param.group(2)).lower()
            position = param.end()
        return cls(base.group(1).lower(), base.group(2).lower(), tuple(sorted(params.items())))

    def __str__(self) -> str:
        if self.is_wildcard:
            return "*"
        params = "".join(f"; {name}={_quote(value)}" for name, value in self.params)
        return f"{self.type}/{self.subtype}{params}"


WILDCARD = MediaType("*", "*")


def _unquote(value: str) -> str:
    if not value.startswith('"'):
        return value
    return re.sub(r"\\(.)", r"\1", value[1:-1])


def _quote(value: str) -> str:
    if _TOKEN_RE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _as_media_type(value: "MediaType | str") -> MediaType:
    return value if isinstance(value, MediaType) else MediaType.parse(value)


def parse_media_types(value: str | Sequence[str] | None) -> tuple[MediaType, ...]:
    """Parse the `media_type` of a declaration: one string, a list, or none."""
    if value is None:
        return ()
    texts = [value] if isinstance(value, str) else value
    return tuple(MediaType.parse(text) for text in texts)


def accepts(accepted: MediaType | str, produced: MediaType | str) -> bool:
    """True when `produced` satisfies the accepted media type.

    The base types must be equal and every parameter of `accepted` must be
    present in `produced` with the same value. Extra parameters on `produced`
    only make it more specific.
    """
    accepted = _as_media_type(accepted)
    produced = _as_media_type(produced)
    if accepted.is_wildcard:
        return True
    if (accepted.type, accepted.subtype) != (produced.type, produced.subtype):
        return False
    return set(accepted.params) <= set(produced.params)


@dataclass(frozen=True, slots=True)
class Scalar:
    """A JSON Schema value: string, number, integer, boolean or object."""

    schema: Mapping[str, Any]

    def __hash__(self) -> int:
        return hash(json.dumps(self.schema, sort_keys=True, default=str))


@dataclass(frozen=True, slots=True)
class _DataKind:
    """Shared shape of the on-disk kinds.

    An empty `media_types` means undeclared. `unknown_reason` explains why the
    media type can only be known at runtime, such as an output whose media
    type follows an input given as an expression.
    """

    kind: ClassVar[str]

    media_types: tuple[MediaType, ...] = ()
    unknown_reason: str | None = None


@dataclass(frozen=True, slots=True)
class File(_DataKind):
    kind: ClassVar[str] = "file"


@dataclass(frozen=True, slots=True)
class Bundle(_DataKind):
    kind: ClassVar[str] = "bundle"


@dataclass(frozen=True, slots=True)
class Group(_DataKind):
    """Files staged together by `fs.group`, with the regex capture names."""

    kind: ClassVar[str] = "group"

    captures: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Array:
    member: "GlowType"


@dataclass(frozen=True, slots=True)
class Unknown:
    """A type that cannot be determined statically."""

    reason: str


GlowType = Scalar | File | Bundle | Group | Array | Unknown

Status = Literal["ok", "runtime_check", "mismatch"]


@dataclass(frozen=True, slots=True)
class Compatibility:
    """The verdict for one edge.

    For a mismatch, `reason` starts with the `expects:` and `got:` lines of
    Appendix B and `hint` holds the usual fix when there is one.
    """

    status: Status
    reason: str | None = None
    hint: str | None = None

    @classmethod
    def ok(cls) -> "Compatibility":
        return cls("ok")

    @classmethod
    def runtime_check(cls, reason: str) -> "Compatibility":
        return cls("runtime_check", reason)

    @classmethod
    def mismatch(cls, reason: str | None = None, hint: str | None = None) -> "Compatibility":
        return cls("mismatch", reason, hint)


_SEVERITY: dict[Status, int] = {"ok": 0, "runtime_check": 1, "mismatch": 2}


def _worst(results: Iterable[Compatibility]) -> Compatibility:
    return max(results, key=lambda result: _SEVERITY[result.status], default=Compatibility.ok())


def render(glow_type: GlowType) -> str:
    """Render a type as it appears in validation messages, such as `array<stac-item>`."""
    match glow_type:
        case Array(member):
            return f"array<{render(member)}>"
        case Unknown():
            return "unknown"
        case Scalar(schema):
            return str(schema.get("title") or schema.get("type") or "any")
        case _:
            if not glow_type.media_types:
                return glow_type.kind
            return f"{glow_type.kind}[{' | '.join(map(str, glow_type.media_types))}]"


def is_assignable(produced: GlowType, accepted: GlowType) -> Compatibility:
    """Decide whether a value of type `produced` may feed an input of type `accepted`."""
    result = _check(produced, accepted)
    if result.status != "mismatch":
        return result
    header = f"expects: {render(accepted)}\ngot:     {render(produced)}"
    reason = f"{header}\n{result.reason}" if result.reason else header
    return Compatibility.mismatch(reason, result.hint)


# The `_check` family returns mismatches with only the detail line, if any;
# `is_assignable` adds the `expects:` / `got:` header once for the whole edge.


def _check(produced: GlowType, accepted: GlowType) -> Compatibility:
    if isinstance(produced, Unknown):
        return Compatibility.runtime_check(produced.reason)
    if isinstance(accepted, Unknown):
        return Compatibility.runtime_check(accepted.reason)
    if isinstance(accepted, Array):
        if isinstance(produced, Array):
            return _check(produced.member, accepted.member)
        return Compatibility.mismatch()
    if isinstance(produced, Array):
        return Compatibility.mismatch(hint="for_each over the array")
    if isinstance(produced, Scalar):
        if isinstance(accepted, Scalar):
            return _check_scalar(produced.schema, accepted.schema)
        return _check_scalar_as_data(produced.schema)
    if isinstance(accepted, Scalar):
        return Compatibility.mismatch()
    return _check_data(produced, accepted)


def _check_scalar_as_data(produced: Mapping[str, Any]) -> Compatibility:
    # A string is a URI that glow-exec stages as a file; its media type is
    # checked by extension or header once the file is there.
    if produced.get("type") == "string":
        return Compatibility.runtime_check("a string is staged as a file and checked at runtime")
    return Compatibility.mismatch()


def _kinds_compatible(produced: _DataKind, accepted: _DataKind) -> bool:
    # A file listed by fs.group may feed a file input.
    return produced.kind == accepted.kind or (produced.kind, accepted.kind) == ("group", "file")


def _check_data(produced: _DataKind, accepted: _DataKind) -> Compatibility:
    if not _kinds_compatible(produced, accepted):
        return Compatibility.mismatch()
    if not accepted.media_types:
        return Compatibility.ok()
    if produced.unknown_reason is not None:
        return Compatibility.runtime_check(produced.unknown_reason)
    if not produced.media_types:
        return Compatibility.runtime_check(f"the {produced.kind} has no declared media type")
    matched = [
        any(accepts(wanted, media_type) for wanted in accepted.media_types)
        for media_type in produced.media_types
    ]
    if all(matched):
        return Compatibility.ok()
    if any(matched):
        return Compatibility.runtime_check("only some of the produced media types are accepted")
    return Compatibility.mismatch()


_COMBINATORS = ("anyOf", "oneOf", "allOf")


def _check_scalar(produced: Mapping[str, Any], accepted: Mapping[str, Any]) -> Compatibility:
    if any(key in schema for schema in (produced, accepted) for key in _COMBINATORS):
        return Compatibility.runtime_check("anyOf, oneOf and allOf are checked at runtime")
    accepted_type = accepted.get("type")
    produced_type = produced.get("type")
    if accepted_type is not None:
        if produced_type is None:
            return Compatibility.runtime_check("the produced value has no declared type")
        if produced_type != accepted_type and (produced_type, accepted_type) != (
            "integer",
            "number",
        ):
            return Compatibility.mismatch()
    result = _check_enum(produced, accepted)
    if produced_type == "object" and result.status != "mismatch":
        result = _worst([result, _check_object(produced, accepted)])
    return result


def _check_enum(produced: Mapping[str, Any], accepted: Mapping[str, Any]) -> Compatibility:
    if "enum" not in accepted:
        return Compatibility.ok()
    if "enum" not in produced:
        return Compatibility.runtime_check("the value is checked against the accepted enum")
    extra = [value for value in produced["enum"] if value not in accepted["enum"]]
    if extra:
        return Compatibility.mismatch(f"values not accepted: {', '.join(map(repr, extra))}")
    return Compatibility.ok()


def _check_object(produced: Mapping[str, Any], accepted: Mapping[str, Any]) -> Compatibility:
    produced_props: Mapping[str, Any] | None = produced.get("properties")
    accepted_props: Mapping[str, Any] = accepted.get("properties") or {}
    if produced_props is None:
        if accepted_props or accepted.get("required"):
            return Compatibility.runtime_check("the object's properties are checked at runtime")
        return Compatibility.ok()
    missing = [name for name in accepted.get("required", []) if name not in produced_props]
    if missing:
        return Compatibility.mismatch(f"missing required property '{missing[0]}'")
    results = []
    for name in accepted_props.keys() & produced_props.keys():
        result = _check(parse_type(produced_props[name]), parse_type(accepted_props[name]))
        if result.status == "mismatch":
            detail = f": {result.reason}" if result.reason else ""
            return Compatibility.mismatch(f"property '{name}'{detail}")
        results.append(result)
    if any(
        schema.get("additionalProperties", False) is not False for schema in (produced, accepted)
    ):
        results.append(Compatibility.runtime_check("additionalProperties are checked at runtime"))
    return _worst(results)


_KINDS: dict[str, type[_DataKind]] = {"file": File, "bundle": Bundle, "group": Group}


def _declaration(decl: Mapping[str, Any] | ToolInput | ToolOutput) -> Mapping[str, Any]:
    if isinstance(decl, ToolInput | ToolOutput):
        return decl.model_dump(by_alias=True, exclude_none=True)
    return decl


def parse_type(decl: Mapping[str, Any] | ToolInput | ToolOutput) -> GlowType:
    """The type of a tool input or output declaration.

    Raises `MediaTypeError` when a declared media type is malformed. A data
    kind output with `media_type_from` is unknown here; use
    `resolve_output_type` with the step's `with` values instead.
    """
    decl = _declaration(decl)
    kind = decl.get("type")
    if kind in _KINDS:
        reason = None
        if "media_type_from" in decl:
            reason = f"the media type follows input '{decl['media_type_from']}'"
        return _KINDS[kind](parse_media_types(decl.get("media_type")), reason)
    if kind == "array":
        items = decl.get("items")
        return Array(parse_type(items) if items else Unknown("array items are not declared"))
    return Scalar(dict(decl))


def resolve_output_type(
    output_decl: ToolOutput,
    with_values: Mapping[str, Any],
    input_defaults: Mapping[str, Any] | None = None,
) -> GlowType:
    """The type of an output once the step's `with` values are known.

    When `media_type_from` names an input given as a constant (or left to its
    default), the media type is `media_types[constant]`. An expression value
    is only known at runtime, so the result carries an `unknown_reason`.
    """
    source = output_decl.media_type_from
    if source is None:
        return parse_type(output_decl)
    kind = _KINDS[output_decl.type]
    defaults = input_defaults or {}
    if source in with_values:
        value = with_values[source]
    elif source in defaults:
        value = defaults[source]
    else:
        return kind(unknown_reason=f"input '{source}' is not set and has no default")
    if isinstance(value, str) and EXPRESSION_OPEN in value:
        return kind(
            unknown_reason=f"the media type follows input '{source}', given as an expression"
        )
    # Keys are matched as strings, as the manifest model does for enum values.
    media_type = (output_decl.media_types or {}).get(str(value))
    if media_type is None:
        return kind(unknown_reason=f"no media type is declared for {source}={value!r}")
    return kind(parse_media_types(media_type))


STRING: GlowType = Scalar({"type": "string"})
# CEL timestamps, such as the result of date(). JSON carries them as RFC 3339
# strings, so a timestamp feeds a string input.
TIMESTAMP: GlowType = Scalar({"type": "string", "format": "date-time", "title": "timestamp"})

# A path segment: a field name, a constant index, or None for an index known
# only at runtime. The same as `glow.expressions.Segment`, which imports this
# module.
_Segment = str | int | None


def parse_type_or_unknown(decl: Mapping[str, Any] | ToolInput) -> GlowType:
    """`parse_type`, with a malformed media type giving `Unknown` instead of an error."""
    if isinstance(decl, Mapping):
        decl = {key: value for key, value in decl.items() if value is not None}
    try:
        return parse_type(decl)
    except MediaTypeError as exc:
        return Unknown(str(exc))


def member_type(base: GlowType, path: Iterable[_Segment]) -> GlowType:
    """The type of `base` followed by field and index segments."""
    for part in path:
        base = _member(base, part)
    return base


def _member(base: GlowType, part: _Segment) -> GlowType:
    match base:
        case Unknown():
            return base
        case Array(member):
            return member if not isinstance(part, str) else Unknown(f"'{part}' of an array")
        case Group():
            return _group_member(base, part)
        case File() | Bundle():
            # A resolved file is a map whose `path` and `uri` are the file itself.
            if part in ("path", "uri"):
                return base
            if part == "media_type":
                return STRING
            return Unknown(f"'{part}' of a {base.kind} is typed at runtime")
    return _schema_member(base.schema, part)


def _group_member(group: Group, part: _Segment) -> GlowType:
    if part == "files":
        return Array(File(group.media_types, group.unknown_reason))
    if part == "key":
        return STRING
    if part == "captures":
        return Scalar({"type": "object", "additionalProperties": {"type": "string"}})
    return Unknown(f"'{part}' of a group is typed at runtime")


def _schema_member(schema: Mapping[str, Any], part: _Segment) -> GlowType:
    if not isinstance(part, str):
        return Unknown("an index into a value that is not an array")
    properties = schema.get("properties") or {}
    if part in properties:
        return parse_type_or_unknown(properties[part])
    extra = schema.get("additionalProperties")
    if isinstance(extra, dict):
        return parse_type_or_unknown(extra)
    return Unknown(f"'{part}' is not a declared property")
