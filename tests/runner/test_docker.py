"""The `docker run` command line and mount planning, without running docker."""

import os
from pathlib import Path

import pytest

from glow.runner.docker import (
    ContainerSpec,
    Docker,
    DockerError,
    Mount,
    docker_command,
    plan_mounts,
)


def spec(**overrides: object) -> ContainerSpec:
    values: dict = {
        "image": "local/fake:dev",
        "tool": "fake.write@1",
        "command": ["/usr/local/bin/write.sh"],
        "work_dir": Path("/tmp/glow-work/header"),
        "environment": {"GLOW_RAW_WITH": "e30=", "GLOW_SCOPE": '{"inputs":{}}'},
        "mounts": [Mount(Path("/data/in"), writable=False), Mount(Path("/data/run"), True)],
    }
    values.update(overrides)
    return ContainerSpec(**values)


def test_command_shape() -> None:
    args, environment = docker_command("docker", Path("/cache/glow-exec"), spec(), "glow-r-1")
    assert args[:4] == ["docker", "run", "--rm", "--name=glow-r-1"]
    assert f"--user={os.getuid()}:{os.getgid()}" in args
    assert "--network=none" in args
    assert "--cap-drop=ALL" in args
    assert "--mount=type=bind,source=/tmp/glow-work/header,target=/work" in args
    assert "--mount=type=bind,source=/cache/glow-exec,target=/glow/exec,readonly" in args
    assert "--mount=type=bind,source=/data/in,target=/data/in,readonly" in args
    assert "--mount=type=bind,source=/data/run,target=/data/run" in args
    tail = args[args.index("--entrypoint=/glow/exec") :]
    assert tail == [
        "--entrypoint=/glow/exec",
        "local/fake:dev",
        "run",
        "--tool",
        "fake.write@1",
        "--",
        "/usr/local/bin/write.sh",
    ]
    # Values travel in the environment of the docker process, not in argv.
    assert args[args.index("GLOW_SCOPE") - 1 : args.index("GLOW_SCOPE") + 1] == ["-e", "GLOW_SCOPE"]
    assert not any("inputs" in arg for arg in args)
    assert environment["GLOW_SCOPE"] == '{"inputs":{}}'


def test_s3_runs_get_the_network_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    args, environment = docker_command(
        "docker",
        Path("/g"),
        spec(network=True, pass_through=("AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN")),
        "n",
    )
    assert "--network=none" not in args
    assert "AWS_ACCESS_KEY_ID" in args
    assert "AWS_SESSION_TOKEN" not in args
    assert "AKIAEXAMPLE" not in " ".join(args)
    assert environment["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"


def test_flags_come_before_the_command() -> None:
    args, _ = docker_command(
        "docker", Path("/g"), spec(flags=["--scope", "@/work/params/GLOW_SCOPE.json"]), "n"
    )
    assert args[-4:] == [
        "--scope",
        "@/work/params/GLOW_SCOPE.json",
        "--",
        "/usr/local/bin/write.sh",
    ]


def test_plan_mounts_merges_and_sorts() -> None:
    planned = plan_mounts(
        [
            Mount(Path("/data/run/"), writable=False),
            Mount(Path("/data"), writable=False),
            Mount(Path("/data/run"), writable=True),
        ]
    )
    assert planned == [Mount(Path("/data"), False), Mount(Path("/data/run"), True)]


@pytest.mark.parametrize(
    "path", ["/", "/work", "/work/x", "/glow", "/etc", "/usr/local", "/a,b", "/a:b", "relative"]
)
def test_plan_mounts_refuses_unsafe_paths(path: str) -> None:
    with pytest.raises(DockerError, match="cannot mount"):
        plan_mounts([Mount(Path(path), writable=True)])


def test_missing_docker_fails_when_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    executor = Docker()
    with pytest.raises(DockerError, match="not installed"):
        _ = executor.docker


def test_glow_exec_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    binary = tmp_path / "glow-exec"
    binary.write_bytes(b"")
    monkeypatch.setenv("GLOW_EXEC", str(binary))
    assert Docker("docker").glow_exec() == binary
    monkeypatch.setenv("GLOW_EXEC", str(tmp_path / "missing"))
    with pytest.raises(DockerError, match="does not exist"):
        Docker("docker").glow_exec()
