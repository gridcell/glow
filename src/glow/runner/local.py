"""Run the IR of a validated workflow on this machine with Docker (plan section 6.4).

Top-level steps run one after another in IR order, which is dependency
order. A `for_each` runs its items on a thread pool sized by
`max_parallelism`; the members of one item run in order. Built-ins run
in-process. Every other step is one `docker run` of its image with glow-exec
mounted in, given the parameters the compiler would emit (`raw-with`,
`scope`, `if`, `upstream-*`, the manifest), so glow-exec behaves as it does in
a cluster.

The runner accepts what the compiler accepts (`glow.compile.scope.check`).
It evaluates the `let` bindings of a for_each once per item, before its
members, and passes them to glow-exec in the scope. It fans in the way Argo
does: each output of a `for_each` becomes an array
with one entry per item, in item order, `null` where the producing step was
skipped. When a `for_each` has a false `if`, every item is skipped.

Run layout, under `<run prefix>/runs/<run-id>/`:

    run.json                                   workflow, inputs and status
    steps/<step>/outputs.resolved.json         a top-level step
    steps/<step>/log.txt                       its container output
    steps/<step>/<output>/<basename>           files glow-exec uploaded
    steps/<block>/outputs.resolved.json        a for_each, fanned in
    steps/<block>/<item>/<member>/...          one member of one item

`<item>` is the group key or the string item when every item has a distinct,
path-safe one, as in the compiled run prefix, and otherwise the item index.
"""

import concurrent.futures
import copy
import datetime
import itertools
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from glow import ir, storage
from glow.builtins import BuiltinError, run_builtin
from glow.compile import scope as compile_scope
from glow.compile.argo import SCRIPT_MAJOR
from glow.compile.encoding import encode_json
from glow.expressions import (
    Evaluator,
    ExpressionEvalError,
    ExpressionSyntaxError,
    ExpressionTooCostlyError,
)
from glow.runner.docker import (
    S3_ENVIRONMENT,
    WORK,
    ContainerSpec,
    Docker,
    DockerError,
    Mount,
    plan_mounts,
)
from glow.runner.inputs import local_paths
from glow.types import Group
from glow.validate.errors import Code, GlowError

LOCAL_SANDBOX_IMAGE = "local/sandbox:dev"
RESOLVED = "outputs.resolved.json"
LOG = "log.txt"
SKIPPED: dict[str, Any] = {"skipped": True}
# Linux limits one environment string to 128 KiB. Larger scope and upstream
# values go to a file in the work directory, passed with the glow-exec flag.
MAX_ENV_VALUE = 96 * 1024
LOG_TAIL_LINES = 20

_INTERPRETERS = {"run": "bash", "script": "python3"}
_DATA_KINDS = frozenset({"file", "bundle", "group"})
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
_DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")
_EXPRESSION_ERRORS = (ExpressionEvalError, ExpressionSyntaxError, ExpressionTooCostlyError)


class RunError(RuntimeError):
    """Raised when the workflow cannot run locally, before any step starts."""

    def __init__(self, errors: list[GlowError]) -> None:
        super().__init__("\n".join(error.render() for error in errors))
        self.errors = errors


class StepFailedError(RuntimeError):
    """Raised when a step fails. `step` names it, with the item for a fan-out member."""

    def __init__(self, step: str, message: str, log: str | None = None) -> None:
        super().__init__(f"step {step} failed: {message}")
        self.step = step
        self.log = log


class _CancelledError(Exception):
    """An item that did not start because another item failed."""


