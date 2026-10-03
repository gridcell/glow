"""Schema-level validation of workflow files and toolpack manifests.

A document is checked twice. The committed JSON Schema gives structural
errors with stable JSON paths. The pydantic model then adds the cross-field
rules JSON Schema cannot express (error types starting with `glow_`). While
the schema reports errors, other model errors are dropped because they
repeat the schema's findings in pydantic's wording.
"""

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from jsonschema.exceptions import best_match
from pydantic import BaseModel
from pydantic import ValidationError as ModelError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from glow.models import ToolpackManifest, Workflow
from glow.schema_export import load_schema

# Workflow files and manifests are small; the cap bounds memory and time when
# a validator runs on untrusted uploads.
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_MESSAGE_CHARS = 200

DocumentKind = Literal["workflow", "toolpack"]

_KINDS: dict[DocumentKind, tuple[type[BaseModel], str]] = {
    "workflow": (Workflow, "workflow.schema.json"),
    "toolpack": (ToolpackManifest, "toolpack.schema.json"),
}
_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DocumentReadError(Exception):
    """The file could not be read at all (missing, too large, not UTF-8)."""


@dataclass(frozen=True, order=True, slots=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    kind: DocumentKind | None
    problems: list[Problem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects aliases, duplicate keys and non-string keys.

    Aliases allow exponential expansion ("billion laughs") once the data is
    walked; duplicate keys silently drop a value; non-string keys usually mean
    a YAML 1.1 surprise such as `on:` loading as `True`.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None, None, "YAML aliases are not supported", event.start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[str] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ConstructorError(
                    None, None, f"mapping key {key!r} must be a string", key_node.start_mark
                )
            if key in seen:
                raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def read_document(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError as exc:
        raise DocumentReadError(f"cannot read {path}: {exc.strerror or exc}") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentReadError(f"{path} is larger than {MAX_DOCUMENT_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentReadError(f"{path} is not valid UTF-8") from exc


def validate_file(path: Path) -> ValidationResult:
    """Validate a workflow file or toolpack manifest on disk."""
    return validate_text(read_document(path))


def validate_text(text: str) -> ValidationResult:
    try:
        data = yaml.load(text, Loader=_StrictSafeLoader)
    except yaml.YAMLError as exc:
        return ValidationResult(None, [Problem("$", f"invalid YAML: {_yaml_error_message(exc)}")])
    return validate_document(data)


def detect_kind(data: Any) -> DocumentKind:
    return "toolpack" if isinstance(data, dict) and "toolpack" in data else "workflow"


def validate_document(data: Any) -> ValidationResult:
    """Validate already-loaded data, choosing the model by the `toolpack` key."""
    kind = detect_kind(data)
    model, schema_file = _KINDS[kind]
    schema_problems = list(_schema_problems(_schema_validator(schema_file).iter_errors(data)))
    model_problems = list(_model_problems(model, data, glow_rules_only=bool(schema_problems)))
    problems = sorted(set(schema_problems + model_problems))
    return ValidationResult(kind, problems)


@cache
def _schema_validator(schema_file: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(schema_file))


def _schema_problems(errors: Iterable[SchemaError]) -> Iterator[Problem]:
    for error in errors:
        while error.context:
            error = _relevant_branch_error(error.context)
        path = list(error.absolute_path)
        if error.validator == "required":
            missing = [name for name in error.validator_value if name not in error.instance]
            for name in missing:
                yield Problem(format_path([*path, name]), "required property is missing")
        elif error.validator == "additionalProperties" and error.validator_value is False:
            known = error.schema.get("properties", {})
            for name in error.instance:
                if name not in known:
                    yield Problem(format_path([*path, name]), "unknown property")
        elif error.validator == "type":
            message = f"expected {error.validator_value}, got {_json_type(error.instance)}"
            yield Problem(format_path(path), message)
        else:
            yield Problem(format_path(path), _one_line(error.message))


def _relevant_branch_error(errors: list[SchemaError]) -> SchemaError:
    """Pick the anyOf/oneOf branch error that best explains the failure.

    A branch whose type matched the value says more than one that only
    reports a type mismatch: for `timeout: 5x`, the duration pattern error
    beats "expected integer".
    """
    type_matched = [error for error in errors if error.validator != "type"]
    return best_match(type_matched or errors)


def _model_problems(
    model: type[BaseModel], data: Any, *, glow_rules_only: bool
) -> Iterator[Problem]:
    try:
        model.model_validate(data)
    except ModelError as exc:
        for error in exc.errors(include_url=False, include_input=False):
            if glow_rules_only and not error["type"].startswith("glow_"):
                continue
            yield Problem(format_path(error["loc"]), _one_line(error["msg"]))


def format_path(parts: Iterable[str | int]) -> str:
    """Format a location as a JSON path such as `$.steps[2].with.source`."""
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        elif _PLAIN_KEY.match(part):
            path += f".{part}"
        else:
            path += f"[{json.dumps(part)}]"
    return path


def _yaml_error_message(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        mark = exc.problem_mark
        return f"{exc.problem} at line {mark.line + 1}, column {mark.column + 1}"
    return _one_line(str(exc))


def _json_type(value: Any) -> str:
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int():
            return "integer"
        case float():
            return "number"
        case str():
            return "string"
        case list():
            return "array"
        case dict():
            return "object"
    return type(value).__name__


def _one_line(message: str) -> str:
    flat = " ".join(message.split())
    if len(flat) > MAX_MESSAGE_CHARS:
        return flat[: MAX_MESSAGE_CHARS - 3] + "..."
    return flat
