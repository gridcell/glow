# glow

GLOW v2 describes geospatial workflows as YAML files that compile to Argo
Workflows. This package holds the data model for workflow files and toolpack
manifests, their JSON Schemas, and the `glow` command line.

The current scope is validation, the toolpack registry, which resolves
`uses:` references to tools and images, and `glow plan`, which prints the
validated graph. The CEL type check of expressions and compilation come later.

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

A workflow that passes the schema check is then checked against the toolpack
registry (`--toolpacks`, default `toolpacks/`):

- every `uses` resolves, every `with` key exists and required inputs are set;
- every `${{ }}` reference names an input, a step output, a loop variable or a
  `let` name that is visible in its scope;
- a step only uses earlier steps, and no steps use each other's outputs;
- the type of each value matches the type the tool input accepts.

These errors name the step and field, carry a stable code, and say how to fix
the problem:

```text
error: cog.with.source [GLOW-E030]
  expects: file[image/tiff; application=geotiff | image/jp2 | application/x-netcdf | application/vnd.gdal.vrt+xml]
  got:     array<group[application/x-netcdf]>  from ${{ steps.items.outputs.groups }}
  hint:    for_each over the array, or .map(...) to extract one value per member
```

| Code | Problem |
| --- | --- |
| `GLOW-E001` | `uses` does not resolve to a tool |
| `GLOW-E002` | `with` key is not an input of the tool |
| `GLOW-E003` | Required input is not set |
| `GLOW-E004` | Media type in the workflow does not parse |
| `GLOW-E005` | `${{` without a closing `}}` |
| `GLOW-E010` | Name is not defined |
| `GLOW-E011` | Reference to a step that runs later |
| `GLOW-E012` | Reference to a member of a `for_each` block from outside it |
| `GLOW-E013` | Reference to an output the step does not declare |
| `GLOW-E020` | Steps use each other's outputs (cycle) |
| `GLOW-E030` | Value type does not match the input type |
| `GLOW-E031` | `for_each` is not over an array |
| `GLOW-E032` | `if` is not a boolean |
| `GLOW-E040` | The toolpack registry cannot be loaded |

Print the validated graph:

```bash
uv run glow plan examples/sst-ingest.yaml
uv run glow plan examples/sst-ingest.yaml --json   # the IR consumed by the compiler
```

The plan lists the steps in dependency order, members indented under their
block, with the staging mode, the resources, and the type of every incoming
edge. An edge that can only be checked when the step runs is marked, so the
author can tighten it:

```text
  cog  gdal.translate@1 (local/gdal:dev)
    staging: copy, resources: default
    with.source: file  <- g.files[0].path  [runtime check: the file has no declared media type]
```

An expression that is not a single reference, such as
`${{ date(g.key, '%Y%m%d') }}`, is checked at runtime until the CEL type
checker is added.

Lint toolpack manifests, and regenerate or check the registry lock:

```bash
uv run glow toolpack lint toolpacks/*/manifest.yaml
uv run glow toolpack lock           # rewrite toolpacks/registry.lock.yaml
uv run glow toolpack lock --check   # exit 1 if the lock is out of date
```

Build the toolpack images and run the wrapper tests inside them (needs
docker):

```bash
make images        # local/gdal:dev, local/stac:dev, local/prescient:dev
make images-test
```

See [docs/toolpacks.md](docs/toolpacks.md) for the toolpack layout,
versioning, the tool contract and the images.

The step runtime, glow-exec, is a Go binary in `glow-exec/`. See
[docs/glow-exec.md](docs/glow-exec.md) for its parameters and behavior.

## Layout

| Path | Content |
| --- | --- |
| `src/glow/models/` | pydantic models for workflows (`workflow.py`) and toolpack manifests (`toolpack.py`) |
| `src/glow/schemas/` | JSON Schemas generated from the models. Do not edit by hand. |
| `src/glow/expressions/` | Finds `${{ ... }}` spans and the references inside them. Expressions are not parsed yet. |
| `src/glow/validate/` | Checks tools, scopes, order and edge types after the schema check |
| `src/glow/ir.py`, `src/glow/plan.py` | The validated workflow IR and its `glow plan` rendering |
| `src/glow/registry.py` | Resolves `uses:` references through the lock file |
| `src/glow/builtins/` | Built-in tools (`fs.group`, `fs.glob`) |
| `examples/` | Example workflows |
| `toolpacks/` | Toolpack manifests, Dockerfiles and wrappers (`gdal`, `stac`, `prescient`), `registry.lock.yaml`, and the image tests in `toolpacks/tests/` |
| `Makefile`, `scripts/images_lock.py` | Build, test, push and pin the toolpack images |
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