class Executor(Protocol):
    def run(self, spec: ContainerSpec, name: str, log: Path, timeout: float | None) -> int: ...


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Where and how to run.

    `run_prefix` is an absolute path or an `s3://` URI. `max_parallelism`
    bounds the containers running at once, and the items of a `for_each`
    that sets no limit of its own; the default is the number of CPUs.
    """

    run_prefix: str
    run_id: str | None = None
    keep_work: bool = False
    max_parallelism: int | None = None
    sandbox_image: str = LOCAL_SANDBOX_IMAGE
    work_root: Path | None = None

    def __post_init__(self) -> None:
        if self.run_id is not None and not _RUN_ID.fullmatch(self.run_id):
            raise ValueError(f"run id {self.run_id!r} must be lowercase letters, digits and '-'")
        if self.max_parallelism is not None and self.max_parallelism < 1:
            raise ValueError("max parallelism must be at least 1")
        if not storage.is_s3(self.run_prefix):
            storage.local_path(self.run_prefix)


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    run_uri: str
    outputs: dict[str, dict[str, Any]]
    work_dir: Path | None


@dataclass(slots=True)
class _Context:
    """Where steps of one level run: top level, or one item of a for_each.

    `scope` holds the expression variables other than `steps`; `resolved`
    the outputs.resolved.json of every step visible here.
    """

    scope: dict[str, Any]
    resolved: dict[str, dict[str, Any]]
    uri: str
    work: Path
    label: str = ""
    skip: bool = False

    def child(self, step_id: str) -> tuple[str, Path, str]:
        label = f"{self.label}.{step_id}" if self.label else step_id
        return storage.join(self.uri, step_id), self.work / step_id, label


@dataclass(slots=True)
class _Item:
    index: int
    value: Any
    segment: str
    resolved: dict[str, Any] = field(default_factory=dict)


def run_workflow(
    workflow: ir.Workflow,
    inputs: Mapping[str, Any],
    options: RunOptions,
    executor: Executor | None = None,
) -> RunResult:
    """Run every step. Raises `RunError` before starting, or `StepFailedError`."""
    errors = check(workflow)
    if errors:
        raise RunError(errors)
    return _Runner(workflow, dict(inputs), options, executor).run()


def check(workflow: ir.Workflow) -> list[GlowError]:
    """What the local runner cannot run: what the compiler rejects, and secrets."""
    errors = compile_scope.check(workflow)
    for step in workflow.steps:
        if step.secrets:
            message = "the local runner cannot mount Kubernetes secrets"
            errors.append(GlowError(Code.NOT_YET_SUPPORTED, f"{step.id}.secrets", message))
    return errors


def new_run_id(workflow: ir.Workflow) -> str:
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dt%H%M%S")
    return f"{workflow.name[:80]}-{stamp}-{secrets.token_hex(3)}"


class _Runner:
    def __init__(
        self,
        workflow: ir.Workflow,
        inputs: dict[str, Any],
        options: RunOptions,
        executor: Executor | None,
    ) -> None:
        self.workflow = workflow
        self.inputs = inputs
        self.options = options
        self.run_id = options.run_id or new_run_id(workflow)
        self.run_uri = storage.join(options.run_prefix, "runs", self.run_id)
        self.store = storage.for_uri(self.run_uri)
        self.children: dict[str | None, list[ir.Step]] = defaultdict(list)
        for step in workflow.steps:
            self.children[step.parent].append(step)
        self.parallelism = options.max_parallelism or os.cpu_count() or 1
        self.slots = threading.Semaphore(self.parallelism)
        self.failed = threading.Event()
        self.names = itertools.count()
        self.executor_lock = threading.Lock()
        self.executor = executor
        self.network = storage.is_s3(options.run_prefix) or _mentions_s3(inputs)
        self.mounts: list[Mount] = []

    def run(self) -> RunResult:
        self.mounts = self._mounts()
        work_root = Path(
            tempfile.mkdtemp(prefix=f"glow-{self.run_id}-", dir=self.options.work_root)
        )
        self._write_run("running")
        top = _Context({"inputs": self.inputs}, {}, storage.join(self.run_uri, "steps"), work_root)
        try:
            outputs = self._run_steps(None, top)
        except BaseException:
            self._write_run("failed")
            raise
        finally:
            if not self.options.keep_work:
                shutil.rmtree(work_root, ignore_errors=True)
        self._write_run("succeeded")
        kept = work_root if self.options.keep_work else None
        return RunResult(self.run_id, self.run_uri, outputs, kept)

    def _mounts(self) -> list[Mount]:
        requested = []
        for local in local_paths(self.workflow, self.inputs):
            if local.writable and not local.path.exists():
                # Docker would create a missing mount source owned by root.
                local.path.mkdir(parents=True)
            requested.append(Mount(local.path, local.writable))
        if not storage.is_s3(self.run_uri):
            run_dir = storage.local_path(self.run_uri)
            run_dir.mkdir(parents=True, exist_ok=True)
            requested.append(Mount(run_dir, writable=True))
        try:
            return plan_mounts(requested)
        except DockerError as exc:
            raise RunError([GlowError(Code.NOT_YET_SUPPORTED, "inputs", str(exc))]) from None

    def _write_run(self, status: str) -> None:
        document = {
            "workflow": self.workflow.name,
            "run_id": self.run_id,
            "inputs": self.inputs,
            "status": status,
        }
        self.store.write_text(storage.join(self.run_uri, "run.json"), _dump(document))

    # Scheduling.

    def _run_steps(self, owner: str | None, context: _Context) -> dict[str, dict[str, Any]]:
        """Run the steps of one level in order. Returns their outputs.resolved.json."""
        results = {}
        for step in self.children[owner]:
            if self.failed.is_set():
                raise _CancelledError
            uri, work, label = context.child(step.id)
            if context.skip:
                result = dict(SKIPPED)
            elif step.loop is None:
                result = self._invoke(step, context, uri, work, label)
            else:
                result = self._fan_out(step, context, uri, work, label)
            self._publish(uri, result)
            context.resolved[step.id] = result
            results[step.id] = result
        return results

    def _fan_out(
        self, step: ir.Step, context: _Context, uri: str, work: Path, label: str
    ) -> dict[str, Any]:
        assert step.loop is not None
        evaluator = self._evaluator(step, context)
        skip = context.skip or not self._condition(step, evaluator, label)
        values = self._operand(step, evaluator, label)
        segments = _segments(values, step.loop.item)
        items = [
            _Item(index, value, segment)
            for index, (value, segment) in enumerate(zip(values, segments, strict=True))
        ]
        is_block = step.id in self.children
        variable = step.loop.variable

        def run_item(item: _Item) -> None:
            if self.failed.is_set():
                raise _CancelledError
            inner = _Context(
                {**context.scope, variable: item.value},
                dict(context.resolved),
                storage.join(uri, item.segment),
                work / item.segment,
                f"{label}[{item.segment}]",
                skip,
            )
            if not skip:
                self._bind_lets(step, inner)
            if is_block:
                self._run_steps(step.id, inner)
                item.resolved = self._block_outputs(step, inner)
            else:
                item.resolved = self._invoke(step, inner, inner.uri, inner.work, inner.label)
                self._publish(inner.uri, item.resolved)

        limit = step.loop.max_parallelism or self.parallelism
        self._parallel(run_item, items, limit)
        outputs = {name: [_output(item.resolved, name) for item in items] for name in step.outputs}
        return {"outputs": outputs, "skipped": False}

    def _parallel(self, function: Callable[[_Item], None], items: list[_Item], limit: int) -> None:
        """Run `function` for each item; the first failure cancels the items not started."""
        if not items:
            return
        first: BaseException | None = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(limit, len(items))) as pool:
            futures = [pool.submit(function, item) for item in items]
            for future in concurrent.futures.as_completed(futures):
                error = future.exception() if not future.cancelled() else None
                if error is None or isinstance(error, _CancelledError):
                    continue
                if first is None:
                    first = error
                    self.failed.set()
                    for pending in futures:
                        pending.cancel()
        if first is not None:
            raise first
        if self.failed.is_set():
            raise _CancelledError

    def _block_outputs(self, block: ir.Step, context: _Context) -> dict[str, Any]:
        """One item's block outputs. A skipped member gives null, as in Argo."""
        outputs = {}
        for name, value in block.block_outputs.items():
            source = compile_scope.block_output_source(self.workflow, block, value)
            assert source is not None, "check() rejects other block outputs"
            member, output = source
            outputs[name] = _output(context.resolved[member], output)
        return {"outputs": outputs, "skipped": False}

    # One step invocation.

    def _invoke(
        self, step: ir.Step, context: _Context, uri: str, work: Path, label: str
    ) -> dict[str, Any]:
        if context.skip:
            return dict(SKIPPED)
        tool = step.tool
        if tool is not None and tool.builtin:
            return self._builtin(step, tool.name, context, label)
        return self._container(step, context, uri, work, label)

    def _builtin(self, step: ir.Step, name: str, context: _Context, label: str) -> dict[str, Any]:
        evaluator = self._evaluator(step, context)
        if not self._condition(step, evaluator, label):
            return dict(SKIPPED)
        try:
            values = evaluator.substitute("with", step.raw_with)
            outputs = run_builtin(name, values)
        except (*_EXPRESSION_ERRORS, BuiltinError) as exc:
            raise StepFailedError(label, str(exc)) from None
        return {"outputs": outputs, "skipped": False}

    def _container(
        self, step: ir.Step, context: _Context, uri: str, work: Path, label: str
    ) -> dict[str, Any]:
        spec_json = step.tool_spec
        assert spec_json is not None
        work.mkdir(parents=True, exist_ok=True)
        environment = {
            "GLOW_RAW_WITH": encode_json(step.raw_with),
            "GLOW_MANIFEST": encode_json(spec_json),
            "GLOW_IF": _condition_text(step),
            "GLOW_RUN_PREFIX": uri,
            "GLOW_STAGING": step.staging,
        }
        flags: list[str] = []
        _parameter(environment, flags, work, "GLOW_SCOPE", "scope", "", _compact(context.scope))
        for producer in self._producers(step, context):
            text = _compact(context.resolved[producer])
            variable = f"GLOW_UPSTREAM_{producer}"
            _parameter(environment, flags, work, variable, "upstream", f"{producer}=", text)
        image, tool, command = self._entry(step, spec_json, work)
        spec = ContainerSpec(
            image=image,
            tool=tool,
            command=command,
            work_dir=work,
            environment=environment,
            mounts=self.mounts,
            network=self.network,
            pass_through=S3_ENVIRONMENT if self.network else (),
            flags=flags,
        )
        log = work.parent / f"{work.name}.{LOG}"
        name = f"glow-{self.run_id}-{next(self.names)}"
        with self.slots:
            if self.failed.is_set():
                raise _CancelledError
            try:
                status = self._executor().run(spec, name, log, _timeout(step.timeout))
            except DockerError as exc:
                raise StepFailedError(label, str(exc)) from None
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        self.store.write_text(storage.join(uri, LOG), text)
        if status != 0:
            raise StepFailedError(label, f"exited with status {status}", _tail(text))
        try:
            resolved = json.loads((work / RESOLVED).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StepFailedError(label, f"no readable {RESOLVED}: {exc}", _tail(text)) from None
        if not isinstance(resolved, dict):
            raise StepFailedError(label, f"{RESOLVED} is not a JSON object")
        return resolved

    def _entry(self, step: ir.Step, spec: dict[str, Any], work: Path) -> tuple[str, str, list[str]]:
        """The image, tool reference and command of a step."""
        tool = step.tool
        if tool is not None:
            image = f"{tool.image}@{tool.digest}" if tool.digest else str(tool.image)
            return image, tool.uses, list(spec.get("command") or [])
        kind = "run" if step.run is not None else "script"
        source = step.run if step.run is not None else step.script
        (work / "script").write_text(source or "", encoding="utf-8")
        command = [_INTERPRETERS[kind], f"{WORK}/script"]
        return self.options.sandbox_image, f"{spec['name']}@{SCRIPT_MAJOR}", command

    def _executor(self) -> Executor:
        with self.executor_lock:
            if self.executor is None:
                self.executor = Docker()
            return self.executor

    def _producers(self, step: ir.Step, context: _Context) -> list[str]:
        """Steps whose outputs the step's expressions use, in first-use order."""
        sources = [edge.source_step for edge in self.workflow.edges_into(step.id)]
        candidates = dict.fromkeys([*step.depends_on, *sources])
        return [
            producer
            for producer in candidates
            if producer is not None and producer != step.id and producer in context.resolved
        ]

    # In-process expressions: the block or fan-out `if`, the for_each operand
    # and built-in `with` blocks.

    def _evaluator(self, step: ir.Step, context: _Context) -> Evaluator:
        upstream = {
            producer: context.resolved[producer] for producer in self._producers(step, context)
        }
        return Evaluator(expression_variables(context.scope, upstream))

    def _bind_lets(self, step: ir.Step, context: _Context) -> None:
        """Add the lets of `step` for one item to the scope, each seeing the ones before it."""
        for name, value in step.let_values.items():
            evaluator = self._evaluator(step, context)
            try:
                context.scope[name] = evaluator.substitute(f"let.{name}", value)
            except _EXPRESSION_ERRORS as exc:
                raise StepFailedError(context.label, str(exc)) from None

    def _condition(self, step: ir.Step, evaluator: Evaluator, label: str) -> bool:
        if step.condition is None:
            return True
        try:
            value = evaluator.eval(compile_scope.condition_expression(step.condition))
        except _EXPRESSION_ERRORS as exc:
            raise StepFailedError(label, f"if: {exc}") from None
        if not isinstance(value, bool):
            raise StepFailedError(label, f"if must evaluate to a boolean, got {json.dumps(value)}")
        return value

    def _operand(self, step: ir.Step, evaluator: Evaluator, label: str) -> list[Any]:
        assert step.loop is not None
        try:
            values = evaluator.substitute("for_each", step.loop.operand)
        except _EXPRESSION_ERRORS as exc:
            raise StepFailedError(label, str(exc)) from None
        if not isinstance(values, list):
            message = f"for_each must evaluate to a list, got {_compact(values)}"
            raise StepFailedError(label, message)
        return values

    def _publish(self, uri: str, resolved: dict[str, Any]) -> None:
        self.store.write_text(storage.join(uri, RESOLVED), _dump(resolved))


def expression_variables(
    scope: Mapping[str, Any], upstream: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The variables glow-exec's stage builds: the scope, plus `steps` from the upstream outputs.

    A skipped step has no outputs. A resolved file gets `path` equal to its
    `uri`, so `file.path` works before staging.
    """
    variables = {name: _with_path_alias(copy.deepcopy(value)) for name, value in scope.items()}
    steps = {}
    for producer, resolved in upstream.items():
        skipped = bool(resolved.get("skipped"))
        outputs = {} if skipped else copy.deepcopy(resolved.get("outputs") or {})
        steps[producer] = {"outputs": _with_path_alias(outputs), "skipped": skipped}
    variables["steps"] = steps
    return variables


def _with_path_alias(value: Any) -> Any:
    if isinstance(value, list):
        return [_with_path_alias(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _with_path_alias(item) for key, item in value.items()}
    uri = result.get("uri")
    if isinstance(uri, str) and "path" not in result and result.get("kind") in _DATA_KINDS:
        result["path"] = uri
    return result


def _output(resolved: Mapping[str, Any], name: str) -> Any:
    if resolved.get("skipped"):
        return None
    return (resolved.get("outputs") or {}).get(name)


def _segments(values: Sequence[Any], item_type: Any) -> list[str]:
    """Item directory names: the group key or string item, else the index."""
    if isinstance(item_type, Group):
        names = [value.get("key") if isinstance(value, dict) else None for value in values]
    else:
        names = list(values)
    usable = all(isinstance(name, str) and _SEGMENT.fullmatch(name) for name in names)
    if usable and len(set(names)) == len(names):
        return [str(name) for name in names]
    return [str(index) for index in range(len(values))]


def _condition_text(step: ir.Step) -> str:
    """The `if` as the compiler passes it: the bare CEL text."""
    if step.condition is None:
        return ""
    return compile_scope.condition_expression(step.condition)


def _parameter(
    environment: dict[str, str],
    flags: list[str],
    work: Path,
    variable: str,
    flag: str,
    flag_prefix: str,
    text: str,
) -> None:
    """Pass a JSON parameter in the environment, or in a file when it is too large."""
    if len(text.encode("utf-8")) <= MAX_ENV_VALUE:
        environment[variable] = text
        return
    params = work / "params"
    params.mkdir(exist_ok=True)
    (params / f"{variable}.json").write_text(text, encoding="utf-8")
    flags += [f"--{flag}", f"{flag_prefix}@{WORK}/params/{variable}.json"]


def _mentions_s3(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("s3://")
    if isinstance(value, dict):
        return any(_mentions_s3(item) for item in value.values())
    if isinstance(value, list):
        return any(_mentions_s3(item) for item in value)
    return False


def _timeout(timeout: int | str | None) -> float | None:
    if timeout is None:
        return None
    if isinstance(timeout, int):
        return float(timeout)
    match = _DURATION.fullmatch(timeout)
    assert match is not None, "the workflow model only accepts h, m and s durations"
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
