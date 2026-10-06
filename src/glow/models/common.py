"""Types shared by the workflow and toolpack models."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

# Names that appear in expressions (`steps.<id>`, `inputs.<name>`, loop
# variables) must be valid CEL identifiers, so hyphens are not allowed.
Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=63)]

DATA_KINDS: frozenset[str] = frozenset({"file", "bundle", "group"})

# A value type usable in tool inputs and outputs: a JSON Schema type or a data kind.
ValueType = Literal[
    "string", "number", "integer", "boolean", "object", "array", "file", "bundle", "group"
]

# A media type such as `image/tiff; application=geotiff`, or `*` for any.
MediaType = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class StrictModel(BaseModel):
    """Base for every file model: unknown keys are errors, not silently dropped."""

    model_config = ConfigDict(extra="forbid", frozen=True)
