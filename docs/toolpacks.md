# Toolpacks

A toolpack is one container image and a manifest that describes the tools in
it. Workflow steps run a tool with `uses: <toolpack>.<tool>@<major>`. This
page covers the directory layout, naming, versioning, the lock file, the
contract between a tool and the step runtime, and how to add a tool.

Image signing and publishing are not covered here.

## Layout

```text
toolpacks/
  registry.lock.yaml        # generated; maps tool@major to manifest, image and digest
  gdal/
    manifest.yaml
    Dockerfile
    wrappers/               # one script per tool, copied to /usr/local/bin
  stac/
    manifest.yaml
    Dockerfile
    requirements.in         # pinned Python dependencies of the image
    requirements.txt        # generated with hashes from requirements.in
    wrappers/
  prescient/
    manifest.yaml           # no image yet; its tool ships in the stac image
  tests/                    # wrapper tests that run inside the built images
```

Each directory under `toolpacks/` holds one `manifest.yaml`. The registry
reads every `toolpacks/*/manifest.yaml`. The directory name is usually the
toolpack name. When a toolpack gets a second major version, keep the old
manifest in its own directory, for example `toolpacks/gdal_v1/`, so that both
majors stay in the lock.

## Manifest

```yaml
toolpack: gdal
version: 1
image: ghcr.io/sparkgeo/glow-gdal@sha256:<64 hex digits>
tools:
  - name: gdal.translate
    description: Convert a raster with gdal_translate.
    inputs:
      source: { type: file, media_type: "image/tiff; application=geotiff", remote: true }
      format: { type: string, enum: [COG, PNG], default: COG }
    required: [source]
    outputs:
      result:
        type: file
        path: out.{ext}
        media_type_from: format
        media_types:
          COG: "image/tiff; application=geotiff; profile=cloud-optimized"
          PNG: image/png
    command: [gdal_translate_wrapper]
```

The JSON Schema is `src/glow/schemas/toolpack.schema.json`. The main rules:

- `image` is a digest reference (`<repository>@sha256:<digest>`), never a tag.
  For local development only, `image: local/<toolpack>:dev` is allowed.
- `inputs` are JSON Schema properties plus the data kinds `file`, `bundle`
  and `group`. `media_type` and `remote` are only allowed on data kinds.
- A file output sets `media_type`, or derives it from an input with
  `media_type_from` and `media_types`. The input must have an `enum`, and
  `media_types` must have an entry for every enum value.
- `path` is a fixed path relative to `/work/out/`. It can contain `{ext}`.
  Absolute paths and `..` are not allowed.
- Every tool sets `command`.

## Naming

- Toolpack names are lowercase letters, digits and `_`, starting with a
  letter: `gdal`, `stac`.
- Tool names are `<toolpack>.<name>`, and can have more dot-separated parts:
  `gdal.translate`, `gdal.dem.color_relief`. Names are unique in a manifest.
- Input and output names are identifiers (letters, digits and `_`), because
  expressions refer to them as `steps.<id>.outputs.<name>`.
- The built-in names `fs.group` and `fs.glob` are reserved. A toolpack must
  not provide a tool with one of these names.

## Versioning

Workflows refer to a tool as `name@major`, for example `gdal.translate@1`.
The `version` key of the manifest is the major version of every tool in it.

- A minor change is additive: a new optional input, a new output, a new tool,
  a new enum value with its `media_types` entry, or a new image digest that
  keeps the same behavior. A minor change keeps `version`.
- Any other change is breaking: removing or renaming an input, output or
  tool, making an input required, changing a type, or removing an enum value.
  A breaking change needs a new major version in a new manifest. Keep the old
  manifest until no workflow uses the old major.

Built-ins are part of the engine and have no major version. Write
`uses: fs.group`, not `uses: fs.group@1`.

## Lock file

`toolpacks/registry.lock.yaml` has one entry per `tool@major`:

```yaml
tools:
  gdal.translate@1:
    toolpack: gdal
    manifest: gdal/manifest.yaml
    image: ghcr.io/sparkgeo/glow-gdal
    digest: sha256:<64 hex digits>
    manifest_sha256: <sha256 of the manifest content>
```

- `digest` is `null` for a local image (`image: local/<toolpack>:dev`).
  `glow toolpack lint` warns about local images. The compiler refuses them
  unless `--allow-local-images` is given.
- `manifest_sha256` is the SHA-256 of the manifest content as canonical JSON.
  Comments, key order and layout do not change it.
- The registry refuses to load when a manifest does not match its lock entry.

Do not edit the lock by hand. Regenerate it with `glow toolpack lock`. CI
runs `glow toolpack lock --check`, which fails when the lock is out of date.

## Tool contract

The step runtime (glow-exec) runs the tool's `command` in the toolpack image
with this layout:

