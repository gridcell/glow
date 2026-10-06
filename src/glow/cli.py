"""The `glow` command line."""

import os
from pathlib import Path
from typing import Annotated

import typer

from glow.builtins import BuiltinError, run_in_work_dir
from glow.compile import CompileError, CompileOptions, compile_workflow, to_yaml
from glow.compile.argo import (
    DEFAULT_ENGINE_IMAGE,
    DEFAULT_GLOW_EXEC_IMAGE,
    DEFAULT_RUN_PREFIX,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SERVICE_ACCOUNT,
)
from glow.lint import lint_manifest
from glow.lock import LockError, lock_is_current, lock_path, write_lock
from glow.manifests import ManifestError
from glow.plan import render_plan
from glow.runner import (
    LOCAL_SANDBOX_IMAGE,
    InputError,
    RunError,
    RunOptions,
    StepFailedError,
    parse_assignments,
    resolve_inputs,
    run_workflow,
)
from glow.runner.docker import Docker
from glow.schema_export import schema_dir, stale_schemas, write_schemas
from glow.validate import DEFAULT_TOOLPACKS, ValidationReport
from glow.validate import validate as validate_path
from glow.validation import DocumentReadError

app = typer.Typer(no_args_is_help=True, add_completion=False, help="GLOW v2 workflow tools.")
schema_app = typer.Typer(no_args_is_help=True, help="Manage the generated JSON Schemas.")
app.add_typer(schema_app, name="schema")
toolpack_app = typer.Typer(no_args_is_help=True, help="Check toolpack manifests and the lock.")
app.add_typer(toolpack_app, name="toolpack")


DEFAULT_LOCAL_RUN_PREFIX = ".glow"

ToolpacksOption = Annotated[
    Path, typer.Option(help="Toolpacks directory holding registry.lock.yaml.")
]


def _report(file: Path, toolpacks: Path) -> ValidationReport:
    try:
        report = validate_path(file, toolpacks)
    except DocumentReadError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    if not report.ok:
        for message in report.messages():
            typer.echo(message, err=True)
        count = len(report.problems) + len(report.errors)
        typer.echo(f"{file}: {count} problem(s) found", err=True)
        raise typer.Exit(1)
    return report


@app.command()
def validate(
    file: Annotated[Path, typer.Argument(help="Workflow file or toolpack manifest (YAML).")],
    toolpacks: ToolpacksOption = DEFAULT_TOOLPACKS,
) -> None:
    """Validate a workflow file or toolpack manifest.

    Workflows are checked against the JSON Schema, then for tools, references,
    scopes, cycles and edge types. Manifests get the schema check only.
    """
    report = _report(file, toolpacks)
    typer.echo(f"ok: {file} is a valid {report.kind}")


@app.command()
def plan(
    file: Annotated[Path, typer.Argument(help="Workflow file (YAML).")],
    toolpacks: ToolpacksOption = DEFAULT_TOOLPACKS,
    as_json: Annotated[bool, typer.Option("--json", help="Print the IR as JSON.")] = False,
) -> None:
    """Validate a workflow and print its steps in dependency order with edge types."""
    report = _report(file, toolpacks)
    if report.ir is None:
        typer.echo(f"error: {file} is a {report.kind}, not a workflow", err=True)
        raise typer.Exit(1)
    typer.echo(report.ir.model_dump_json(indent=2) if as_json else render_plan(report.ir))


