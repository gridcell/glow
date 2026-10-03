"""Workflow input values from `glow run --input name=value`.

Each value is read according to the declared input type:

- `string` and `uri` take the text as it is; a `uri` without a scheme is a
  local path, made absolute against the current directory;
- `file` and `bundle` take a local path, `file://` or `s3://` URI and become
  a resolved map `{uri, media_type, kind}`, so glow-exec checks the media
  type when it stages the file;
- `integer`, `number`, `boolean`, `array` and `object` take JSON text;
- an input without a known type takes JSON, or the text when it is not JSON.

Inputs that are not given take their default. A missing required input, an
unknown name or a value of the wrong type is an error.
"""

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glow import ir
from glow.types import Bundle, File, GlowType, Group, Scalar

_JSON_TYPES: Mapping[str, type | tuple[type, ...]] = {
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


class InputError(ValueError):
    """Raised when the given inputs do not fit the workflow."""


@dataclass(frozen=True, slots=True)
class LocalPath:
    """A local path that a workflow input names, with how containers may use it."""

    path: Path
    writable: bool


def parse_assignments(assignments: Iterable[str]) -> dict[str, str]:
    """`name=value` pairs. The value may contain `=`; a repeated name is an error."""
    values: dict[str, str] = {}
    for assignment in assignments:
        name, found, value = assignment.partition("=")
        if not found or not name:
            raise InputError(f"--input {assignment!r} must have the form name=value")
        if name in values:
            raise InputError(f"input {name!r} is given more than once")
        values[name] = value
    return values


def resolve_inputs(
    workflow: ir.Workflow, given: Mapping[str, str], cwd: Path | None = None
) -> dict[str, Any]:
    """The value of every workflow input, from the given text or the default."""
    cwd = cwd or Path.cwd()
    problems = [f"input {name!r} is not declared" for name in given if name not in workflow.inputs]
    values: dict[str, Any] = {}
    for name, glow_type in workflow.inputs.items():
        if name in given:
            try:
                values[name] = _parse(glow_type, given[name], cwd)
            except InputError as exc:
                problems.append(f"input {name!r}: {exc}")
        elif name in workflow.defaults:
            values[name] = workflow.defaults[name]
        else:
            problems.append(f"input {name!r} is required; pass --input {name}=<value>")
    if problems:
        raise InputError("\n".join(problems))
    return values


def local_paths(workflow: ir.Workflow, values: Mapping[str, Any]) -> list[LocalPath]:
    """Local paths named by `uri`, `file` and `bundle` inputs.

    Tools read `file` and `bundle` inputs only, so they are read-only. A `uri`
    may be a destination such as `dest`, so it is writable.
    """
    paths = []
    for name, glow_type in workflow.inputs.items():
        value = values.get(name)
        uri = value.get("uri") if isinstance(value, dict) else value
        if not isinstance(uri, str) or uri.startswith("s3://"):
            continue
        if isinstance(glow_type, File | Bundle):
            paths.append(LocalPath(Path(uri.removeprefix("file://")), writable=False))
        elif _is_uri(glow_type):
            paths.append(LocalPath(Path(uri.removeprefix("file://")), writable=True))
    return paths


def _parse(glow_type: GlowType, text: str, cwd: Path) -> Any:
    if isinstance(glow_type, File | Bundle):
        return _data(glow_type, text, cwd)
    if isinstance(glow_type, Group):
        raise InputError("group inputs cannot be given on the command line")
    if _is_uri(glow_type):
        return _location(text, cwd)
    json_type = glow_type.schema.get("type") if isinstance(glow_type, Scalar) else None
    if json_type == "string":
        return text
    expected = _JSON_TYPES.get(str(json_type))
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        if expected is None:
            return text
        raise InputError(f"{text!r} is not valid JSON for a {json_type}") from None
    if expected is not None and (
        not isinstance(value, expected) or (json_type != "boolean" and isinstance(value, bool))
    ):
        raise InputError(f"{text!r} is not a {json_type}")
    return value


def _is_uri(glow_type: GlowType) -> bool:
    return isinstance(glow_type, Scalar) and glow_type.schema.get("format") == "uri"


def _location(text: str, cwd: Path) -> str:
    """An `s3://` URI as it is, or a local path or `file://` URI as an absolute path."""
    if not text:
        raise InputError("the value is empty")
    if text.startswith("s3://"):
        if len(text) <= len("s3://") or text[len("s3://")] == "/":
            raise InputError(f"{text!r} has no bucket")
        return text
    if "://" in text and not text.startswith("file://"):
        raise InputError(f"{text!r} must be a local path, file:// URI or s3:// URI")
    path = Path(text.removeprefix("file://"))
    return os.path.normpath(cwd / path)


def _data(glow_type: File | Bundle, text: str, cwd: Path) -> dict[str, Any]:
    uri = _location(text, cwd)
    if not uri.startswith("s3://"):
        path = Path(uri)
        exists = path.is_file() if isinstance(glow_type, File) else path.is_dir()
        if not exists:
            what = "file" if isinstance(glow_type, File) else "directory"
            raise InputError(f"{uri} is not an existing {what}")
    value: dict[str, Any] = {"uri": uri, "kind": glow_type.kind}
    # The declared media type, when there is exactly one, so that glow-exec
    # checks it against the tool input when it stages the file.
    if len(glow_type.media_types) == 1:
        value["media_type"] = str(glow_type.media_types[0])
    return value
