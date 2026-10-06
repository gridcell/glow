# glow-exec

glow-exec is the step runtime. It is one static Go binary that runs inside a
toolpack container. For each step it prepares the inputs, runs the tool and
publishes the outputs. The source is in `glow-exec/`.

The compiler starts a step as:

```text
/glow/exec run --tool <name@major> -- <command...>
```

If no command follows `--`, glow-exec runs the tool's `command` from the
manifest.

The glow-exec image has no shell. The init container of a step pod copies the
binary into a shared volume with:

```text
/glow-exec install /glow/exec
```

## Phases

`run` executes three phases in order. `stage` and `collect` run one phase
alone, which helps when you debug a step.

1. **stage**
   - Evaluates `if`. If it is false, stage writes `{"skipped": true}` to
     `/work/outputs.resolved.json` and glow-exec exits 0 without running the
     tool.
   - Evaluates every `${{ }}` expression in the `with` block.
   - Applies input defaults from the manifest.
   - Validates each input against its manifest declaration, then checks the
     media type of each resolved file.
   - Downloads each file input to `/work/in/<input>/<basename>`.
   - Writes `/work/inputs.json`.
2. **run**: runs the command as a child process in `/work`, with a minimal
   environment, and streams its stdout and stderr.
3. **collect**
   - Reads `/work/outputs.json`.
   - Finds each file output at its manifest `path`, or at the path the tool
     wrote to `outputs.json`.
   - Checks that every declared output exists and that each file extension
     fits its media type.
   - Uploads files to `<GLOW_RUN_PREFIX>/<output>/<basename>`.
   - Writes `/work/outputs.resolved.json`, and the value of each output to
     `/work/outputs/<output>.json`. Argo reads each of these files as one
     output parameter.

A validation error names the input or output and the rejected value or the
expected path. Stage errors happen before the tool starts.

## Parameters

The compiler passes the step parameters as environment variables. A command
line flag overrides each one. A flag value that starts with `@` names a file,
whose content is read without base64 encoding.

| Variable | Flag | Content |
| --- | --- | --- |
| `GLOW_RAW_WITH` | `--raw-with` | Base64 of the step's `with` block as JSON, with `${{ }}` expressions unevaluated. |
| `GLOW_MANIFEST` | `--manifest` | Base64 of the toolpack manifest (YAML or JSON), or of a single tool spec for `run` and `script` steps. |
| `GLOW_SCOPE` | `--scope` | JSON object. Each top-level key is an expression variable: `inputs`, loop variables, `let` values. |
| `GLOW_LET` | `--let` | Base64 of a JSON list of `{"name", "value"}` objects: the `let` bindings to evaluate, in order, with `${{ }}` unevaluated. Empty means none. |
| `GLOW_IF` | `--if` | The step's `if` value, for example `${{ steps.count.outputs.total > 0 }}`. A value without `${{ }}` is one expression; the compiler passes this form, because Argo reads `{{` as its own tag. Empty means run. |
| `GLOW_UPSTREAM_<step>` | `--upstream <step>=<json>` | The `outputs.resolved.json` of an upstream step. `<step>` is the step id as written in the workflow. The flag can repeat. |
| `GLOW_RUN_PREFIX` | `--run-prefix` | Where outputs go: an absolute path, `file://` URI or `s3://` URI. Required by collect. |
| `GLOW_WORK_DIR` | `--work-dir` | The work directory. The default is `/work`. |
| `GLOW_STAGING` | `--staging` | `copy` (the default) or `auto`, which uses `copy`. `none` is not supported yet. |

For `run` and `script` steps, the compiler writes the tool spec. An input it
cannot type statically has no `type`, and accepts any value.

Each decoded parameter is limited to 16 MiB. The manifest is limited to
1 MiB. Linux limits one environment variable to 128 KiB, so large values
need the parameter spill to files, which is a planned follow-up.

## Values inside blocks

An Argo template sees only its own inputs and the workflow parameters. So
the compiler passes each value that a step in a `for_each` block uses from
outside the block as a template input, through every level of nesting:

| Template input | Value |
| --- | --- |
| `loop-<block>` | The loop variable of `<block>`, as JSON. |
| `upstream-<step>` | The `outputs.resolved.json` of a step outside the block. |
| `task-<step>-<output>` | One output of a step outside the block, for the `for_each` of a nested block. |
| `run-prefix` | The run prefix of the block item. |

`<block>` and `<step>` are Argo task names. A template declares only the
inputs that its tasks use.

The step then gets these parameters:

- `scope` holds `inputs` and each loop variable that the step uses, from
  any level. Argo substitutes the values before the pod starts.
- `let` lists each `let` binding that the step uses, and each binding that
  those bindings use. The outermost block comes first. In one block, the
  order is the order of definition. Stage evaluates each binding with the
  scope, the upstream steps and the bindings before it. Then it evaluates
  `if` and the `with` block, which can use the bindings.
- `upstream-<step>` is given for each step the expressions use, also for a
  step outside the block. The references of each binding and of the `if` of
  each enclosing block count.

For example, take this block:

```yaml
- id: per_scene
  for_each: ${{ inputs.scenes }}
  as: scene
  let:
    scene_id: ${{ scene.id }}
  steps:
    - id: per_band
      for_each: ${{ scene.bands }}
      as: band
      steps:
        - id: cog
          uses: gdal.translate@1
          with:
            source: ${{ band.path }}
            subdataset: ${{ scene_id }}
```

The step `cog` gets:

```text
scope: {"inputs":{"scenes":{{workflow.parameters.scenes}}},
        "scene":{{inputs.parameters.loop-per-scene}},
        "band":{{inputs.parameters.loop-per-band}}}
let:   base64 of [{"name":"scene_id","value":"${{ scene.id }}"}]
```

The template `per-band-block` declares `loop-per-scene`, `loop-per-band` and
`run-prefix`. The template `per-scene-block` passes
`{{inputs.parameters.loop-per-scene}}` on to it.

glow-exec has one variable per name. If a step needs two variables with the
same name, the compiler fails with `GLOW-E050`. For example, a `let` of an
outer block uses the loop variable `x`, and an inner block also names its
loop variable `x`.

A block output that reads a nested block's output is an array with one
entry per item. Each entry is the inner array, so the type is
`array<array<T>>`.

## Expressions

Expressions are CEL. The variables are the keys of the scope, plus `steps`.
`steps.<id>.outputs` holds the outputs of an upstream step. A skipped
upstream step has an empty `outputs` map and `skipped: true`.

A string that is one expression and nothing else keeps the result type. In a
longer string, each result is spliced in as text: strings as they are, other
values as JSON.

A resolved file is a map `{uri, media_type, kind}`. glow-exec adds `path`
with the same value as `uri`, so `g.files[0].path` works before staging.
After staging, `inputs.json` holds the local path.

Custom functions:

