"""Catalog of built-in tools (plan section 5.4).

Built-ins use the same `Tool` model as toolpack manifests, so the validator
checks `with` blocks and output references for both in one way. They have no
`command` and no image, and workflows refer to them without `@major`.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from glow.models import Tool

_FILE: dict[str, Any] = {"type": "file"}

_ROOT: dict[str, Any] = {
    "type": "string",
    "format": "uri",
    "description": "Registered location to list, such as s3://bucket/prefix/.",
}

# One group as produced by fs.group. `captures` holds every named capture of
# the pattern for the group's files.
_GROUP: dict[str, Any] = {
    "type": "group",
    "properties": {
        "key": {"type": "string", "description": "Value of the `key` capture."},
        "captures": {"type": "object", "additionalProperties": {"type": "string"}},
        "files": {"type": "array", "items": _FILE},
    },
    "required": ["key", "captures", "files"],
}

_SPECS: list[dict[str, Any]] = [
    {
        "name": "fs.group",
        "description": (
            "List files under root whose path relative to root matches pattern, "
            "and group them by the value of one named capture."
        ),
        "inputs": {
            "root": _ROOT,
            "pattern": {
                "type": "string",
                "format": "regex",
                "description": "Regular expression with named captures, such as (?P<date>\\d{8}).",
            },
            "key": {"type": "string", "description": "Name of the capture to group by."},
            "media_type": {
                "type": "string",
                "description": "Media type to assign to the listed files.",
            },
        },
        "required": ["root", "pattern", "key"],
        "outputs": {
            "groups": {
                "type": "array",
                "items": _GROUP,
                "description": "One group per distinct key.",
            },
        },
    },
    {
        "name": "fs.glob",
        "description": "List files under root whose path relative to root matches a glob pattern.",
        "inputs": {
            "root": _ROOT,
            "pattern": {"type": "string", "description": "Glob pattern, such as **/*.tif."},
        },
        "required": ["root", "pattern"],
        "outputs": {
            "files": {"type": "array", "items": _FILE, "description": "Matching files."},
        },
    },
]

BUILTINS: Mapping[str, Tool] = MappingProxyType(
    {spec["name"]: Tool.model_validate(spec) for spec in _SPECS}
)
