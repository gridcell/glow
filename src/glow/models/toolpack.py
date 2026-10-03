"""Toolpack manifest model (plan section 5.1).

A tool's `inputs` are JSON Schema properties, restricted to the keywords
below, plus the data kinds `file`, `bundle` and `group` with their
`media_type` and `remote` annotations.
"""

from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from glow.models.common import DATA_KINDS, Identifier, MediaType, StrictModel, ValueType

ToolpackName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=63)]
ToolName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=128)
]
# Images are pinned by digest so a manifest always names the exact bytes it runs.
ImageRef = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$", max_length=512)
]


class ToolInput(StrictModel):
    """One input property of a tool."""

    type: ValueType
    description: str | None = None
    format: str | None = None
    enum: Annotated[list[Any], Field(min_length=1)] | None = None
    default: Any = None
    items: "ToolInput | None" = None
    properties: dict[str, "ToolInput"] | None = None
    required: list[str] | None = None
    additional_properties: "ToolInput | bool | None" = Field(
        default=None, alias="additionalProperties"
    )
    minimum: float | None = None
    maximum: float | None = None
    min_items: Annotated[int, Field(ge=0)] | None = Field(default=None, alias="minItems")
    max_items: Annotated[int, Field(ge=0)] | None = Field(default=None, alias="maxItems")
    media_type: MediaType | list[MediaType] | None = None
    remote: bool | None = Field(
        default=None, description="The tool can read this input from a URI with staging: none."
    )

    @model_validator(mode="after")
    def _data_kind_annotations(self) -> "ToolInput":
        if self.type in DATA_KINDS:
            return self
        for key, value in (("media_type", self.media_type), ("remote", self.remote)):
            if value is not None:
                raise PydanticCustomError(
                    "glow_tool_input",
                    "{key} is only allowed on file, bundle or group inputs, not {type}",
                    {"key": key, "type": self.type},
                )
        return self


class ToolOutput(StrictModel):
    """One declared output of a tool.

    A file output names its media type directly with `media_type`, or derives
    it from an input value with `media_type_from` and the `media_types` map.
    """

    type: ValueType
    description: str | None = None
    path: str | None = Field(
        default=None, description="Fixed path relative to /work/out/; may use {ext}."
    )
    media_type: MediaType | None = None
    media_type_from: Identifier | None = None
    media_types: dict[str, MediaType] | None = None

    @model_validator(mode="after")
    def _check(self) -> "ToolOutput":
        problem = self._path_problem() or self._media_type_problem()
        if problem is not None:
            raise PydanticCustomError("glow_tool_output", problem)
        return self

    def _path_problem(self) -> str | None:
        if self.path is None:
            return None
        # Outputs are collected from /work/out/; keep the path inside it.
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            return "path must be relative to /work/out/ and must not contain '..'"
        return None

    def _media_type_problem(self) -> str | None:
        has_media_info = any(
            value is not None for value in (self.media_type, self.media_type_from, self.media_types)
        )
        if has_media_info and self.type not in DATA_KINDS:
            return f"media types are only allowed on file, bundle or group outputs, not {self.type}"
        if self.media_type is not None and self.media_type_from is not None:
            return "set media_type or media_type_from, not both"
        if (self.media_type_from is None) != (self.media_types is None):
            return "media_type_from and media_types must be set together"
        return None


class Tool(StrictModel):
    """One tool in a toolpack. Doubles as an MCP tool definition."""

    name: ToolName
    description: Annotated[str, StringConstraints(min_length=1)]
    inputs: dict[Identifier, ToolInput] = Field(default_factory=dict)
    required: list[Identifier] = Field(
        default_factory=list, description="Input names that must be set, as in JSON Schema."
    )
    outputs: dict[Identifier, ToolOutput] = Field(default_factory=dict)
    command: Annotated[list[Annotated[str, StringConstraints(min_length=1)]], Field(min_length=1)]

    @model_validator(mode="after")
    def _check(self) -> "Tool":
        problem = self._required_problem() or self._media_type_from_problem()
        if problem is not None:
            raise PydanticCustomError("glow_tool", problem)
        return self

    def _required_problem(self) -> str | None:
        unknown = [name for name in self.required if name not in self.inputs]
        if unknown:
            return f"required names unknown inputs: {', '.join(unknown)}"
        return None

    def _media_type_from_problem(self) -> str | None:
        for output_name, output in self.outputs.items():
            source = output.media_type_from
            if source is None:
                continue
            if source not in self.inputs:
                return f"output '{output_name}' takes media_type_from unknown input '{source}'"
            choices = self.inputs[source].enum
            missing = [str(c) for c in choices or [] if str(c) not in (output.media_types or {})]
            if missing:
                return f"output '{output_name}' has no media_types entry for: {', '.join(missing)}"
        return None


class ToolpackManifest(StrictModel):
    """A toolpack: one container image and the tools it provides."""

    toolpack: ToolpackName
    image: ImageRef
    tools: Annotated[list[Tool], Field(min_length=1)]

    @model_validator(mode="after")
    def _check_tool_names(self) -> "ToolpackManifest":
        seen: set[str] = set()
        prefix = f"{self.toolpack}."
        for tool in self.tools:
            if not tool.name.startswith(prefix):
                raise PydanticCustomError(
                    "glow_toolpack",
                    "tool '{name}' must be named '{prefix}<tool>'",
                    {"name": tool.name, "prefix": prefix},
                )
            if tool.name in seen:
                raise PydanticCustomError(
                    "glow_toolpack", "duplicate tool name '{name}'", {"name": tool.name}
                )
            seen.add(tool.name)
        return self
