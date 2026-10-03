"""The GLOW functions available in expressions (plan section 5.4).

    date(string, format) timestamp     parse with a strftime subset
    path.basename(string) string       last path segment of a path or URI
    path.dirname(string) string        everything before the last segment
    path.stem(string) string           basename without its extension
    path.ext(string) string            extension with its dot, or ""
    path.join(string, string) string   join with exactly one "/"
    media.matches(actual, declared) bool
    media.accepts(accepted, produced) bool
    media.base(string) string          type/subtype without parameters
    media.param(string, name) string   one parameter, or ""
    media.ext(string) string           preferred extension, without a dot

glow-exec (`glow-exec/internal/cel/functions.go`) implements `date`,
`path.basename|stem|ext|join`, `media.matches` and `media.ext` with the same
results and error texts; the shared fixture in `tests/fixtures/expressions`
checks both.

cel-python parses `path.basename(x)` as the method `basename` called on the
identifier `path`. So `path` and `media` are bound to namespace sentinels and
each function checks its receiver; any other receiver is "no such overload".
"""

import datetime
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from celpy import celtypes
from celpy.evaluation import CELEvalError, base_functions

# A module import, not names: glow.types imports glow.expressions, so it may
# still be initializing when this module loads.
from glow import types as glow_types


@dataclass(frozen=True, slots=True)
class Namespace:
    """The value bound to `path` or `media` so that `path.basename(x)` resolves."""

    name: str


PATH = Namespace("path")
MEDIA = Namespace("media")
NAMESPACES: dict[str, Namespace] = {PATH.name: PATH, MEDIA.name: MEDIA}

# Base media type to extensions, the first being preferred. Kept in step with
# glow-exec/internal/mediatype/mediatype.go.
EXTENSIONS: dict[str, tuple[str, ...]] = {
    "image/tiff": ("tif", "tiff"),
    "image/png": ("png",),
    "image/jpeg": ("jpg", "jpeg"),
    "image/jp2": ("jp2",),
    "image/webp": ("webp",),
    "application/x-netcdf": ("nc",),
    "application/x-hdf5": ("h5", "hdf5"),
    "application/vnd.gdal.vrt+xml": ("vrt",),
    "application/json": ("json",),
    "application/geo+json": ("geojson",),
    "application/vnd.apache.parquet": ("parquet",),
    "application/xml": ("xml",),
    "application/zip": ("zip",),
    "text/plain": ("txt",),
    "text/csv": ("csv",),
}

# Result types of the namespaced functions, as JSON Schema type names, and
# their argument count. The type checker reads this table.
SIGNATURES: dict[tuple[str, str], tuple[int, str]] = {
    ("path", "basename"): (1, "string"),
    ("path", "dirname"): (1, "string"),
    ("path", "stem"): (1, "string"),
    ("path", "ext"): (1, "string"),
    ("path", "join"): (2, "string"),
    ("media", "matches"): (2, "boolean"),
    ("media", "accepts"): (2, "boolean"),
    ("media", "base"): (1, "string"),
    ("media", "param"): (2, "string"),
    ("media", "ext"): (1, "string"),
}

Result = Any


def _error(message: str) -> CELEvalError:
    return CELEvalError(message, ValueError, None)


def _no_overload(name: str, args: Sequence[Any]) -> CELEvalError:
    kinds = ", ".join(type(arg).__name__.removesuffix("Type").lower() for arg in args)
    return CELEvalError(f"no such overload: {name}({kinds})", TypeError, None)


def _quoted(text: str) -> str:
    return json.dumps(text)


# Path helpers. A trailing "/" is ignored, as for a URI prefix such as
# s3://bucket/raw/.


def _last_segment(text: str) -> str:
    trimmed = text.rstrip("/")
    return trimmed[trimmed.rfind("/") + 1 :]


def _split_ext(name: str) -> tuple[str, str]:
    # Like os.path.splitext: leading dots do not start an extension.
    dot = name.rfind(".")
    if dot <= 0 or not name[:dot].strip("."):
        return name, ""
    return name[:dot], name[dot:]


def basename(text: str) -> str:
    return _last_segment(text)


def dirname(text: str) -> str:
    trimmed = text.rstrip("/")
    return trimmed[: trimmed.rfind("/")] if "/" in trimmed else ""


def stem(text: str) -> str:
    return _split_ext(_last_segment(text))[0]


def ext(text: str) -> str:
    return _split_ext(_last_segment(text))[1]


def join(base: str, child: str) -> str:
    if not base:
        return child
    return base.rstrip("/") + "/" + child.lstrip("/")


# Media-type helpers, on top of glow.types.


def _media_type(text: str) -> "glow_types.MediaType | CELEvalError":
    try:
        return glow_types.MediaType.parse(text)
    except glow_types.MediaTypeError as exc:
        return _error(f"media: {exc}")


def media_matches(actual: str, declared: str) -> bool | CELEvalError:
    return media_accepts(declared, actual)


def media_accepts(accepted: str | Sequence[str], produced: str) -> bool | CELEvalError:
    """True when `produced` satisfies `accepted`, one media type or a list of them."""
    wanted = [accepted] if isinstance(accepted, str) else list(accepted)
    if not all(isinstance(text, str) for text in wanted):
        return _no_overload("media.accepts", [accepted, produced])
    parsed = [_media_type(text) for text in (*wanted, produced)]
    for item in parsed:
        if isinstance(item, CELEvalError):
            return item
    *accepted_types, produced_type = parsed
    return any(glow_types.accepts(item, produced_type) for item in accepted_types)


