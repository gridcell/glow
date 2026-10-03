"""Compile the IR of a validated workflow to an Argo Workflow (plan sections 6.1 and 6.5).

The Workflow has one entrypoint DAG template, `main`, one DAG template per
for_each block (`<step>-block`), and one container template per distinct
tool configuration (`tool-<tool>`, `builtin-<tool>`, `script-<step>`). A
for_each with `max_parallelism` runs inside a `<step>-fanout` DAG template
whose `parallelism` bounds the fan-out.

Every step runs under glow-exec, which reads its parameters from the
environment (docs/glow-exec.md). Each Argo parameter carries JSON:

- `raw-with` is the base64 `with` block, expressions unevaluated;
- `scope` is a JSON object of the expression variables, assembled from
  workflow parameters and the loop variables the step uses, at any depth;
- `let` is the base64 list of the lets the step uses, which glow-exec
  evaluates in order;
- `upstream-<step>` is the `outputs.resolved.json` of a step the expressions
  use. After a fan-out it is rebuilt from the per-output aggregates, which
  Argo collects into JSON arrays; a nested fan-out gives arrays of arrays;
- `if` is the bare CEL text, combined with the `if` of enclosing blocks;
- `run-prefix` is where glow-exec uploads outputs.

Workflow parameter values must therefore be JSON too: a string input is
passed as `"text"`, with the quotes.

A block template sees only its own inputs, so the compiler threads every
outer value its members use through each template on the way down (see
`_Frame`): `loop-<block>` for a loop variable, `upstream-<step>` for an outer
step, `task-<step>-<output>` for an outer for_each operand, and `run-prefix`.
"""

import json
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from hera.workflows import Workflow
from hera.workflows import models as m

from glow import ir
from glow.compile import scope
from glow.compile.encoding import encode_json, encode_text
from glow.compile.errors import CompileError
from glow.compile.naming import NameTable, argo_name
from glow.models.workflow import Resources, ResourceSpec
from glow.types import GlowType, Group, Scalar
from glow.validate.errors import Code, GlowError

GLOW_EXEC = "/glow/exec"
# The engine image runs a built-in as `glow-builtin <name>`; built-ins have
# no major version, and glow-exec needs one.
BUILTIN_COMMAND = "glow-builtin"
BUILTIN_MAJOR = 1
SCRIPT_MAJOR = 1
RESOLVED_PARAMETER = "outputs"
RESOLVED_PATH = "/work/outputs.resolved.json"
OUTPUTS_DIR = "/work/outputs"
SECRETS_DIR = "/secrets"
WORK_SIZE_LIMIT = "20Gi"
GPU_RESOURCE = "nvidia.com/gpu"

DEFAULT_SERVICE_ACCOUNT = "glow-runner"
DEFAULT_RUN_PREFIX = "s3://glow-runs"
DEFAULT_GLOW_EXEC_IMAGE = "ghcr.io/sparkgeo/glow-exec:latest"
DEFAULT_ENGINE_IMAGE = "ghcr.io/sparkgeo/glow-engine:latest"
DEFAULT_SANDBOX_IMAGE = "ghcr.io/sparkgeo/glow-sandbox:latest"

# The script travels base64 in an argument, so `{{` in it never reaches Argo.
# `$1` is the encoded script; the shell never sees its decoded text.
_DECODE_SCRIPT = 'printf %s "$1" | base64 -d > /work/script && exec {interpreter} /work/script'
_INTERPRETERS = {"run": "bash", "script": "python3"}

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
_RUN_PREFIX = re.compile(r"(s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]|file://|/)[^\s{}]*")
_IMAGE = re.compile(r"[^\s{}]+")
_PATH = re.compile(r"\s*\$\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}\s*")
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")

_SECURITY_CONTEXT = m.SecurityContext(
    run_as_non_root=True,
    read_only_root_filesystem=True,
    allow_privilege_escalation=False,
    capabilities=m.Capabilities(drop=["ALL"]),
    seccomp_profile=m.SeccompProfile(type="RuntimeDefault"),
)


