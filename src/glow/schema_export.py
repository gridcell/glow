"""Generate the committed JSON Schemas from the pydantic models.

The models are the source of truth. The JSON files under `glow/schemas/` are
generated output, committed so that editors and other tools can use them, and
checked in CI against the models.
"""

import json
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema
from pydantic_core import core_schema

from glow.models import ToolpackManifest, Workflow

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

SCHEMA_MODELS: dict[str, tuple[type[BaseModel], str]] = {
    "workflow.schema.json": (Workflow, "GLOW workflow"),
    "toolpack.schema.json": (ToolpackManifest, "GLOW toolpack manifest"),
}


class _GlowJsonSchema(GenerateJsonSchema):
    """Schema generator tuned for hand-written YAML files.

    Optional fields are written as their plain type, not `anyOf [T, null]`,
    and without per-property titles, which keeps the schema readable and
    gives clearer validation messages.
    """

    def nullable_schema(self, schema: core_schema.NullableSchema) -> dict[str, Any]:
        return self.generate_inner(schema["schema"])

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> dict[str, Any]:
        if schema.get("default", ...) is None:
            return self.generate_inner(schema["schema"])
        return super().default_schema(schema)

    def field_title_should_be_set(self, schema: Any) -> bool:
        return False

    def dict_schema(self, schema: core_schema.DictSchema) -> dict[str, Any]:
        # pydantic renders constrained keys as `patternProperties`, which lets
        # keys that miss the pattern through. Constrain the names instead.
        result = super().dict_schema(schema)
        patterns = result.pop("patternProperties", None)
        if patterns:
            ((pattern, value_schema),) = patterns.items()
            result["propertyNames"] = {**result.get("propertyNames", {}), "pattern": pattern}
            result["additionalProperties"] = value_schema
        return result


def build_schema(model: type[BaseModel], title: str) -> dict[str, Any]:
    schema = model.model_json_schema(mode="validation", schema_generator=_GlowJsonSchema)
    schema["title"] = title
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def render_schema(model: type[BaseModel], title: str) -> str:
    return json.dumps(build_schema(model, title), indent=2, sort_keys=True) + "\n"


def schema_dir() -> Path:
    return Path(str(resources.files("glow") / "schemas"))


def load_schema(filename: str) -> dict[str, Any]:
    return json.loads((resources.files("glow") / "schemas" / filename).read_text("utf-8"))


def write_schemas(directory: Path) -> list[Path]:
    """Write every schema into `directory` and return the paths written."""
    written = []
    for filename, (model, title) in SCHEMA_MODELS.items():
        path = directory / filename
        path.write_text(render_schema(model, title), encoding="utf-8")
        written.append(path)
    return written


def stale_schemas(directory: Path) -> list[Path]:
    """Return the schema files in `directory` that differ from the models."""
    stale = []
    for filename, (model, title) in SCHEMA_MODELS.items():
        path = directory / filename
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != render_schema(model, title):
            stale.append(path)
    return stale