@app.command()
def compile(
    file: Annotated[Path, typer.Argument(help="Workflow file (YAML).")],
    output: Annotated[
        Path | None, typer.Option("-o", "--output", help="Write the YAML here, not to stdout.")
    ] = None,
    toolpacks: ToolpacksOption = DEFAULT_TOOLPACKS,
    namespace: Annotated[str | None, typer.Option(help="Namespace of the Workflow.")] = None,
    service_account: Annotated[
        str, typer.Option(help="Service account the step pods run as.")
    ] = DEFAULT_SERVICE_ACCOUNT,
    run_prefix: Annotated[
        str, typer.Option(help="Where step outputs go: s3:// URI, file:// URI or absolute path.")
    ] = DEFAULT_RUN_PREFIX,
    glow_exec_image: Annotated[
        str, typer.Option(help="Image holding the glow-exec binary.")
    ] = DEFAULT_GLOW_EXEC_IMAGE,
    engine_image: Annotated[
        str, typer.Option(help="Image that runs built-in steps.")
    ] = DEFAULT_ENGINE_IMAGE,
    sandbox_image: Annotated[
        str, typer.Option(help="Image that runs run and script steps.")
    ] = DEFAULT_SANDBOX_IMAGE,
    allow_local_images: Annotated[
        bool,
        typer.Option(help="Accept toolpack images without a digest (local development)."),
    ] = False,
) -> None:
    """Validate a workflow and compile it to an Argo Workflow."""
    try:
        options = CompileOptions(
            namespace=namespace,
            service_account=service_account,
            run_prefix=run_prefix,
            glow_exec_image=glow_exec_image,
            engine_image=engine_image,
            sandbox_image=sandbox_image,
            allow_local_images=allow_local_images,
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    report = _report(file, toolpacks)
    if report.ir is None:
        typer.echo(f"error: {file} is a {report.kind}, not a workflow", err=True)
        raise typer.Exit(1)
    try:
        text = to_yaml(compile_workflow(report.ir, options))
    except CompileError as exc:
        for error in exc.errors:
            typer.echo(error.render(), err=True)
        typer.echo(f"{file}: {len(exc.errors)} problem(s) found", err=True)
        raise typer.Exit(1) from exc
    if output is None:
        typer.echo(text, nl=False)
        return
    output.write_text(text)
    typer.echo(f"wrote {output}")


@app.command()
def run(
    file: Annotated[Path, typer.Argument(help="Workflow file (YAML).")],
    inputs: Annotated[
        list[str] | None,
        typer.Option("--input", "-i", help="Workflow input as name=value. Repeat for each input."),
    ] = None,
    run_prefix: Annotated[
        str,
        typer.Option(help="Where run data goes: a local directory, file:// URI or s3:// URI."),
    ] = DEFAULT_LOCAL_RUN_PREFIX,
    toolpacks: ToolpacksOption = DEFAULT_TOOLPACKS,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and print the plan; run nothing.")
    ] = False,
    keep_work: Annotated[
        bool, typer.Option("--keep-work", help="Keep each step's work directory.")
    ] = False,
    glow_exec: Annotated[
        Path | None,
        typer.Option(
            help="glow-exec binary for Linux. Defaults to $GLOW_EXEC, else local/glow-exec:dev."
        ),
    ] = None,
    sandbox_image: Annotated[
        str, typer.Option(help="Image that runs run and script steps.")
    ] = LOCAL_SANDBOX_IMAGE,
    max_parallelism: Annotated[
        int | None,
        typer.Option(min=1, help="Most containers at once. Defaults to the number of CPUs."),
    ] = None,
) -> None:
    """Run a workflow on this machine with Docker.

    Built-ins run in-process; every other step runs in its image under
    glow-exec. Each step's outputs.resolved.json is kept under
    <run prefix>/runs/<run-id>/steps/.
    """
    report = _report(file, toolpacks)
    if report.ir is None:
        typer.echo(f"error: {file} is a {report.kind}, not a workflow", err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.echo(render_plan(report.ir))
        return
    try:
        values = resolve_inputs(report.ir, parse_assignments(inputs or []))
        options = RunOptions(
            run_prefix=_run_prefix(run_prefix),
            keep_work=keep_work,
            max_parallelism=max_parallelism,
            sandbox_image=sandbox_image,
        )
    except (InputError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc
    try:
        result = run_workflow(report.ir, values, options, Docker(glow_exec=glow_exec))
    except RunError as exc:
        for error in exc.errors:
            typer.echo(error.render(), err=True)
        typer.echo(f"{file}: {len(exc.errors)} problem(s) found", err=True)
        raise typer.Exit(1) from exc
    except StepFailedError as exc:
        typer.echo(f"error: {exc}", err=True)
        if exc.log:
            typer.echo(exc.log, err=True)
        raise typer.Exit(1) from exc
    for step_id, resolved in result.outputs.items():
        status = "skipped" if resolved.get("skipped") else "ok"
        typer.echo(f"{status}: {step_id}")
    if result.work_dir is not None:
        typer.echo(f"work directories kept in {result.work_dir}")
    typer.echo(f"run {result.run_id} succeeded; outputs under {result.run_uri}")


def _run_prefix(value: str) -> str:
    """An s3:// URI as it is, a local directory as an absolute path."""
    if value.startswith("s3://"):
        return value.rstrip("/")
    if "://" in value and not value.startswith("file://"):
        raise ValueError(f"run prefix {value!r} must be a directory, file:// URI or s3:// URI")
    return os.path.abspath(value.removeprefix("file://"))


@app.command()
def builtin(
    name: Annotated[str, typer.Argument(help="Built-in tool, such as fs.group.")],
) -> None:
    """Run a built-in under glow-exec: read inputs.json, write outputs.json.

    The engine image runs this in a cluster. The work directory is
    $GLOW_WORK_DIR, /work by default.
    """
    try:
        run_in_work_dir(name)
    except BuiltinError as exc:
        typer.echo(f"error: {name}: {exc}", err=True)
        raise typer.Exit(1) from exc


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


def builtin_main() -> None:
    """`glow-builtin <name>`, the command the compiler gives built-in steps."""
    typer.run(builtin)