@dataclass(frozen=True, slots=True)
class CompileOptions:
    """Deployment settings. Raises `ValueError` for a value that is unsafe to embed."""

    namespace: str | None = None
    service_account: str = DEFAULT_SERVICE_ACCOUNT
    run_prefix: str = DEFAULT_RUN_PREFIX
    glow_exec_image: str = DEFAULT_GLOW_EXEC_IMAGE
    engine_image: str = DEFAULT_ENGINE_IMAGE
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    allow_local_images: bool = False

    def __post_init__(self) -> None:
        if self.namespace is not None and not _DNS_LABEL.fullmatch(self.namespace):
            raise ValueError(f"namespace {self.namespace!r} is not a Kubernetes namespace name")
        if not _DNS_SUBDOMAIN.fullmatch(self.service_account):
            raise ValueError(f"service account {self.service_account!r} is not a valid name")
        if not _RUN_PREFIX.fullmatch(self.run_prefix):
            raise ValueError(
                f"run prefix {self.run_prefix!r} must be an s3:// URI, a file:// URI "
                "or an absolute path, without spaces or braces"
            )
        for label, image in (
            ("glow-exec", self.glow_exec_image),
            ("engine", self.engine_image),
            ("sandbox", self.sandbox_image),
        ):
            if not _IMAGE.fullmatch(image):
                raise ValueError(f"{label} image {image!r} must not contain spaces or braces")
        object.__setattr__(self, "run_prefix", self.run_prefix.rstrip("/") or "/")


def compile_workflow(workflow: ir.Workflow, options: CompileOptions | None = None) -> Workflow:
    """Build the Argo Workflow. Raises `CompileError` listing every reason it cannot."""
    options = options or CompileOptions()
    errors = [*scope.check(workflow), *_image_errors(workflow, options)]
    errors += _default_errors(workflow)
    if errors:
        raise CompileError(errors)
    return _Compiler(workflow, options).build()


def to_yaml(workflow: Workflow) -> str:
    return workflow.to_yaml()


def _image_errors(workflow: ir.Workflow, options: CompileOptions) -> list[GlowError]:
    if options.allow_local_images:
        return []
    errors = []
    for step in workflow.steps:
        tool = step.tool
        if tool is not None and not tool.builtin and tool.digest is None:
            message = f"{tool.uses} runs the local development image {tool.image}"
            hint = "pin the image with `make images-lock`, or pass --allow-local-images"
            errors.append(GlowError(Code.LOCAL_IMAGE, f"{step.id}.uses", message, hint=hint))
    return errors


def _default_errors(workflow: ir.Workflow) -> list[GlowError]:
    errors = []
    for name, value in workflow.defaults.items():
        if "{{" in json.dumps(value):
            message = "a default holding '{{' cannot be passed as an Argo parameter"
            errors.append(GlowError(Code.NOT_YET_SUPPORTED, f"inputs.{name}.default", message))
    return errors


def _parameter(name: str, value: str | None = None, default: str | None = None) -> m.Parameter:
    return m.Parameter(name=name, value=value, default=default)


def _input(name: str) -> str:
    return f"{{{{inputs.parameters.{name}}}}}"


def _loop_input(block_task: str) -> str:
    return f"loop-{block_task}"