| Function | Result |
| --- | --- |
| `date(value, format)` | Timestamp in UTC. The format supports `%Y %m %d %H %M %S %j %%`. Each directive reads one digit up to its width. |
| `path.basename(p)` | The last segment of a path or URI. A trailing `/` is ignored. |
| `path.stem(p)` | The basename without its extension. |
| `path.ext(p)` | The extension with its dot, for example `.gz`. A leading dot does not start an extension. |
| `path.join(a, b)` | `a` and `b` joined with one `/`. |
| `media.matches(actual, declared)` | True if `actual` fits `declared`. See [Media types](#media-types). |
| `media.ext(media_type)` | The preferred extension without a dot, for example `tif`. Unknown types are an error. |

One expression has a cost limit, so a hostile expression cannot run for a
long time.

To evaluate one expression, use `glow-exec eval`:

```bash
glow-exec eval --env '{"g": {"key": "20240105"}}' "date(g.key, '%Y%m%d')"
glow-exec eval --template --env @env.json 'sst-${{ g.key }}'
```

`tests/fixtures/expressions/cases.json` holds cases that the Go and Python
evaluators must agree on. The Go test `internal/cel/parity_test.go` runs
all of them.

## Input values

| Kind | Accepted values | Value in `inputs.json` |
| --- | --- | --- |
| `file` | A URI or path, or a resolved file map | `/work/in/<input>/<basename>` |
| `bundle` | A URI or path of a directory or prefix, or a resolved bundle map | `/work/in/<input>/`, with the relative layout kept |
| `group` | A list of files, or a map with a `files` list (a resolved group or an `fs.group` entry) | A list of paths in `/work/in/<input>/`. Two files with the same basename are an error. |

## Outputs

`/work/outputs.resolved.json` has this shape:

```json
{
  "outputs": {
    "result": {"uri": "s3://bucket/run/cog/result/out.tif", "media_type": "image/tiff; application=geotiff; profile=cloud-optimized", "kind": "file"},
    "tiles": {"uri": "s3://bucket/run/cog/tiles", "kind": "bundle"},
    "parts": {"uri": "s3://bucket/run/cog/parts", "kind": "group", "files": [{"uri": "...", "media_type": "text/csv", "kind": "file"}]},
    "info": {"bands": 3}
  },
  "skipped": false
}
```

Rules for outputs:

- A file output with a manifest `path` is read from that path under
  `/work/out/`, with `{ext}` replaced by the extension of its media type.
- A file or bundle output without a `path` must have its path, relative to
  `/work/out/`, in `outputs.json`. A group output without a `path` must
  have a list of paths.
- Every non-file output must be in `outputs.json` and match its declared
  type. A key in `outputs.json` that the manifest does not declare is an
  error.
- Output paths must stay under `/work/out/`. Symbolic links that point out
  of it are refused, and bundles must not contain symbolic links.

## Media types

A value of media type `actual` fits a declared type when:

- the declared type is `*`, or
- the base types are equal, or the declared type is `type/*` and the base
  type of `actual` starts with `type/`, and
- every parameter of the declared type is in `actual` with the same value.

So a cloud-optimized GeoTIFF fits `image/tiff; application=geotiff`, but a
plain `image/tiff` does not.

Stage checks the media type of each resolved file map against the input's
`media_type`. A bare URI has no media type and is not checked. An input
declared with a `category` arrives with the category already expanded into
`media_type`, so glow-exec has no category logic.

Collect checks each file extension against its media type with a table of
common extensions in `internal/mediatype`. An unknown extension is accepted.
A `.json` file fits any `+json` type, for example a STAC item `item.json`
with type `application/geo+json`. A file output with no declared media type gets one from its extension, or
`application/octet-stream`.

## Storage

The run prefix and input URIs can be:

- an absolute path or a `file://` URI, for local runs and tests.
- an `s3://` URI. The AWS SDK reads credentials, region and endpoint from the
  standard configuration. Set `AWS_ENDPOINT_URL_S3` for another endpoint, and
  `GLOW_S3_PATH_STYLE=true` for path-style addressing, as MinIO usually
  needs.

Other schemes, such as `http://`, are refused. An upload is one `PutObject`
request, so one object can be at most 5 GB.

## Security

- The tool gets only `PATH`, `TMPDIR`, `LANG`, `LC_ALL`, `TZ`, `HOME` and
  `GLOW_WORK_DIR`. Cloud credentials stay with glow-exec.
- The command runs through `exec`, never through a shell.
- Input and output names must be identifiers, because they become
  directory names.
- Bundle object names and output paths that leave their directory are
  refused.
- On SIGTERM, glow-exec sends SIGTERM to the tool and kills it after 10
  seconds.

## Exit status

| Status | Meaning |
| --- | --- |
| 0 | The step succeeded, or `if` was false. |
| 1 | glow-exec failed: bad parameters, invalid inputs, a missing output, or a storage error. |
| 2 | Usage error. |
| Other | The tool failed. glow-exec passes on the tool's own status. |

## Build and test

```bash
cd glow-exec
go vet ./...
go build ./...
go test ./...
docker build -t glow-exec .
```

The S3 store test needs an S3-compatible service and runs with a build tag:

```bash
AWS_ENDPOINT_URL_S3=http://localhost:9000 GLOW_S3_PATH_STYLE=true \
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=us-east-1 \
GLOW_TEST_S3_BUCKET=glow-test go test -tags s3 ./internal/prefix/
```
