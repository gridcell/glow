"""Workflow file model (plan section 5.2).

Expressions (`${{ ... }}`) are opaque strings here. Structural rules that
JSON Schema cannot express, such as "exactly one step body", are enforced by
model validators that raise errors whose type starts with `glow_`.
"""

from collections.abc import Iterator
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from glow.models.common import DATA_KINDS, Identifier, MediaType, StrictModel, ValueType

InputType = Literal[
    "string", "number", "integer", "boolean", "object", "array", "uri", "file", "bundle", "group"
]
StepKind = Literal["uses", "run", "script", "for_each"]
Staging = Literal["copy", "none", "auto"]

# Kubernetes DNS label: the workflow name ends up in Argo object names.
WorkflowName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
]
SecretName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=253)
]
# `toolpack.tool[.sub]` with an optional `@major`. Built-ins such as `fs.group` carry no version.
ToolRef = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+(@[0-9]+)?$", max_length=128),
]
Expression = Annotated[str, StringConstraints(min_length=1)]
CpuQuantity = Annotated[str, StringConstraints(pattern=r"^[0-9]+(\.[0-9]+)?m?$")]
MemoryQuantity = Annotated[
    str, StringConstraints(pattern=r"^[0-9]+(\.[0-9]+)?(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$")
]
# Seconds as an integer, or a duration such as `90s`, `30m` or `1h30m`.
Duration = Annotated[
    str,
    StringConstraints(pattern=r"^([0-9]+h([0-9]+m)?([0-9]+s)?|[0-9]+m([0-9]+s)?|[0-9]+s)$"),
]

STEP_BODIES: tuple[str, ...] = ("uses", "run", "script")


class InputSpec(StrictModel):
    """A workflow input declared under top-level `inputs`."""

    type: InputType
    media_type: MediaType | None = None
    default: Any = None
    description: str | None = None

    @model_validator(mode="after")
    def _media_type_needs_data_kind(self) -> "InputSpec":
        if self.media_type is not None and self.type not in DATA_KINDS:
            raise PydanticCustomError(
                "glow_input_media_type",
                "media_type is only allowed on file, bundle or group inputs, not {type}",
                {"type": self.type},
            )
        return self


class ResourceSpec(StrictModel):
    cpu: CpuQuantity | Annotated[float, Field(gt=0)] | None = None
    memory: MemoryQuantity | None = None
    gpu: Annotated[int, Field(ge=0)] | None = None


class Resources(StrictModel):
    requests: ResourceSpec | None = None
    limits: ResourceSpec | None = None


class OutputDecl(StrictModel):
    """A typed output declared by a `run` or `script` step."""

    type: ValueType
    media_type: MediaType | None = None
    description: str | None = None


class Step(StrictModel):
    """One step. Its body is `uses`, `run`, `script`, or a `for_each` fan-out.

    A `for_each` step either holds nested `steps` (a block, whose interface is
    `outputs` mapping names to expressions) or exactly one inline body.
    """

    id: Identifier
    uses: ToolRef | None = None
    run: str | None = Field(default=None, description="Inline bash script.")
    script: str | None = Field(default=None, description="Inline Python script.")
    for_each: Expression | list[Any] | None = None
    as_: Identifier | None = Field(default=None, alias="as")
    steps: Annotated[list["Step"], Field(min_length=1)] | None = None
    outputs: dict[Identifier, Expression | OutputDecl] | None = None
    with_: dict[str, Any] | None = Field(default=None, alias="with")
    if_: Expression | None = Field(default=None, alias="if")
    let: dict[Identifier, Expression] | None = None
    resources: Resources | None = None
    timeout: Annotated[int, Field(gt=0)] | Duration | None = None
    retries: Annotated[int, Field(ge=0)] | None = None
    staging: Staging | None = None
    secrets: Annotated[list[SecretName], Field(json_schema_extra={"uniqueItems": True})] | None = (
        None
    )
    max_parallelism: Annotated[int, Field(ge=1)] | None = None

    @property
    def kind(self) -> StepKind:
        if self.for_each is not None:
            return "for_each"
        if self.uses is not None:
            return "uses"
        if self.run is not None:
            return "run"
        return "script"

    def _bodies(self) -> list[str]:
        return [body for body in STEP_BODIES if getattr(self, body) is not None]

    @model_validator(mode="after")
    def _check_shape(self) -> "Step":
        problem = self._for_each_problem() if self.for_each is not None else self._plain_problem()
        problem = problem or self._outputs_problem()
        if problem is not None:
            raise PydanticCustomError("glow_step_shape", problem)
        return self

    def _plain_problem(self) -> str | None:
        bodies = self._bodies()
        if len(bodies) != 1:
            found = ", ".join(bodies) if bodies else "none"
            return f"step must set exactly one of uses, run, script, for_each (found: {found})"
        for key, value in (("as", self.as_), ("steps", self.steps)):
            if value is not None:
                return f"'{key}' is only allowed on a for_each step"
        if self.max_parallelism is not None:
            return "'max_parallelism' is only allowed on a for_each step"
        return None

    def _for_each_problem(self) -> str | None:
        bodies = self._bodies()
        if self.as_ is None:
            return "for_each requires 'as' to name the loop variable"
        if self.steps is not None and bodies:
            return (
                "for_each takes nested 'steps' or one inline body, not both "
                f"(found: steps, {', '.join(bodies)})"
            )
        if self.steps is None and len(bodies) != 1:
            found = ", ".join(bodies) if bodies else "none"
            return (
                "for_each requires nested 'steps' or exactly one of uses, run, script "
                f"(found: {found})"
            )
        return None

    def _outputs_problem(self) -> str | None:
        if self.steps is not None:
            return _outputs_must_be(self.outputs, str, "block outputs must be expressions")
        if self.run is not None or self.script is not None:
            if not self.outputs:
                return "run and script steps must declare 'outputs' with types"
            return _outputs_must_be(self.outputs, OutputDecl, "script outputs must declare a type")
        if self.outputs is not None:
            return "'outputs' is not allowed on a uses step; outputs come from the tool manifest"
        return None


def _outputs_must_be(outputs: dict[str, Any] | None, expected: type, message: str) -> str | None:
    for name, value in (outputs or {}).items():
        if not isinstance(value, expected):
            return f"{message} (output '{name}')"
    return None


def iter_steps(steps: list[Step]) -> Iterator[Step]:
    """Yield every step depth first, including steps nested in for_each blocks."""
    for step in steps:
        yield step
        yield from iter_steps(step.steps or [])


class Workflow(StrictModel):
    """A GLOW v2 workflow file."""

    name: WorkflowName
    inputs: dict[Identifier, InputSpec] = Field(default_factory=dict)
    steps: Annotated[list[Step], Field(min_length=1)]

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "Workflow":
        # Ids are unique across the whole file, not only per scope, so a nested
        # `steps.<id>` reference can never shadow an outer step.
        seen: set[str] = set()
        for step in iter_steps(self.steps):
            if step.id in seen:
                raise PydanticCustomError(
                    "glow_duplicate_step_id", "duplicate step id '{id}'", {"id": step.id}
                )
            seen.add(step.id)
        return self