@dataclass(slots=True)
class _Frame:
    """The DAG template a task is built in, and the inputs it declares.

    `tasks` are the tasks the DAG holds; no other task is in reach. The
    caller passes the `provided` values, a block's loop variable and run
    prefix; every other value comes from `outer`, the frame of the caller.
    Asking for a value the frame does not hold adds a template input to
    `inputs`, with its value in the caller, so a template declares exactly
    the inputs its tasks use. `main` has no outer frame and writes under
    `top_prefix`. `conditions` are the `if` of the enclosing blocks, which
    every member also evaluates.
    """

    tasks: frozenset[str]
    top_prefix: str | None = None
    provided: dict[str, str] = field(default_factory=dict)
    conditions: tuple[str, ...] = ()
    outer: "_Frame | None" = None
    inputs: dict[str, str] = field(default_factory=dict)

    def request(self, name: str, value_in: Callable[["_Frame"], str]) -> str:
        """The template input `name`; `value_in(outer)` gives its value in the caller."""
        if name not in self.inputs:
            if name in self.provided:
                self.inputs[name] = self.provided[name]
            else:
                assert self.outer is not None, f"main has no value for {name}"
                self.inputs[name] = value_in(self.outer)
        return _input(name)

    def prefix(self) -> str:
        """The run prefix under which this DAG's steps write."""
        if self.top_prefix is not None:
            return self.top_prefix
        return self.request("run-prefix", _Frame.prefix)

    def loop_value(self, block_task: str) -> str:
        """The loop variable of a block, as JSON."""
        return self.request(_loop_input(block_task), lambda outer: outer.loop_value(block_task))

    def loop_expression(self, block_task: str) -> str:
        """The loop variable of a block, as a variable of an Argo `{{= }}` expression."""
        self.loop_value(block_task)
        return f"inputs.parameters['{_loop_input(block_task)}']"

    def task_output(self, task: str, parameter: str) -> str:
        if task in self.tasks:
            return _task_output(task, parameter)
        return self.request(
            f"task-{task}-{parameter}", lambda outer: outer.task_output(task, parameter)
        )


