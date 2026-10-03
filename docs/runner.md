# Local runner

`glow run` runs a workflow on one machine with Docker. It uses the same
staging contract as a cluster run: every tool step runs under glow-exec with
the parameters the compiler emits, so a workflow that runs locally runs the
same way in Argo.

```bash
uv run glow run examples/sst-ingest.yaml \
  --input source=tests/fixtures/data/sst \
  --input dest=/tmp/sst-out \
  --input collection=noaa-sst \
  --input color_table=tests/fixtures/data/sst/colors.txt
```

## Requirements

- Docker.
- A glow-exec binary for Linux. The runner uses, in order: `--glow-exec PATH`,
  the `GLOW_EXEC` environment variable, or the binary in the
  `local/glow-exec:dev` image, which it copies once to `~/.cache/glow/`.
- The images the workflow uses. `make images` builds the toolpack images,
  `local/glow-exec:dev`, `local/engine:dev` and `local/sandbox:dev`.

A workflow of built-ins alone does not need Docker.

## Options

| Option | Effect |
| --- | --- |
| `--input name=value`, `-i` | One workflow input. Repeat for each input. |
| `--run-prefix` | Where run data goes: a directory (default `.glow`), `file://` URI or `s3://` URI. |
| `--dry-run` | Validate and print the plan, as `glow plan` does. Run nothing. |
| `--keep-work` | Keep the work directory of each step, and print where it is. |
| `--glow-exec` | The glow-exec binary. |
| `--sandbox-image` | The image for `run` and `script` steps. Default `local/sandbox:dev`. |
| `--max-parallelism` | The most containers that run at once. Default: the number of CPUs. |
| `--toolpacks` | The directory that holds `registry.lock.yaml`. |

## Input values

Each value is read according to the declared input type:

- `string`: the text as it is.
- `uri`: an `s3://` URI as it is. A local path or `file://` URI becomes an
  absolute path.
- `file`, `bundle`: a local path, `file://` or `s3://` URI. The value becomes
  a resolved map `{uri, media_type, kind}` with the declared media type, so
  glow-exec checks it when it stages the file. A local file or directory must
  exist.
- `integer`, `number`, `boolean`, `array`, `object`: JSON text, for example
  `--input names='["a","b"]'`.

Inputs that you do not give take their default. A missing required input, an
unknown name or a value of the wrong type stops the run before any step
starts.

## How steps run

- Top-level steps run one after another, in dependency order.
- A built-in (`fs.group`, `fs.glob`) runs in the `glow` process.
- Every other step is one `docker run` of the step image. The work directory
  is mounted at `/work` and glow-exec at `/glow/exec`. The container runs
  `/glow/exec run --tool <tool> -- <command>` as your user, with no
  capabilities. A `run` step runs `bash /work/script` and a `script` step runs
  `python3 /work/script` in the sandbox image.
- The parameters are environment variables: `GLOW_RAW_WITH`,
  `GLOW_MANIFEST`, `GLOW_SCOPE`, `GLOW_IF`, `GLOW_UPSTREAM_<step>`,
  `GLOW_RUN_PREFIX` and `GLOW_STAGING`. A scope or upstream value larger than
  96 KiB goes to a file in `/work/params/` and is passed with the glow-exec
  flag instead.
- A `for_each` runs its items on a thread pool. The pool size is the step's
  `max_parallelism`, or else `--max-parallelism`. The members of one item run
  in order.
- The first failed step stops the run. Items that have not started do not
  start.

The runner accepts what the compiler accepts. A construct that fails
`glow compile` with `GLOW-E050` also fails `glow run`. Steps with `secrets`
are refused, because there is no Kubernetes secret to mount. `resources` and
`retries` have no effect locally. `timeout` stops the container.

### Fan-in

Fan-in follows Argo. Each output of a `for_each` becomes an array with one
entry per item, in item order. The entry is `null` where the step that
produces it was skipped. When a `for_each` has a false `if`, every item is
skipped and each output is an array of `null`. The `outputs.resolved.json` of
a `for_each` is `{"outputs": {<name>: [...]}, "skipped": false}`.

## Mounts and network

Paths keep their absolute path inside the containers, so the URIs in
`outputs.resolved.json` are valid inside and outside them:

- the run directory, `<run prefix>/runs/<run-id>/`, is mounted read-write;
- `file` and `bundle` inputs are mounted read-only;
- `uri` inputs are mounted read-write, because a `uri` can be a destination
  such as `dest`. A `uri` path that does not exist is created as a directory.

A path that contains `,` or `:`, or that would hide a system directory such
as `/usr` or `/work`, is refused.

Containers have no network unless the run prefix or an input is an `s3://`
URI. Then glow-exec gets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `AWS_ENDPOINT_URL`,
`AWS_ENDPOINT_URL_S3` and `GLOW_S3_PATH_STYLE` from your environment. The
values do not appear on the docker command line. Profiles in `~/.aws/` are
not mounted, so export the credentials. For S3 from the `glow` process
(built-ins, run records), install the `s3` extra: `uv sync --extra s3`.

## Run layout

Under `<run prefix>/runs/<run-id>/`:

```text
run.json                               workflow, inputs and status
steps/<step>/outputs.resolved.json     a top-level step
steps/<step>/log.txt                   its container output
steps/<step>/<output>/<basename>       the files glow-exec uploaded
steps/<for_each>/outputs.resolved.json the fanned-in outputs
steps/<for_each>/<item>/<member>/...   one member of one item
```

`<item>` is the group key, or the string item, when every item has a
different one that is safe as a path segment. Otherwise it is the item index.
The run id is `<workflow>-<UTC time>-<random>`.

Every step keeps its `outputs.resolved.json`, so you can inspect a failed run
edge by edge. The work directories are temporary and are removed after the
run, unless you pass `--keep-work`.

## Built-ins in a cluster

The compiler runs a built-in in the engine image as
`/glow/exec run --tool <name>@1 -- glow-builtin <name>`. `glow-builtin`, the
same as `glow builtin <name>`, reads `/work/inputs.json` and writes
`/work/outputs.json`. glow-exec then collects the outputs.

`fs.group` lists every file under `root`, recursively, and matches its path
relative to `root` with `pattern` (Python `re.search`). It groups the
matching files by the `key` capture. Groups are sorted by key and files by
path. Each file is `{uri, kind, media_type}`, and the URI keeps the original
basename. `fs.glob` matches with a glob, where `**` crosses directories.
Symbolic links are skipped.

## Tests

```bash
uv run pytest tests/runner                    # fake glow-exec; Docker tests skip
GLOW_RUNNER_TESTS=1 uv run pytest tests/runner -m "not integration"   # needs docker and local/glow-exec:dev
GLOW_IMAGE_TESTS=1 uv run pytest tests/runner -m integration          # needs make images
UPDATE_BASELINE=1 uv run pytest tests/runner/test_fake_end_to_end.py  # refresh the baseline
```

`tests/runner/fake/` holds a busybox toolpack and a workflow with a top-level
step, a `for_each` block with two items, fan-in into a consumer and a skipped
step. `tests/runner/fake/baseline.json` records every `outputs.resolved.json`
of that run, with the run directory written as `$RUN`. A cluster run of the
same workflow is compared against it.

The integration test writes two netCDF files into
`tests/fixtures/data/sst/` with the gdal image. To create them for a manual
run:

```bash
docker run --rm --network=none --user "$(id -u):$(id -g)" \
  --mount "type=bind,source=$PWD/tests/fixtures/data/sst,target=/data" \
  local/gdal:dev python3 /data/make_fixtures.py /data
```