| Path | Written by | Content |
| --- | --- | --- |
| `/work/inputs.json` | runtime | The step's `with` values as one JSON object keyed by input name. |
| `/work/in/` | runtime | Staged input files. A `file` input in `inputs.json` is a path under `/work/in/`. |
| `/work/out/` | tool | Output files, each at the fixed `path` declared in the manifest. |
| `/work/outputs.json` | tool | Values of the non-file outputs, as one JSON object keyed by output name. |

Rules for tools:

- Read inputs only from `/work/inputs.json` and `/work/in/`.
- Write each file output to its declared `path` under `/work/out/`. Replace
  `{ext}` with the extension for the chosen format. The runtime collects
  outputs from these fixed paths; it does not scan the directory.
- Write every declared non-file output to `/work/outputs.json`.
- Exit with status 0 on success and non-zero on failure.
- An input with `remote: true` can receive a URI instead of a staged path
  when the step sets `staging: none`. The tool must then read the URI
  directly, for example through GDAL `/vsis3/`.

## Images and wrappers

Each tool's `command` is a wrapper script in the image. The wrapper reads
`/work/inputs.json`, maps the inputs to the flags of the underlying program,
and writes the outputs. The manifest stays declarative. Wrappers read
`GLOW_WORK_DIR` (default `/work`). glow-exec gives the tool a minimal
environment, so a wrapper must not depend on `ENV` values from the
Dockerfile. Images run as the non-root user `glow`.

| Image | Base | Tools |
| --- | --- | --- |
| `glow-gdal` | `ghcr.io/osgeo/gdal:ubuntu-full`, pinned by digest. `ubuntu-small` has no netCDF driver. | `gdal.translate`, `gdal.dem.color_relief`, `gdal.info` |
| `glow-stac` | `python:3.12-slim`, pinned by digest, with pystac and rasterio | `stac.item`, `stac.publish`, `prescient.render_from_color_table` |

Tool behavior that the manifests do not show:

- `gdal.translate` reads `subdataset` as `NETCDF:"<source>":<subdataset>`.
  Each `creation_options` entry becomes `-co KEY=VALUE`.
- `gdal.dem.color_relief` runs `gdaldem color-relief -alpha`, then
  `gdal_translate` to `format`. `size` becomes `-outsize`; a 0 keeps the
  aspect ratio.
- `stac.item` writes a STAC 1.0 item to `item.json`. Assets come from
  `assets` (a map) and `asset_entries` (a list of assets with a `key`). An
  entry replaces an asset with the same key. An `href` can be a resolved
  file, whose media type becomes the asset `type`. The bbox and geometry
  come from the first asset that is a local georeferenced raster. The tool
  has no storage credentials, so an `s3://` href gives no footprint.
  `extensions.render` becomes the `renders` property.
- `stac.publish` writes each item to `<dest>/<collection>/<item-id>.json`
  and replaces an older copy. Then it rebuilds
  `<dest>/<collection>/items.json`, a GeoJSON FeatureCollection of all items
  in that directory. `dest` and the items must be local paths or `file://`
  URIs. S3 and pgSTAC destinations are not supported yet.
- `prescient.render_from_color_table` reads a gdaldem color table. The
  render rescales the table's value range onto 0..255 and has a 256-entry
  `colormap` interpolated between rows. Rows with value `nv` are skipped;
  percentages and color names are refused.

Build and test the images with docker:

```bash
make images        # local/gdal:dev, local/stac:dev and local/prescient:dev
make images-test   # toolpacks/tests, run inside the images
```

`make images` also builds glow-exec and, when their Dockerfiles exist, the
engine and sandbox images. The committed manifests use the local images, so
`glow toolpack lint` warns and the lock has `digest: null`. To pin published
digests, push the images and rewrite the manifests and the lock:

```bash
make images-push REGISTRY=ghcr.io/sparkgeo
make images-lock REGISTRY=ghcr.io/sparkgeo
```

To change the stac image dependencies, edit `requirements.in` and run the
`uv pip compile` command at the top of that file.

## Built-ins

Built-ins run inside the engine, not in a toolpack image. They are declared
in `src/glow/builtins/catalog.py` with the same tool model as manifests.

| Built-in | Inputs | Output |
| --- | --- | --- |
| `fs.group` | `root` (URI), `pattern` (regular expression with named captures), `key` (capture name), `media_type` (optional) | `groups`: array of `{key, captures, files}` |
| `fs.glob` | `root` (URI), `pattern` (glob) | `files`: array of file |

## Add a tool

1. Add the tool to the toolpack's `manifest.yaml`, or create
   `toolpacks/<toolpack>/manifest.yaml` for a new toolpack. For local work,
   use `image: local/<toolpack>:dev`.
2. Implement the command in the image so that it follows the tool contract.
3. Check the manifest:

   ```bash
   uv run glow toolpack lint toolpacks/<toolpack>/manifest.yaml
   ```

4. Regenerate the lock and commit it with the manifest:

   ```bash
   uv run glow toolpack lock
   ```

5. Before merge, replace a local image with the published digest reference
   and run `glow toolpack lock` again.
