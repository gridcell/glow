# glow

GLOW v2 describes geospatial workflows as YAML files that compile to Argo
Workflows. This package holds the data model for workflow files and toolpack
manifests, their JSON Schemas, and the `glow` command line.

The current scope is schema-level validation and the toolpack registry, which
resolves `uses:` references to tools and images. Expression type checks and
compilation come later.

## Setup

Install [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync
```

## Usage

Validate a workflow file or a toolpack manifest:

```bash
uv run glow validate examples/sst-ingest.yaml
```

A file with a top-level `toolpack` key is checked as a toolpack manifest.
Any other file is checked as a workflow. On success the command prints one
line and exits 0. On failure it prints one line per problem with the JSON
path of the problem and exits 1:

```text
error: $.schedule: unknown property
error: $.steps[0].id: required property is missing
error: $.steps[1]: step must set exactly one of uses, run, script, for_each (found: uses, run)
```

The command exits 2 when it cannot read the file. Files larger than 1 MiB are
rejected. YAML aliases, duplicate keys and non-string keys are errors.

Lint toolpack manifests, and regenerate or check the registry lock:

```bash
uv run glow toolpack lint toolpacks/*/manifest.yaml
uv run glow toolpack lock           # rewrite toolpacks/registry.lock.yaml
uv run glow toolpack lock --check   # exit 1 if the lock is out of date
```

See [docs/toolpacks.md](docs/toolpacks.md) for the toolpack layout,
versioning and the tool contract.

The step runtime, glow-exec, is a Go binary in `glow-exec/`. See
[docs/glow-exec.md](docs/glow-exec.md) for its parameters and behavior.

## Layout

| Path | Content |
| --- | --- |
| `src/glow/models/` | pydantic models for workflows (`workflow.py`) and toolpack manifests (`toolpack.py`) |
| `src/glow/schemas/` | JSON Schemas generated from the models. Do not edit by hand. |
| `src/glow/expressions/` | Helper that finds `${{ ... }}` spans. Expressions are not parsed yet. |
| `src/glow/registry.py` | Resolves `uses:` references through the lock file |
| `src/glow/builtins/` | Built-in tools (`fs.group`, `fs.glob`) |
| `examples/` | Example workflows |
| `toolpacks/` | Toolpack manifests (`gdal`, `stac`, `prescient`) and `registry.lock.yaml` |
| `docs/decisions.md` | Adopted design decisions |
| `docs/toolpacks.md` | Toolpack layout, naming, versioning and tool contract |
| `docs/glow-exec.md` | The glow-exec step runtime |
| `glow-exec/` | The glow-exec Go module |
| `tests/fixtures/expressions/` | Expression cases shared by the Go and Python evaluators |

## Workflow syntax

A workflow has `name`, `inputs` and `steps`. Each step has an `id` and one
body:

- `uses`: a tool reference, `name@major` (built-ins such as `fs.group` have no version).
- `run`: an inline bash script. It must declare typed `outputs`.
- `script`: an inline Python script. It must declare typed `outputs`.
- `for_each` with `as`: a fan-out. The body is nested `steps` with block
  `outputs` (names mapped to expressions), or one inline `uses`, `run` or
  `script`.

Optional step keys are `with`, `if`, `let`, `resources`, `timeout`,
`retries`, `staging` (`copy`, `none` or `auto`), `secrets`, and
`max_parallelism` (only on `for_each`). Step ids must be unique in the file
and must be valid identifiers (letters, digits and `_`), because expressions
refer to them as `steps.<id>`.

## Development

```bash
uv run ruff check
uv run ruff format --check
uv run pytest
uv run glow schema export --check   # committed schemas match the models
```

After you change a model, regenerate the schemas and commit them:

```bash
uv run glow schema export
```
