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
# `local/<toolpack>:dev` is the one exception, for local development; the
# compiler refuses it unless told otherwise.
DIGEST_IMAGE_PATTERN = r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}"
LOCAL_IMAGE_PATTERN = r"local/[a-z][a-z0-9_]*:dev"
ImageRef = Annotated[
    str,
    StringConstraints(pattern=rf"^({DIGEST_IMAGE_PATTERN}|{LOCAL_IMAGE_PATTERN})$", max_length=512),
]
MajorVersion = Annotated[int, Field(strict=True, ge=1, le=999_999)]


def local_image(toolpack: str) -> str:
    """The local development image reference for a toolpack."""
    return f"local/{toolpack}:dev"


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
    items: ToolInput | None = Field(default=None, description="Element type of an array output.")

    @model_validator(mode="after")
    def _check(self) -> "ToolOutput":
        problem = self._path_problem() or self._media_type_problem() or self._items_problem()
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

    def _items_problem(self) -> str | None:
        if self.items is not None and self.type != "array":
            return f"items is only allowed on array outputs, not {self.type}"
        return None


class Tool(StrictModel):
    """One tool in a toolpack, or an in-engine built-in such as `fs.group`.

    Doubles as an MCP tool definition. Built-ins run inside the engine and
    have no `command`; a toolpack manifest requires one on every tool.
    """

    name: ToolName
    description: Annotated[str, StringConstraints(min_length=1)]
    inputs: dict[Identifier, ToolInput] = Field(default_factory=dict)
    required: list[Identifier] = Field(
        default_factory=list, description="Input names that must be set, as in JSON Schema."
    )
    outputs: dict[Identifier, ToolOutput] = Field(default_factory=dict)
    command: (
        Annotated[list[Annotated[str, StringConstraints(min_length=1)]], Field(min_length=1)] | None
    ) = None

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
            if choices is None:
                return f"output '{output_name}' takes media_type_from input '{source}' without enum"
            missing = [str(c) for c in choices if str(c) not in (output.media_types or {})]
            if missing:
                return f"output '{output_name}' has no media_types entry for: {', '.join(missing)}"
        return None


class ToolpackManifest(StrictModel):
    """A toolpack: one container image and the tools it provides."""

    toolpack: ToolpackName
    version: MajorVersion = Field(
        description="Major version. Workflows refer to the tools as `<tool>@<version>`."
    )
    image: ImageRef
    tools: Annotated[list[Tool], Field(min_length=1)]

    @property
    def is_local(self) -> bool:
        """The image is the local development image, not a digest reference."""
        return "@" not in self.image

    @model_validator(mode="after")
    def _check(self) -> "ToolpackManifest":
        if self.is_local and self.image != local_image(self.toolpack):
            raise PydanticCustomError(
                "glow_toolpack",
                "local image must be '{expected}'",
                {"expected": local_image(self.toolpack)},
            )
        self._check_tools()
        return self

    def _check_tools(self) -> None:
        seen: set[str] = set()
        prefix = f"{self.toolpack}."
        for tool in self.tools:
            if tool.command is None:
                raise PydanticCustomError(
                    "glow_toolpack", "tool '{name}' must set command", {"name": tool.name}
                )
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