class _Compiler:
    def __init__(self, workflow: ir.Workflow, options: CompileOptions) -> None:
        self.workflow = workflow
        self.options = options
        self.children: dict[str | None, list[ir.Step]] = defaultdict(list)
        for step in workflow.steps:
            self.children[step.parent].append(step)
        tasks = NameTable("task")
        self.names = {step.id: tasks.claim(argo_name(step.id), step.id) for step in workflow.steps}
        self.templates = NameTable("template")
        self.errors: list[GlowError] = tasks.errors
        self.dag_templates: list[m.Template] = []
        self.leaf_templates: dict[str, m.Template] = {}

    def build(self) -> Workflow:
        self.templates.claim("main", "main")
        top = f"{self.options.run_prefix}/runs/{{{{workflow.uid}}}}/steps"
        frame = _Frame(self._task_names(None), top_prefix=top)
        main = m.Template(name="main", dag=m.DAGTemplate(tasks=self._tasks(frame, None)))
        errors = self.errors + self.templates.errors
        if errors:
            raise CompileError(errors)
        return Workflow(
            generate_name=f"{self.workflow.name}-",
            namespace=self.options.namespace,
            entrypoint="main",
            service_account_name=self.options.service_account,
            arguments=[
                _parameter(name, _default_value(self.workflow, name))
                for name in self.workflow.inputs
            ],
            templates=[main, *self.dag_templates, *self.leaf_templates.values()],
        )

    def _tasks(self, frame: _Frame, owner: str | None) -> list[m.DAGTask]:
        return [self._task(step, frame) for step in self.children[owner]]

    def _task_names(self, owner: str | None) -> frozenset[str]:
        return frozenset(self.names[step.id] for step in self.children[owner])

    def _task(self, step: ir.Step, frame: _Frame) -> m.DAGTask:
        name = self.names[step.id]
        depends = " && ".join(self.names[producer] for producer in step.depends_on) or None
        if step.loop is not None and step.loop.max_parallelism is not None:
            return self._fanout_task(step, frame, name, depends)
        return self._inner_task(step, frame, name, depends)

    def _inner_task(
        self, step: ir.Step, frame: _Frame, name: str, depends: str | None
    ) -> m.DAGTask:
        if step.loop is None:
            needs = scope.environment(self.workflow, step)
            run_prefix = f"{frame.prefix()}/{name}"
            arguments = self._body_arguments(step, frame, needs, None, run_prefix)
            template = self._body_template(step, needs)
            return m.DAGTask(
                name=name,
                template=template,
                depends=depends,
                arguments=m.Arguments(parameters=arguments),
            )
        item = _item_value(step.loop.item)
        with_param = self._with_param(step, frame)
        run_prefix = f"{frame.prefix()}/{name}/{_item_segment(step.loop.item)}"
        if step.id in self.children:
            template, arguments = self._block_call(step, frame, item, run_prefix)
        else:
            needs = scope.environment(self.workflow, step)
            template = self._body_template(step, needs)
            arguments = self._body_arguments(step, frame, needs, item, run_prefix)
        return m.DAGTask(
            name=name,
            template=template,
            depends=depends,
            with_param=with_param,
            arguments=m.Arguments(parameters=arguments),
        )

    def _fanout_task(
        self, step: ir.Step, frame: _Frame, name: str, depends: str | None
    ) -> m.DAGTask:
        # Argo bounds parallelism per template, not per task, so the fan-out
        # gets a template of its own whose only task is the fan-out.
        assert step.loop is not None and step.loop.max_parallelism is not None
        template_name = self.templates.claim(f"{name}-fanout", step.id)
        slot = self._reserve(template_name)
        inner_frame = _Frame(
            frozenset({name}), frame.top_prefix, conditions=frame.conditions, outer=frame
        )
        inner = self._inner_task(step, inner_frame, name, None)
        outputs = [
            m.Parameter(name=output, value_from=m.ValueFrom(parameter=_task_output(name, output)))
            for output in step.outputs
        ]
        self.dag_templates[slot] = m.Template(
            name=template_name,
            parallelism=step.loop.max_parallelism,
            inputs=(
                m.Inputs(parameters=[_parameter(key) for key in inner_frame.inputs])
                if inner_frame.inputs
                else None
            ),
            outputs=m.Outputs(parameters=outputs) if outputs else None,
            dag=m.DAGTemplate(tasks=[inner]),
        )
        arguments = [_parameter(key, value) for key, value in inner_frame.inputs.items()]
        return m.DAGTask(
            name=name,
            template=template_name,
            depends=depends,
            arguments=m.Arguments(parameters=arguments) if arguments else None,
        )

    def _block_call(
        self, block: ir.Step, frame: _Frame, item: str, run_prefix: str
    ) -> tuple[str, list[m.Parameter]]:
        assert block.loop is not None
        conditions = frame.conditions
        if block.condition is not None:
            conditions = (*conditions, scope.condition_expression(block.condition))
        task = self.names[block.id]
        name = self.templates.claim(f"{task}-block", block.id)
        slot = self._reserve(name)
        inner = _Frame(
            self._task_names(block.id),
            provided={_loop_input(task): item, "run-prefix": run_prefix},
            conditions=conditions,
            outer=frame,
        )
        tasks = self._tasks(inner, block.id)
        outputs = [
            m.Parameter(
                name=output, value_from=m.ValueFrom(parameter=self._block_output(block, value))
            )
            for output, value in block.block_outputs.items()
        ]
        self.dag_templates[slot] = m.Template(
            name=name,
            inputs=m.Inputs(parameters=[_parameter(key) for key in inner.inputs]),
            outputs=m.Outputs(parameters=outputs) if outputs else None,
            dag=m.DAGTemplate(tasks=tasks),
        )
        return name, [_parameter(key, value) for key, value in inner.inputs.items()]

    def _reserve(self, name: str) -> int:
        """Keep a place for a DAG template, so callers come before the templates they call."""
        self.dag_templates.append(m.Template(name=name))
        return len(self.dag_templates) - 1

    def _block_output(self, block: ir.Step, value: str) -> str:
        source = scope.block_output_source(self.workflow, block, value)
        assert source is not None, "scope.check rejects other block outputs"
        member, output = source
        return _task_output(self.names[member], output)

    def _with_param(self, step: ir.Step, frame: _Frame) -> str:
        assert step.loop is not None
        operand = step.loop.operand
        if isinstance(operand, list):
            text = json.dumps(operand, ensure_ascii=False, separators=(",", ":"))
            if "{{" not in text:
                return text
            return self._unsupported(step, "a literal for_each list holding '{{'")
        match = _PATH.fullmatch(operand)
        parts = match.group(1).split(".") if match else []
        if parts[:1] == ["steps"] and len(parts) == 4 and parts[2] == "outputs":
            return frame.task_output(self.names[parts[1]], parts[3])
        if parts[:1] == ["inputs"] and len(parts) >= 2:
            variable, rest = f"workflow.parameters.{parts[1]}", parts[2:]
            if not rest:
                return f"{{{{{variable}}}}}"
            return _json_path(variable, rest)
        binder = self._loop_binder(step)
        if binder is not None and parts[:1] == [self._loop(binder).variable]:
            block_task = self.names[binder]
            if len(parts) == 1:
                return frame.loop_value(block_task)
            return _json_path(frame.loop_expression(block_task), parts[1:])
        return self._unsupported(
            step,
            "a compiled for_each must be one reference: steps.<id>.outputs.<name>, "
            "inputs.<name>[.<field>...] or <loop variable>[.<field>...]",
        )

    def _loop_binder(self, step: ir.Step) -> str | None:
        """The block whose loop variable the for_each operand of `step` uses, if any."""
        for edge in self.workflow.edges_into(step.id):
            if edge.target == "for_each" and edge.symbol == "loop":
                return edge.binder
        return None

    def _loop(self, step_id: str) -> ir.Loop:
        loop = self.workflow.step(step_id).loop
        assert loop is not None, "a loop reference names a for_each step"
        return loop

    def _unsupported(self, step: ir.Step, message: str) -> str:
        self.errors.append(GlowError(Code.NOT_YET_SUPPORTED, f"{step.id}.for_each", message))
        return "[]"

    def _upstream_value(self, producer_id: str, frame: _Frame) -> str:
        task = self.names[producer_id]
        if task not in frame.tasks:
            return frame.request(
                f"upstream-{task}", lambda outer: self._upstream_value(producer_id, outer)
            )
        producer = self.workflow.step(producer_id)
        if producer.loop is None:
            return frame.task_output(task, RESOLVED_PARAMETER)
        # Argo aggregates each output of a fan-out into a JSON array; glow-exec
        # expects the shape of one outputs.resolved.json.
        fields = ",".join(
            f'"{output}":{frame.task_output(task, output)}' for output in producer.outputs
        )
        return f'{{"outputs":{{{fields}}},"skipped":false}}'

    def _scope(
        self, step: ir.Step, frame: _Frame, needs: scope.Environment, item: str | None
    ) -> str:
        inputs = ",".join(
            f'"{name}":{{{{workflow.parameters.{name}}}}}' for name in self.workflow.inputs
        )
        variables = {"inputs": f"{{{inputs}}}"}
        for binder in needs.loops:
            if binder == step.id:
                assert item is not None, "a step's own loop variable is its item"
                value = item
            else:
                value = frame.loop_value(self.names[binder])
            variables[self._loop(binder).variable] = value
        return "{" + ",".join(f'"{name}":{value}' for name, value in variables.items()) + "}"

    def _lets(self, needs: scope.Environment) -> str:
        bindings = [
            {"name": name, "value": self.workflow.step(binder).let_values[name]}
            for binder, name in needs.lets
        ]
        return encode_json(bindings)

    def _body_arguments(
        self,
        step: ir.Step,
        frame: _Frame,
        needs: scope.Environment,
        item: str | None,
        run_prefix: str,
    ) -> list[m.Parameter]:
        arguments = [
            _parameter("raw-with", encode_json(step.raw_with)),
            _parameter("scope", self._scope(step, frame, needs, item)),
            _parameter("run-prefix", run_prefix),
        ]
        if needs.lets:
            arguments.append(_parameter("let", self._lets(needs)))
        conditions = frame.conditions
        if step.condition is not None:
            conditions = (*conditions, scope.condition_expression(step.condition))
        if conditions:
            text = (
                conditions[0] if len(conditions) == 1 else " && ".join(f"({c})" for c in conditions)
            )
            arguments.append(_parameter("if", text))
        source = step.run if step.run is not None else step.script
        if source is not None:
            arguments.append(_parameter("script", encode_text(source)))
        for producer in needs.steps:
            value = self._upstream_value(producer, frame)
            arguments.append(_parameter(f"upstream-{self.names[producer]}", value))
        return arguments

    def _body_template(self, step: ir.Step, needs: scope.Environment) -> str:
        spec = step.tool_spec
        assert spec is not None
        tool = step.tool
        if tool is not None and tool.builtin:
            base = f"builtin-{argo_name(tool.name)}"
            image = self.options.engine_image
            reference = f"{tool.name}@{BUILTIN_MAJOR}"
            args: list[str] | None = [BUILTIN_COMMAND, tool.name]
        elif tool is not None:
            base = f"tool-{argo_name(tool.name)}"
            image = f"{tool.image}@{tool.digest}" if tool.digest else str(tool.image)
            reference = tool.uses
            args = spec.get("command")
        else:
            kind = "run" if step.run is not None else "script"
            base = f"script-{self.names[step.id]}"
            image = self.options.sandbox_image
            reference = f"{spec['name']}@{SCRIPT_MAJOR}"
            interpreter = _INTERPRETERS[kind]
            decode = _DECODE_SCRIPT.format(interpreter=interpreter)
            args = ["sh", "-c", decode, f"glow-{kind}", _input("script")]
        template = self._container_template(step, image, reference, args, list(needs.steps))
        return self._add_leaf(template, base, step)

    def _add_leaf(self, template: m.Template, base: str, step: ir.Step) -> str:
        """Reuse an identical template, else name this one `base` or `base-<step>`."""
        body = template.model_dump(exclude={"name"})
        for candidate in (base, f"{base}-{self.names[step.id]}"):
            existing = self.leaf_templates.get(candidate)
            if existing is None:
                self.leaf_templates[candidate] = template.model_copy(update={"name": candidate})
                return self.templates.claim(candidate, candidate)
            if existing.model_dump(exclude={"name"}) == body:
                return candidate
        raise AssertionError("each step builds one template, so base-<step> is free")

    def _container_template(
        self,
        step: ir.Step,
        image: str,
        reference: str,
        args: list[str] | None,
        upstream: list[str],
    ) -> m.Template:
        spec = step.tool_spec or {}
        outputs = list(spec.get("outputs", {}))
        if RESOLVED_PARAMETER in outputs:
            message = (
                f"output '{RESOLVED_PARAMETER}' clashes with the parameter "
                "that holds outputs.resolved.json"
            )
            self.errors.append(GlowError(Code.NAME_COLLISION, f"{step.id}.outputs", message))
        upstream_names = [f"upstream-{self.names[producer]}" for producer in upstream]
        is_script = step.tool is None
        inputs = [
            _parameter("raw-with"),
            _parameter("scope"),
            _parameter("let", default=""),
            _parameter("if", default=""),
            _parameter("run-prefix"),
            *([_parameter("script")] if is_script else []),
            *(_parameter(name) for name in upstream_names),
        ]
        env = [
            m.EnvVar(name="GLOW_RAW_WITH", value=_input("raw-with")),
            m.EnvVar(name="GLOW_MANIFEST", value=encode_json(spec)),
            m.EnvVar(name="GLOW_SCOPE", value=_input("scope")),
            m.EnvVar(name="GLOW_LET", value=_input("let")),
            m.EnvVar(name="GLOW_IF", value=_input("if")),
            m.EnvVar(name="GLOW_RUN_PREFIX", value=_input("run-prefix")),
            m.EnvVar(name="GLOW_STAGING", value=step.staging),
            *(
                m.EnvVar(name=f"GLOW_UPSTREAM_{producer}", value=_input(name))
                for producer, name in zip(upstream, upstream_names, strict=True)
            ),
        ]
        volumes = [
            m.Volume(name="glow", empty_dir=m.EmptyDirVolumeSource()),
            m.Volume(
                name="work",
                empty_dir=m.EmptyDirVolumeSource(size_limit=m.Quantity(WORK_SIZE_LIMIT)),
            ),
        ]
        mounts = [
            m.VolumeMount(name="glow", mount_path="/glow", read_only=True),
            m.VolumeMount(name="work", mount_path="/work"),
        ]
        for index, secret in enumerate(step.secrets):
            volume = f"secret-{index}"
            volumes.append(m.Volume(name=volume, secret=m.SecretVolumeSource(secret_name=secret)))
            mounts.append(
                m.VolumeMount(name=volume, mount_path=f"{SECRETS_DIR}/{secret}", read_only=True)
            )
        init = m.UserContainer(
            name="glow-init",
            image=self.options.glow_exec_image,
            command=["/glow-exec", "install", GLOW_EXEC],
            volume_mounts=[m.VolumeMount(name="glow", mount_path="/glow")],
            security_context=_SECURITY_CONTEXT,
        )
        container = m.Container(
            image=image,
            command=[GLOW_EXEC, "run", "--tool", reference, "--"],
            args=args,
            env=env,
            resources=_resources(step.resources),
            security_context=_SECURITY_CONTEXT,
            volume_mounts=mounts,
        )
        output_parameters = [
            m.Parameter(name=RESOLVED_PARAMETER, value_from=m.ValueFrom(path=RESOLVED_PATH)),
            *(
                # A skipped step writes no per-output files.
                m.Parameter(
                    name=output,
                    value_from=m.ValueFrom(path=f"{OUTPUTS_DIR}/{output}.json", default="null"),
                )
                for output in outputs
            ),
        ]
        return m.Template(
            name="",
            inputs=m.Inputs(parameters=inputs),
            outputs=m.Outputs(parameters=output_parameters),
            volumes=volumes,
            init_containers=[init],
            container=container,
            active_deadline_seconds=_timeout(step.timeout),
            retry_strategy=_retry_strategy(step.retries),
        )


