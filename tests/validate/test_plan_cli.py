import json
import re
from pathlib import Path

from typer.testing import CliRunner

from glow.cli import app
from tests.conftest import TOOLPACKS
from tests.validate.conftest import EXAMPLES, FIXTURES

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, [*args, "--toolpacks", str(TOOLPACKS)])


def edge_lines(output: str, step_id: str) -> list[str]:
    """The edge lines printed under one step."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"{step_id}  "))
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    edges = []
    for line in lines[start + 1 :]:
        if len(line) - len(line.lstrip()) != indent:
            break
        if "  <- " in line:
            edges.append(line.strip())
    return edges


def test_plan_lists_steps_in_dependency_order() -> None:
    result = invoke("plan", str(EXAMPLES / "sst-ingest.yaml"))
    assert result.exit_code == 0, result.stderr
    header = re.compile(r"^\s*([A-Za-z_]\w*)  ")
    headers = [m.group(1) for line in result.stdout.splitlines() if (m := header.match(line))]
    assert headers == ["items", "per_item", "cog", "thumb", "render", "item", "publish"]


def test_plan_types_every_edge() -> None:
    result = invoke("plan", str(EXAMPLES / "sst-ingest.yaml"))
    assert result.exit_code == 0
    assert edge_lines(result.stdout, "publish") == [
        "with.collection: string  <- inputs.collection",
        "with.items: array<file[application/geo+json]>  <- steps.per_item.outputs.item",
        "with.dest: string  <- inputs.dest",
    ]
    assert "staging: copy, resources: default" in result.stdout


def test_group_with_media_type_is_static() -> None:
    result = invoke("plan", str(EXAMPLES / "sst-ingest.yaml"))
    assert edge_lines(result.stdout, "cog") == [
        "with.source: file[application/x-netcdf]  <- g.files[0].path",
    ]


def test_computed_expression_is_typed_statically() -> None:
    # date(g.key, ...) is a timestamp, which feeds the string datetime input.
    result = invoke("plan", str(EXAMPLES / "sst-ingest.yaml"))
    assert "with.datetime: string  <- g.key" in edge_lines(result.stdout, "item")
    assert result.stdout.splitlines()[0].endswith("15 edges, 1 checked at runtime")


def test_unknown_function_is_a_runtime_check(tmp_path: Path) -> None:
    path = tmp_path / "w.yaml"
    path.write_text(
        "name: t\ninputs:\n  scene: { type: file }\nsteps:\n"
        "  - id: a\n    uses: gdal.info@1\n"
        "    with: { source: '${{ pick(inputs.scene) }}' }\n"
    )
    result = invoke("plan", str(path))
    assert result.exit_code == 0, result.stderr
    assert edge_lines(result.stdout, "a") == [
        "with.source: file  <- inputs.scene  [runtime check: function 'pick' is typed at runtime]",
    ]


def test_group_without_media_type_is_runtime() -> None:
    result = invoke("plan", str(FIXTURES / "group-without-media-type.yaml"))
    assert result.exit_code == 0, result.stderr
    assert edge_lines(result.stdout, "cog") == [
        "with.source: file  <- g.files[0].path"
        "  [runtime check: the file has no declared media type]",
    ]


def test_plan_shows_resources() -> None:
    result = invoke("plan", str(EXAMPLES / "minimal-if-script.yaml"))
    assert result.exit_code == 0
    assert "resources: requests cpu=1 memory=2Gi, limits cpu=2 memory=4Gi" in result.stdout


def test_plan_json_is_the_ir() -> None:
    result = invoke("plan", str(EXAMPLES / "sst-ingest.yaml"), "--json")
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["name"] == "sst-ingest"
    source = next(e for e in data["edges"] if e["target_step"] == "cog")
    assert source["check"] == "ok"
    assert source["type"]["media_types"] == ["application/x-netcdf"]


def test_plan_fails_on_invalid_workflow() -> None:
    result = invoke("plan", str(FIXTURES / "array-into-file.yaml"))
    assert result.exit_code == 1
    assert "error: cog.with.source [GLOW-E030]" in result.stderr
    assert result.stdout == ""


def test_plan_rejects_manifest() -> None:
    result = invoke("plan", str(TOOLPACKS / "gdal" / "manifest.yaml"))
    assert result.exit_code == 1
    assert "not a workflow" in result.stderr


def test_validate_reports_semantic_errors() -> None:
    result = invoke("validate", str(FIXTURES / "cycle.yaml"))
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "error: first [GLOW-E020]"
    assert result.stderr.splitlines()[-1].endswith("1 problem(s) found")


def test_validate_unreadable_file(tmp_path: Path) -> None:
    result = invoke("validate", str(tmp_path / "missing.yaml"))
    assert result.exit_code == 2


def test_plan_neutralises_control_characters(tmp_path: Path) -> None:
    path = tmp_path / "w.yaml"
    path.write_text(
        "name: t\ninputs:\n  flag: { type: boolean }\n  scene: { type: file }\nsteps:\n"
        "  - id: a\n    uses: gdal.info@1\n"
        "    if: \"${{ inputs.flag && '\\x1b[2J' != '' }}\"\n"
        "    with: { source: '${{ inputs.scene }}' }\n"
    )
    result = invoke("plan", str(path))
    assert result.exit_code == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "condition: ${{ inputs.flag && '?[2J' != '' }}" in result.stdout
