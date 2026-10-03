"""`docker run` for one step, with the same pod shape the compiler emits.

The container gets the step's work directory at `/work`, the glow-exec
binary at `/glow/exec` (read-only) and the step parameters as environment
variables. Host paths that workflow inputs name and the local run directory
are bind-mounted at the same absolute path, so the URIs in
`outputs.resolved.json` are valid inside and outside containers alike.

Parameter values never appear on the docker command line: each `-e NAME`
takes its value from the environment of the docker process, so large values
avoid the argument length limit and stay out of the process list. AWS
credentials reach glow-exec the same way, and only when an `s3://` URI is in
play; otherwise the container has no network.
"""

import os
import shutil
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from glow.compile.argo import GLOW_EXEC

WORK = "/work"
GLOW_EXEC_IMAGE = "local/glow-exec:dev"
# The binary's path in the glow-exec image.
GLOW_EXEC_IN_IMAGE = "/glow-exec"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "glow"
DOCKER_TIMEOUT = 120

# glow-exec reads these for S3; they are passed only when S3 is in play.
S3_ENVIRONMENT = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL",
    "AWS_ENDPOINT_URL_S3",
    "GLOW_S3_PATH_STYLE",
)

# Mounting over these would break the container or the step contract.
_RESERVED = ("/work", "/glow", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib")
# `--mount` separates fields with commas and `-v` with colons.
_UNSAFE_PATH_CHARACTERS = frozenset(",:\n\r\0")


class DockerError(RuntimeError):
    """Raised when docker is missing or a docker command fails."""


@dataclass(frozen=True, slots=True)
class Mount:
    path: Path
    writable: bool


def plan_mounts(mounts: Iterable[Mount]) -> list[Mount]:
    """One mount per path, writable if any request is, sorted so parents come first.

    Raises `DockerError` for a path that cannot be mounted safely.
    """
    merged: dict[Path, bool] = {}
    for mount in mounts:
        path = Path(os.path.normpath(mount.path))
        check_mount_path(path)
        merged[path] = merged.get(path, False) or mount.writable
    return [Mount(path, writable) for path, writable in sorted(merged.items())]


def check_mount_path(path: Path) -> None:
    text = str(path)
    if not path.is_absolute():
        raise DockerError(f"cannot mount {text}: not an absolute path")
    if _UNSAFE_PATH_CHARACTERS & set(text):
        raise DockerError(f"cannot mount {text}: the path contains ',' or ':'")
    if text == "/" or any(text == root or text.startswith(root + "/") for root in _RESERVED):
        raise DockerError(f"cannot mount {text}: it would hide a directory the container needs")


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """Everything one step container needs."""

    image: str
    tool: str
    command: Sequence[str]
    work_dir: Path
    environment: Mapping[str, str]
    mounts: Sequence[Mount] = ()
    network: bool = False
    pass_through: Sequence[str] = ()
    flags: Sequence[str] = ()


def docker_command(
    docker: str, glow_exec: Path, spec: ContainerSpec, name: str
) -> tuple[list[str], dict[str, str]]:
    """The `docker run` arguments and the environment to run them with."""
    args = [
        docker,
        "run",
        "--rm",
        f"--name={name}",
        f"--user={os.getuid()}:{os.getgid()}",
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
        f"--mount=type=bind,source={spec.work_dir},target={WORK}",
        f"--mount=type=bind,source={glow_exec},target={GLOW_EXEC},readonly",
    ]
    if not spec.network:
        args.append("--network=none")
    for mount in spec.mounts:
        readonly = "" if mount.writable else ",readonly"
        args.append(f"--mount=type=bind,source={mount.path},target={mount.path}{readonly}")
    environment = dict(os.environ)
    for key in spec.pass_through:
        if key in os.environ:
            args += ["-e", key]
    for key, value in spec.environment.items():
        args += ["-e", key]
        environment[key] = value
    args += [
        f"--entrypoint={GLOW_EXEC}",
        spec.image,
        "run",
        "--tool",
        spec.tool,
        *spec.flags,
        "--",
        *spec.command,
    ]
    return args, environment


class Docker:
    """Runs step containers. Safe to use from several threads."""

    def __init__(self, docker: str | None = None, glow_exec: Path | None = None) -> None:
        # Looked up now, but only required once a container runs, so that a
        # workflow of built-ins alone runs without docker.
        self._docker = docker or shutil.which("docker")
        self._glow_exec = glow_exec
        self._lock = threading.Lock()

    @property
    def docker(self) -> str:
        if self._docker is None:
            raise DockerError("docker is not installed or not on PATH")
        return self._docker

    def glow_exec(self) -> Path:
        """The glow-exec binary: given, from `GLOW_EXEC`, or extracted from its image once."""
        with self._lock:
            if self._glow_exec is None:
                configured = os.environ.get("GLOW_EXEC")
                self._glow_exec = Path(configured) if configured else self._extract()
            if not self._glow_exec.is_file():
                raise DockerError(f"glow-exec binary {self._glow_exec} does not exist")
            return self._glow_exec.resolve()

    def run(self, spec: ContainerSpec, name: str, log: Path, timeout: float | None) -> int:
        """Run one step container, writing its output to `log`. Returns the exit status."""
        args, environment = docker_command(self.docker, self.glow_exec(), spec, name)
        with log.open("wb") as output:
            process = subprocess.Popen(
                args, stdout=output, stderr=subprocess.STDOUT, env=environment
            )
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Killing the client would leave the container running.
                self._quiet(["kill", name])
                process.wait()
                output.write(f"\nglow: step timed out after {timeout:g}s\n".encode())
                return 124
            except BaseException:
                self._quiet(["kill", name])
                process.wait()
                raise

    def _extract(self) -> Path:
        image_id = self._output(["image", "inspect", "--format", "{{.Id}}", GLOW_EXEC_IMAGE])
        if image_id is None:
            raise DockerError(
                f"no glow-exec binary: build {GLOW_EXEC_IMAGE} with `make images`, "
                "or pass --glow-exec or set GLOW_EXEC"
            )
        target = CACHE_DIR / f"glow-exec-{image_id.removeprefix('sha256:')[:16]}"
        if target.is_file():
            return target
        container = self._output(["create", GLOW_EXEC_IMAGE])
        if container is None:
            raise DockerError(f"cannot create a container from {GLOW_EXEC_IMAGE}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".part")
            copied = self._output(["cp", f"{container}:{GLOW_EXEC_IN_IMAGE}", str(partial)])
            if copied is None:
                raise DockerError(f"cannot copy {GLOW_EXEC_IN_IMAGE} out of {GLOW_EXEC_IMAGE}")
            partial.chmod(0o755)
            partial.replace(target)
        finally:
            self._quiet(["rm", container])
        return target

    def _output(self, args: list[str]) -> str | None:
        result = subprocess.run(
            [self.docker, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCKER_TIMEOUT,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def _quiet(self, args: list[str]) -> None:
        subprocess.run(
            [self.docker, *args], capture_output=True, check=False, timeout=DOCKER_TIMEOUT
        )
