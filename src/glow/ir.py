"""Intermediate representation of a validated workflow.

The IR is what the compiler and the local runner consume: every step with
its resolved tool identity, the sibling dependencies, the loop scopes, and
every edge with its type and whether it is checked statically or at runtime.
It round-trips through JSON, with types encoded as tagged objects such as
`{"kind": "file", "media_types": ["image/png"]}`.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, PlainSerializer, PlainValidator, WithJsonSchema

from glow.models import Resources
from glow.models.common import StrictModel
from glow.models.workflow import Staging, StepKind
from glow.types import Array, Bundle, File, GlowType, Group, MediaType, Scalar, Unknown

_DATA_KINDS: dict[str, type[File | Bundle | Group]] = {
    "file": File,
    "bundle": Bundle,
    "group": Group,
}


def encode_type(glow_type: GlowType) -> dict[str, Any]:
    match glow_type:
        case Scalar(schema):
            return {"kind": "scalar", "schema": dict(schema)}
        case Array(member):
            return {"kind": "array", "member": encode_type(member)}
        case Unknown(reason):
            return {"kind": "unknown", "reason": reason}
    data: dict[str, Any] = {
        "kind": glow_type.kind,
        "media_types": [str(media_type) for media_type in glow_type.media_types],
        "unknown_reason": glow_type.unknown_reason,
    }
    if isinstance(glow_type, Group):
        data["captures"] = list(glow_type.captures)
    return data


def decode_type(value: Any) -> GlowType:
    if isinstance(value, Scalar | File | Bundle | Group | Array | Unknown):
        return value
    if not isinstance(value, dict):
        raise ValueError("a type must be an object with a 'kind'")
    kind = value.get("kind")
    if kind == "scalar":
        return Scalar(dict(value["schema"]))
    if kind == "array":
        return Array(decode_type(value["member"]))
    if kind == "unknown":
        return Unknown(str(value["reason"]))
    if kind in _DATA_KINDS:
        media_types = tuple(MediaType.parse(text) for text in value.get("media_types", []))
        reason = value.get("unknown_reason")
        if kind == "group":
            return Group(media_types, reason, tuple(value.get("captures", [])))
        return _DATA_KINDS[kind](media_types, reason)
    raise ValueError(f"unknown type kind {kind!r}")


TypeRef = Annotated[
    GlowType,
    PlainValidator(decode_type),
    PlainSerializer(encode_type),
    WithJsonSchema({"type": "object", "required": ["kind"]}),
]

Check = Literal["ok", "runtime_check", "unchecked"]
SymbolKind = Literal["input", "step", "loop", "let"]


class ToolIdentity(StrictModel):
    """A resolved `uses`. Built-ins have no toolpack, image or digest."""

    uses: str
    name: str
    builtin: bool
    toolpack: str | None = None
    image: str | None = None
    digest: str | None = None
    manifest_sha256: str | None = None


class Edge(StrictModel):
    """One reference feeding one field of a step.

    `type` is the type of the referenced value. `check` is `ok` when the edge
    is proven statically, `runtime_check` when glow-exec checks it, with the
    reason, and `unchecked` when the field declares no type. `symbol` says
    what the reference resolved to; for a loop variable or a let, `binder` is
    the for_each step that binds it, which tells an inner name from an outer
    one it shadows.
    """

    source: str = Field(description="The reference as written, such as steps.cog.outputs.result.")
    source_step: str | None = Field(description="The producing step for steps.* references.")
    symbol: SymbolKind
    binder: str | None = Field(
        default=None, description="The for_each step binding a loop variable or let reference."
    )
    target_step: str
    target: str = Field(description="The field on the target step, such as with.source.")
    expression: str = Field(description="The ${{ }} span holding the reference.")
    type: TypeRef
    accepts: TypeRef | None = None
    check: Check
    reason: str | None = None


class Loop(StrictModel):
    """The scope a for_each step opens."""

    variable: str
    operand: str | list[Any] = Field(description="The for_each value as written.")
    over: TypeRef
    item: TypeRef
    max_parallelism: int | None = None


class Step(StrictModel):
    id: str
    kind: StepKind
    parent: str | None = Field(description="The enclosing for_each block, if any.")
    tool: ToolIdentity | None = None
    tool_spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The tool spec glow-exec checks the step against: the manifest tool for uses, "
            "a synthesized spec for run and script. None for a block."
        ),
    )
    raw_with: dict[str, Any] = Field(
        default_factory=dict, description="The `with` block as written, expressions unevaluated."
    )
    run: str | None = Field(default=None, description="The inline bash script of a run step.")
    script: str | None = Field(default=None, description="The inline Python script.")
    depends_on: list[str] = Field(description="Sibling steps whose outputs this step uses.")
    loop: Loop | None = None
    let: dict[str, TypeRef] = Field(default_factory=dict)
    let_values: dict[str, str] = Field(
        default_factory=dict, description="The let expressions as written, in definition order."
    )
    outputs: dict[str, TypeRef] = Field(
        default_factory=dict, description="Output types as later steps see them."
    )
    block_outputs: dict[str, str] = Field(
        default_factory=dict, description="A block's output expressions, by output name."
    )
    condition: str | None = Field(default=None, description="The `if` expression.")
    staging: Staging = "copy"
    resources: Resources | None = None
    timeout: int | str | None = None
    retries: int | None = None
    secrets: list[str] = Field(default_factory=list)


class Workflow(StrictModel):
    """A validated workflow. Steps are in dependency order, each block before its members."""

    name: str
    inputs: dict[str, TypeRef]
    defaults: dict[str, Any] = Field(
        default_factory=dict, description="Default values of the inputs that declare one."
    )
    steps: list[Step]
    edges: list[Edge]

    def step(self, step_id: str) -> Step:
        return next(step for step in self.steps if step.id == step_id)

    def edges_into(self, step_id: str) -> list[Edge]:
        return [edge for edge in self.edges if edge.target_step == step_id]