def _task_output(task: str, parameter: str) -> str:
    return f"{{{{tasks.{task}.outputs.parameters.{parameter}}}}}"


def _default_value(workflow: ir.Workflow, name: str) -> str | None:
    if name not in workflow.defaults:
        return None
    return json.dumps(workflow.defaults[name], ensure_ascii=False, separators=(",", ":"))


def _json_path(variable: str, fields: list[str]) -> str:
    return f"{{{{=toJson(jsonpath({variable}, '$.{'.'.join(fields)}'))}}}}"


def _is_string(glow_type: GlowType) -> bool:
    return isinstance(glow_type, Scalar) and glow_type.schema.get("type") == "string"


def _item_value(item_type: GlowType) -> str:
    """The loop item as JSON. Argo substitutes a string item without quotes."""
    return "{{=toJson(item)}}" if _is_string(item_type) else "{{item}}"


def _item_segment(item_type: GlowType) -> str:
    """The run prefix segment of one fan-out item."""
    return "{{item.key}}" if isinstance(item_type, Group) else "{{item}}"


def _resources(resources: Resources | None) -> m.ResourceRequirements | None:
    if resources is None:
        return None

    def quantities(spec: ResourceSpec | None) -> dict[str, m.Quantity] | None:
        if spec is None:
            return None
        values = {"cpu": spec.cpu, "memory": spec.memory, GPU_RESOURCE: spec.gpu}
        found = {key: _quantity(value) for key, value in values.items() if value is not None}
        return found or None

    return m.ResourceRequirements(
        requests=quantities(resources.requests), limits=quantities(resources.limits)
    )


def _quantity(value: str | float | int) -> m.Quantity:
    # The workflow model reads `cpu: 4` as 4.0.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return m.Quantity(str(value))


def _timeout(timeout: int | str | None) -> m.IntOrString | None:
    if timeout is None:
        return None
    if isinstance(timeout, int):
        return m.IntOrString(timeout)
    match = _DURATION.fullmatch(timeout)
    assert match is not None, "the workflow model only accepts h, m and s durations"
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return m.IntOrString(hours * 3600 + minutes * 60 + seconds)


def _retry_strategy(retries: int | None) -> m.RetryStrategy | None:
    if retries is None:
        return None
    return m.RetryStrategy(limit=m.IntOrString(str(retries)), retry_policy="OnError")
