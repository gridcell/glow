"""The `glow` command line."""

from pathlib import Path
from typing import Annotated

import typer

from glow.schema_export import schema_dir, stale_schemas, write_schemas
from glow.validation import DocumentReadError, validate_file

app = typer.Typer(no_args_is_help=True, add_completion=False, help="GLOW v2 workflow tools.")
schema_app = typer.Typer(no_args_is_help=True, help="Manage the generated JSON Schemas.")
app.add_typer(schema_app, name="schema")


@app.command()
def validate(
    file: Annotated[Path, typer.Argument(help="Workflow file or toolpack manifest (YAML).")],
) -> None:
    """Validate a workflow file or toolpack manifest against its JSON Schema."""
    try:
        result = validate_file(file)
    except DocumentReadError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if result.ok:
        typer.echo(f"ok: {file} is a valid {result.kind}")
        return
    for problem in result.problems:
        typer.echo(f"error: {problem}", err=True)
    typer.echo(f"{file}: {len(result.problems)} problem(s) found", err=True)
    raise typer.Exit(1)


@schema_app.command("export")
def export(
    check: Annotated[
        bool, typer.Option("--check", help="Fail if the committed schemas are out of date.")
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option(help="Directory to write to. Defaults to the package schema directory."),
    ] = None,
) -> None:
    """Generate the JSON Schemas from the models."""
    directory = output or schema_dir()
    if check:
        stale = stale_schemas(directory)
        for path in stale:
            typer.echo(f"error: {path} does not match the models", err=True)
        if stale:
            typer.echo("run `glow schema export` and commit the result", err=True)
            raise typer.Exit(1)
        typer.echo(f"ok: schemas in {directory} are up to date")
        return
    for path in write_schemas(directory):
        typer.echo(f"wrote {path}")


def main() -> None:
    app()
