"""The `glow` command line."""

from pathlib import Path
from typing import Annotated

import typer

from glow.lint import lint_manifest
from glow.lock import LockError, lock_is_current, lock_path, write_lock
from glow.manifests import ManifestError
from glow.schema_export import schema_dir, stale_schemas, write_schemas
from glow.validation import DocumentReadError, validate_file

app = typer.Typer(no_args_is_help=True, add_completion=False, help="GLOW v2 workflow tools.")
schema_app = typer.Typer(no_args_is_help=True, help="Manage the generated JSON Schemas.")
app.add_typer(schema_app, name="schema")
toolpack_app = typer.Typer(no_args_is_help=True, help="Check toolpack manifests and the lock.")
app.add_typer(toolpack_app, name="toolpack")

DEFAULT_TOOLPACKS = Path("toolpacks")


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


@toolpack_app.command("lint")
def lint(
    manifests: Annotated[list[Path], typer.Argument(help="Toolpack manifest files (YAML).")],
) -> None:
    """Check toolpack manifests. Warnings alone do not fail the command."""
    exit_code = 0
    for manifest in manifests:
        try:
            findings = lint_manifest(manifest)
        except DocumentReadError as exc:
            typer.echo(f"error: {exc}", err=True)
            exit_code = 2
            continue
        for finding in findings:
            typer.echo(f"{manifest}: {finding}", err=True)
        if any(finding.level == "error" for finding in findings):
            exit_code = max(exit_code, 1)
        else:
            typer.echo(f"ok: {manifest}")
    raise typer.Exit(exit_code)


@toolpack_app.command("lock")
def lock(
    check: Annotated[
        bool, typer.Option("--check", help="Fail if the lock does not match the manifests.")
    ] = False,
    root: Annotated[Path, typer.Option(help="Toolpacks directory.")] = DEFAULT_TOOLPACKS,
) -> None:
    """Regenerate registry.lock.yaml from the toolpack manifests."""
    path = lock_path(root)
    try:
        if not check:
            typer.echo(f"wrote {write_lock(root)}")
            return
        current = lock_is_current(root)
    except ManifestError as exc:
        for problem in exc.problems:
            typer.echo(f"error: {exc.path}: {problem}", err=True)
        raise typer.Exit(1) from exc
    except (DocumentReadError, LockError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not current:
        typer.echo(f"error: {path} does not match the manifests", err=True)
        typer.echo("run `glow toolpack lock` and commit the result", err=True)
        raise typer.Exit(1)
    typer.echo(f"ok: {path} is up to date")


def main() -> None:
    app()