def media_base(text: str) -> str | CELEvalError:
    parsed = _media_type(text)
    if isinstance(parsed, CELEvalError):
        return parsed
    return "*" if parsed.is_wildcard else f"{parsed.type}/{parsed.subtype}"


def media_param(text: str, name: str) -> str | CELEvalError:
    parsed = _media_type(text)
    if isinstance(parsed, CELEvalError):
        return parsed
    return dict(parsed.params).get(name.lower(), "")


def media_ext(text: str) -> str | CELEvalError:
    base = media_base(text)
    extensions = EXTENSIONS.get(base) if isinstance(base, str) else None
    if not extensions:
        return _error(f"media.ext: no extension known for {_quoted(text)}")
    return extensions[0]


# date(): a strftime subset that glow-exec implements too. Each directive
# reads one digit up to its width, like Python's strptime.

_DATE_WIDTHS = {"Y": 4, "m": 2, "d": 2, "H": 2, "M": 2, "S": 2, "j": 3}


class _DateError(ValueError):
    pass


def parse_date(value: str, format_text: str) -> datetime.datetime:
    """Parse `value` with a format limited to %Y %m %d %H %M %S %j and %%. Returns UTC."""
    parts = {"Y": 1900, "m": 1, "d": 1, "H": 0, "M": 0, "S": 0, "j": 0}
    mismatch = f"date: {_quoted(value)} does not match format {_quoted(format_text)}"
    position = 0
    index = 0
    while index < len(format_text):
        char = format_text[index]
        if char == "%":
            if index + 1 == len(format_text):
                raise _DateError(f"date: format {_quoted(format_text)} ends with %")
            index += 1
            char = format_text[index]
            if char != "%":
                if char not in _DATE_WIDTHS:
                    raise _DateError(f"date: unsupported directive %{char}")
                digits = _read_digits(value, position, _DATE_WIDTHS[char])
                if not digits:
                    raise _DateError(mismatch)
                parts[char] = int(digits)
                position += len(digits)
                index += 1
                continue
        if position >= len(value) or value[position] != char:
            raise _DateError(mismatch)
        position += 1
        index += 1
    if position != len(value):
        raise _DateError(f"date: {_quoted(value)} has text after format {_quoted(format_text)}")
    return _timestamp(value, parts)


def _read_digits(value: str, position: int, width: int) -> str:
    end = position
    while end < len(value) and end - position < width and "0" <= value[end] <= "9":
        end += 1
    return value[position:end]


def _timestamp(value: str, parts: dict[str, int]) -> datetime.datetime:
    invalid = f"date: {_quoted(value)} is not a valid date"
    try:
        if parts["j"] > 0:
            start = datetime.datetime(parts["Y"], 1, 1, tzinfo=datetime.UTC)
            result = start + datetime.timedelta(days=parts["j"] - 1)
            if result.year != parts["Y"]:
                raise _DateError(invalid)
            return result.replace(hour=parts["H"], minute=parts["M"], second=parts["S"])
        return datetime.datetime(
            parts["Y"],
            parts["m"],
            parts["d"],
            parts["H"],
            parts["M"],
            parts["S"],
            tzinfo=datetime.UTC,
        )
    except (ValueError, OverflowError) as exc:
        raise _DateError(invalid) from exc


def date(value: Any, format_text: Any) -> celtypes.TimestampType | CELEvalError:
    if not isinstance(value, str) or not isinstance(format_text, str):
        return _no_overload("date", [value, format_text])
    try:
        return celtypes.TimestampType(parse_date(value, format_text))
    except _DateError as exc:
        return _error(str(exc))


# Dispatch on the receiver of a method call.

_IMPLEMENTATIONS: dict[tuple[str, str], Callable[..., Any]] = {
    ("path", "basename"): basename,
    ("path", "dirname"): dirname,
    ("path", "stem"): stem,
    ("path", "ext"): ext,
    ("path", "join"): join,
    ("media", "matches"): media_matches,
    ("media", "accepts"): media_accepts,
    ("media", "base"): media_base,
    ("media", "param"): media_param,
    ("media", "ext"): media_ext,
}


def _string_result(value: Any) -> Any:
    if isinstance(value, bool):
        return celtypes.BoolType(value)
    if isinstance(value, str):
        return celtypes.StringType(value)
    return value


def _namespaced(method: str, fallback: Callable[..., Any] | None = None) -> Callable[..., Any]:
    def call(receiver: Any, *args: Any) -> Any:
        if not isinstance(receiver, Namespace):
            if fallback is not None:
                return fallback(receiver, *args)
            return _no_overload(method, [receiver, *args])
        name = f"{receiver.name}.{method}"
        implementation = _IMPLEMENTATIONS.get((receiver.name, method))
        if implementation is None:
            return CELEvalError(f"undeclared reference to '{name}'", KeyError, None)
        arity, _ = SIGNATURES[(receiver.name, method)]
        accepted = (str,) if (receiver.name, method) != ("media", "accepts") else (str, list)
        if len(args) != arity or not all(
            isinstance(arg, str if index else accepted) for index, arg in enumerate(args)
        ):
            return _no_overload(name, args)
        return _string_result(implementation(*args))

    call.__name__ = f"glow_{method}"
    return call


def _methods() -> dict[str, Callable[..., Any]]:
    methods = {method for _, method in _IMPLEMENTATIONS}
    # `matches` is also the standard string method: keep it for other receivers.
    return {
        method: _namespaced(method, base_functions.get(method) if method == "matches" else None)
        for method in sorted(methods)
    }


FUNCTIONS: dict[str, Callable[..., Any]] = {"date": date, **_methods()}
