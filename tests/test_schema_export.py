import json
from pathlib import Path

from typer.testing import CliRunner

from glow.cli import app
from glow.schema_export import SCHEMA_MODELS, schema_dir, stale_schemas, write_schemas

runner = CliRunner()


def test_committed_schemas_match_models() -> None:
    assert stale_schemas(schema_dir()) == []


def test_schemas_are_draft_2020_12() -> None:
    for filename in SCHEMA_MODELS:
        schema = json.loads((schema_dir() / filename).read_text())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False


def test_export_check_passes_for_committed_schemas() -> None:
    result = runner.invoke(app, ["schema", "export", "--check"])
    assert result.exit_code == 0, result.output


def test_export_check_fails_when_stale(tmp_path: Path) -> None:
    write_schemas(tmp_path)
    (tmp_path / "workflow.schema.json").write_text("{}\n")
    result = runner.invoke(app, ["schema", "export", "--check", "--output", str(tmp_path)])
    assert result.exit_code == 1
    assert "workflow.schema.json does not match the models" in result.stderr


def test_export_writes_files(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "export", "--output", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert stale_schemas(tmp_path) == []
