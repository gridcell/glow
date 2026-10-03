"""Fixtures for the local runner tests.

`FakeGlowExec` stands in for docker and glow-exec: it decodes the parameters
the runner passes, evaluates `if` and `with` with the Python evaluator and
fakes each tool in Python, so scheduling, fan-in and the run layout are
tested without Docker.

The Docker tests need docker and a glow-exec binary: `GLOW_EXEC`, or the
`local/glow-exec:dev` image from `make images`. Without them they are
skipped, unless GLOW_RUNNER_TESTS=1 is set, as in CI, where that is an error.
"""

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

import pytest

from glow import ir
from glow.compile.encoding import decode_json
from glow.expressions import Evaluator
from glow.runner.docker import GLOW_EXEC_IMAGE, WORK, ContainerSpec, Docker
from glow.runner.local import SKIPPED, expression_variables
from glow.validate import validate

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake"
FAKE_TOOLPACKS = FAKE / "toolpacks"
FAKE_WORKFLOW = FAKE / "workflow.yaml"
FAKE_IMAGE = "local/fake:dev"
REQUIRED = os.environ.get("GLOW_RUNNER_TESTS") == "1"
BUILD_TIMEOUT = 600


def load_ir(path: Path, toolpacks: Path) -> ir.Workflow:
    report = validate(path, toolpacks)
    assert report.ir is not None, report.messages()
    return report.ir


FakeTool = Callable[[dict[str, Any], str], dict[str, Any]]


class FakeGlowExec:
    """Runs a step the way glow-exec would, with each tool faked in Python.

    `calls` records every container spec. A tool named in `fail` exits 3.
    """

    def __init__(self, fail: frozenset[str] = frozenset()) -> None:
        self.calls: list[ContainerSpec] = []
        self.fail = fail
        self._lock = threading.Lock()

    def run(self, spec: ContainerSpec, name: str, log: Path, timeout: float | None) -> int:
        with self._lock:
            self.calls.append(spec)
        env = dict(spec.environment)
        flags = dict(zip(spec.flags[::2], spec.flags[1::2], strict=True))
        scope = json.loads(_value(env.get("GLOW_SCOPE"), flags.get("--scope"), spec.work_dir))
        upstream = {
            key.removeprefix("GLOW_UPSTREAM_"): json.loads(value)
            for key, value in env.items()
            if key.startswith("GLOW_UPSTREAM_")
        }
        for flag, value in zip(spec.flags[::2], spec.flags[1::2], strict=True):
            if flag == "--upstream":
                producer, _, text = value.partition("=")
                upstream[producer] = json.loads(_value(None, text, spec.work_dir))
        tool = decode_json(env["GLOW_MANIFEST"])
        evaluator = Evaluator(expression_variables(scope, upstream))
        log.write_text(f"fake {tool['name']}\n")
        resolved: dict[str, Any] = dict(SKIPPED)
        if not env["GLOW_IF"] or evaluator.eval(env["GLOW_IF"]):
            if tool["name"] in self.fail:
                log.write_text(f"{tool['name']}: failing on purpose\n")
                return 3
            values = evaluator.substitute("with", decode_json(env["GLOW_RAW_WITH"]))
            try:
                outputs = _fake_tool(tool, values, env["GLOW_RUN_PREFIX"])
            except (TypeError, KeyError, OSError) as exc:
                # glow-exec rejects such inputs before the tool starts.
                log.write_text(f"glow-exec: error: {exc!r}\n")
                return 1
            resolved = {"outputs": outputs, "skipped": False}
        (spec.work_dir / "outputs.resolved.json").write_text(json.dumps(resolved))
        return 0


def _value(env_value: str | None, flag: str | None, work: Path) -> str:
    if flag is None:
        return env_value or "{}"
    assert flag.startswith(f"@{WORK}/"), flag
    return (work / flag.removeprefix(f"@{WORK}/")).read_text()


def _write(prefix: str, output: str, name: str, text: str) -> dict[str, Any]:
    path = Path(prefix) / output / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return {"uri": str(path), "media_type": "text/plain", "kind": "file"}


def _uri(value: Any) -> str:
    return value["uri"] if isinstance(value, dict) else value


def _fake_tool(tool: Mapping[str, Any], values: dict[str, Any], prefix: str) -> dict[str, Any]:
    name = tool["name"]
    if name == "fake.write":
        return {"result": _write(prefix, "result", f"{values['name']}.txt", values["text"] + "\n")}
    if name == "fake.copy":
        source = Path(_uri(values["source"]))
        return {"result": _write(prefix, "result", source.name, source.read_text())}
    if name == "fake.concat":
        text = "".join(Path(_uri(part)).read_text() for part in values["parts"])
        return {
            "result": _write(prefix, "result", "joined.txt", text),
            "count": len(values["parts"]),
        }
    # Any other tool: an empty file for each file output, a zero value otherwise.
    outputs: dict[str, Any] = {}
    for output, declaration in tool.get("outputs", {}).items():
        kind = declaration.get("type")
        if kind == "file":
            outputs[output] = _write(prefix, output, f"{output}.out", "")
        else:
            outputs[output] = {"integer": 0, "number": 0, "array": []}.get(kind, {})
    return outputs


def unavailable(reason: str) -> NoReturn:
    if REQUIRED:
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def docker() -> Docker:
    """A Docker executor with a glow-exec binary."""
    executable = shutil.which("docker")
    if executable is None:
        unavailable("docker is not installed")
    configured = os.environ.get("GLOW_EXEC")
    if configured is None:
        result = subprocess.run(
            [executable, "image", "inspect", GLOW_EXEC_IMAGE],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode != 0:
            unavailable(f"{GLOW_EXEC_IMAGE} is missing and GLOW_EXEC is not set; run make images")
    executor = Docker(executable)
    executor.glow_exec()
    return executor


@pytest.fixture(scope="session")
def fake_image(docker: Docker) -> str:
    """Build the fake toolpack image."""
    result = subprocess.run(
        [docker.docker, "build", "-t", FAKE_IMAGE, str(FAKE_TOOLPACKS / "fake")],
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT,
    )
    assert result.returncode == 0, result.stderr
    return FAKE_IMAGE


RUN_PLACEHOLDER = "$RUN"


def resolved_tree(run_dir: Path) -> dict[str, Any]:
    """Every outputs.resolved.json under `run_dir/steps`, by relative path, with the
    run directory replaced by `$RUN` so that runs compare across machines."""
    steps = run_dir / "steps"
    tree = {}
    for path in sorted(steps.rglob("outputs.resolved.json")):
        text = path.read_text().replace(str(run_dir), RUN_PLACEHOLDER)
        tree[path.parent.relative_to(steps).as_posix()] = json.loads(text)
    return tree
