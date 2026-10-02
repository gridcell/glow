# GLOW v1 Reference Context (sparkgeo/glow)

Compressed review of https://github.com/sparkgeo/glow (private). Snapshot: commit `5bc90e4`, 2023-09-26,
191 commits (Tom Christian 186, Alain St. Pierre 5). Repo is dormant since late 2023. 14 open issues.
Purpose of this file: give an agent working on glow_v2 enough context about the original to reason about
the design, reuse ideas, and avoid known pitfalls without re-reading the repo.

---

## 1. What GLOW is

GLOW = a geospatial workflow product. Authors write a **Workflow** (JSON, GLOW schema) that composes
**Tasks** (instances of **Task Definitions**, which wrap a container image). The **GLOW Translator**
(Python) validates the Workflow, resolves dependencies, fetches Task Definitions from a registry, and
emits **Argo Workflows YAML** (plus Argo Events EventSource + Sensor when triggered). At run time Argo
executes task containers plus GLOW "internals" helper containers that move parameters and files via S3.

Analogy used throughout: Task Definition = class, Task = object.

MVP target (discussion #26): S3/Minio GeoJSON drop triggers workflow -> area check (conditional) ->
price check (conditional) -> fetch image -> split -> fan-out COG conversion -> fan-in -> upload to S3 ->
notify. A REST API and UI were planned but never built in this repo. Only the translator, schema,
dev-tools, and some IaC exist.

External refs: Notion "Project GLOW" page; sibling repo `sparkgeo/glow-task-definition` (template for
task authors); `sparkgeo/enbridge_poc` has working IaC that was never merged back (issue #174).

---

## 2. Repo layout

```
schema/versions/v1/            JSON schemas + docs (source of truth for the data model)
  workflow.schema.json, task-definition.schema.json, common.schema.json
  parameter-types.schema.json  GENERATED from type-hierarchy.yml (do not hand-edit)
  type-hierarchy.yml           parameter type tree
  docs/README.md, conditionals.md, mock-workflows/mvp/
  src/generation/parameter_types/   python that generates the enum from the yml
glow_translator_common/versions/
  glow_translator_common_all/  version-agnostic, SHIPPED to public dev-tools + internals image
  glow_translator_common_v1/   v1 shared models/parsers, SHIPPED publicly
glow_translator/
  glow_translator/translator.py           entrypoint, dispatches on schema_version
  versions/glow_translator_all/           private, version-agnostic (Argo output models, TD retriever)
  versions/glow_translator_v1/            private, v1 logic (parser, dependencies, Argo emitter, internals)
dev-tools/versions/v1/         task definition build + test harness, tarred and published
tests/                         pytest (unit) + smoke_tests (end-to-end translate via docker)
sample_workflows/              hand-written Argo YAML experiments (fan-out, triggers, mutex, etc)
iac/                           AWS CDK (EKS blueprints + Argo helm addon), BROKEN per issue #174
deploy/, argoCD_workflows/     manual deploy notes, Google SSO, minikube quickstart
scripts/                       dev-init, test, build-schema, build-dev-tools, deploy internals image
version-info.json              {"supported_schema_versions":[1],"latest_version":1}
```

**Public/private split rationale**: `*_common_*` packages are copied into the public dev-tools tarball
and the public `sparkgeo/glow-v1-argo-internals` Docker image, so they are kept minimal. `glow_translator_*`
stays private. Namespaced packages, each with its own `setup.py`, installed editable by `scripts/dev-init.sh`.

**Versioning strategy** (discussion #154): integer schema versions, not semver. Additive changes stay in
the version; breaking changes bump it. Translator version mirrors Workflow schema version. Task Definition
schema is re-versioned in lockstep even if unchanged. Workflow vN should accept Task Definitions vN and
vN-1. Parameter types "belong" to the Workflow schema.

---

## 3. Toolchain and conventions

- Python 3.11, pydantic 1.x (`BaseSettings` for config), `jsonschema` 4.19 + `referencing` for Draft-07
  validation, `aiohttp` for TD fetch, PyYAML for output, dataclasses (`kw_only=True`) for all models.
- black, isort (black profile), flake8 (max line 120, complexity 9), mypy via pre-commit. CI = GitHub
  Actions `validate.yml`: lint -> tests -> on main, build+push internals image.
- Settings via env prefixes: `common_` (schema_docs_root_dir), `v_all_` (task_definition_cache_path),
  `v1_argo_` (ttl_seconds_success=3600, ttl_seconds_failure=172800, internals_image_tag="main").
- **Branch-tagged internals image**: `sparkgeo/glow-v1-argo-internals:<git-branch>`. `constants.py` uses
  pygit2 (dev extra) to read the current branch at import time. Generated Argo YAML hardcodes this tag.
  If the tag was not pushed, Argo hangs for minutes then fails. Deploy with
  `scripts/deploy-v1-argo-internals-image.sh`.
- Names must be Argo-safe: `[^a-z0-9-]` -> `-`, lowercased (`formats/argo/util.py`).
- Logging: workflow param `logging` (default 2 = info; 0 error, 1 warning, 3 debug) injected into every
  task container as env `GLOW_log_level`. Author-defined `logging` input is overwritten.

---

## 4. Data model (schema v1)

### Task Definition
Required: `id` (`[A-Za-z0-9_-]+`, not versioned), `schema_version`, `name`, `container_image`
{registry*, repository?, name*, tag* (not "latest"), secrets?}. Optional: description, labels,
`command` (string[]), `script` (base64; mounted at `/script/script`; `${script}` placeholder in command is
replaced, else path appended), `input_parameters` (max 150), `output_parameters`, `secrets`
[{name, description}], software_licenses, attributions, `documentation` (base64 markdown, overwritten at
build from src/README.md).

Referenced from a Workflow by **task_definition_id** =
`http(s)://<registry>/<account>/tasks/<name>:<x.y.z>`. Translator fetches
`<protocol>://<registry>/<account>/tasks/<name>.<version>.json`, caches at
`glow_translator_all/task_definitions/.cache/<registry>.<account>.<name>.<version>.json`, validates,
and rewrites `id` to the full URL for global uniqueness. Real registry used: an S3 website bucket
`glow-task-definitions.s3.us-west-2.amazonaws.com/sparkgeo/tasks/...` (since deleted; issue #175).

### Parameter
`{name ([A-Za-z0-9_]{1,50}), description, parameter_type}`. Input adds `default_value` (presence =
optional). Output adds `data_licenses`.

### Parameter types (type-hierarchy.yml)
Hierarchical slash paths. Roots:
- `number`: boolean (1/0), positive-number, negative-number, integer/{positive,negative}, float/{...}
- `string`: crs/{authority-prefixed-identifier/{epsg,esri}-prefixed-identifier, proj4, wkt},
  url/{http-https-url, ftp-url, s3-url}, auth/{jwt, JSON-encoded-user-pass, JSON-encoded-AWS-credentials},
  layer-name, temporal/{iso-8601-date, iso-8601-tz-aware-datetime, duration-and-unit},
  data/{JSON, GeoJSON, WKT, GML}, email-address
- `filesystem-object`: file/{JSON-file, geo-file/{vector-file/{GeoJSON,GeoPackage,Shapefile-shp}-file,
  vector-tile-archive, raster-file/TIF-file/COG-file, raster-tile-archive}},
  directory/geo-directory/{vector-collection, vector-tile, raster-collection, raster-tile}-directory-path
- `cloud-object`: AWS-S3-object[filesystem-object], Azure-Blob-object[...], http-https-object[...],
  ftp-object[filesystem-object/file]  (parameterised = expands to cloud-object/X/filesystem-object/...)
- `array[number|string|filesystem-object|cloud-object]` -> `array/<member path>` (1-D only)
- `reference`: special, task-definition inputs only; receives a dict of ALL prior parameter values
  (filesystem-object values blanked). Used by string-builder/logger tasks with `${references.a.outputs.b}`.

Rules: outputs should be as specific as possible, inputs as general as possible. Workflow inputs cannot
be `filesystem-object/*` (use `cloud-object/*`). Type-compatibility validation between linked params was
never implemented (issue #153).

### Workflow
Required: `name` (<=100), `schema_version`, `steps` (>=1). Optional: `conditionals`, `fan_outs`,
`triggers` (only one supported), `input_parameters`, `input_parameter_values`, `output_reference_values`.

**Executable IDs** `^[a-zA-Z][a-zA-Z0-9-]+$` <=50, unique across steps+conditionals+fan_outs.

**Node** (abstract): id, name?, `input_parameter_values`, `waits_for_ids` (explicit ordering when no
data dependency). Subtypes:
- **Task**: `type:"task"`, `task_definition_id`, `resources` {min/max_cpu, min/max_memory_mib,
  min/max_gpu} -> k8s requests/limits via podSpecPatch, `secret_values`, `container_image_secret_values`.
- **Workflow Caller**: `type:"workflow caller"`, `workflow_id`. Parsed but translation raises
  NotImplementedError (issue #138). Schema has typo `"require"` instead of `"required"`.

**Parameter Value** `{value_type: constant|reference, value}`; with-targets adds `targets: [names]`.
Reference grammar:
- `workflow.inputs.<name>`
- `<executable-id>.outputs.<name>` (tasks or fan-outs; conditionals have no outputs)
- `<id>.status` (conditionals only; values "Succeeded"/"Failed")
- `fan-out-entry`, `parent-fan-out-entry`, `parent-parent-...` (inside fan-outs; current iteration value)
Constants must be array/number/string JSON. Nothing else (no functions; use a task instead).

**Conditional** {id, name, `test`, `passed_ids`, `failed_ids`?}. `test` is a tree:
- Composer {type:"composer", member: any|all|one, content:[...]}
- Modifier {type:"modifier", member: not, content: {...}}
- Condition {type:"condition", member, content:{a, b}}; at least one of a/b must be a reference.
  Members: equalTo (any type), greaterThan/greaterThanOrEqualTo/lessThan/lessThanOrEqualTo (number or
  string/temporal/iso-8601), patternMatch/startsWith/endsWith/contains (string; `re.match` anchored at
  start, case-sensitive). Constants are coerced to the reference's type at run time.
  Referencing `X.status` marks X as `permitted_to_fail` so the DAG continues on failure.

**Fan-Out** {id, name, `fan_out_on` (must resolve to `array/*`), `max_parallelism`?, `ids` (members:
nodes, conditionals, nested fan-outs), `output_parameter_values`}. Outputs are aggregated as arrays
(`array/<type>`); filesystem outputs collapse to one `filesystem-object/directory` via common prefix.
Members may only be referenced from inside the same fan-out; expose via fan-out outputs otherwise.
`max_parallelism` implemented with an Argo mutex named `<fanout>-template-{{sprig.mod(entry_index, N)}}`.

**Trigger** (abstract) {id, name, trigger_type}. Only **Minio Bucket Event** is implemented
(S3BucketEvent model exists, disabled): access_key/secret_key (k8s SecretValue {secret_name,
secret_property}), bucket_name, s3_events (`s3:...`), filters {prefix, suffix}, endpoint (`http(s)://`).
Mappable outputs: bucket_name, event_type, event_time, endpoint, triggering_access_key, triggering_ip,
object_key, object_byte_count, object_content_type -> Argo Events dataKeys `notification.0.s3.*` etc.
Trigger-supplied workflow inputs arrive as plain strings (not JSON-encoded), handled specially.

**Secrets**: never via input params. `secret_values` map k8s secret name+key to TD secret names; exposed
in container as env `GLOW_secrets_<name>` via secretKeyRef. Unmapped secrets fall back to k8s secret
`glow-defaults` key `empty-secret-value` (must exist in namespace). Translator only warns on mismatch.

---

## 5. Translation pipeline (`translate(workflow_json, TranslationFormat.ARGO) -> List[Result]`)

1. `glow_translator.translator.translate`: parse JSON, require `schema_version`, pick handler, check
   format, `validate_against_schema` (loads all `*.schema.json` in the version dir into a referencing
   Registry), delegate to `glow_translator_v1.translator.translate`.
2. v1: `workflow_parser.parse` -> dataclass `Workflow`. Reject unsupported trigger types.
   `retrieve_task_definitions_from_workflow` (async gather, cached). `build_executables` (dependency
   graph + validation). Then `formats/argo/argo_translator.translate`.
3. **dependencies.py `build_executables`** (flagged as refactor candidate, issue #139): builds
   `Executable{execute: Node|FanOut|Conditional, dependencies:[Dependency{executable,
   requires_conditional_outcome, requires_status_data}], nested_by, permitted_to_fail}`.
   Edges from: input reference values, conditional passed/failed membership, waits_for_ids, conditional
   test references, fan_out_on. Dependencies of fan-out members are **promoted** to the fan-out (unless
   the dependency is a sibling in the same fan-out), recursively for nested fan-outs.
   Validations raise: WorkflowDuplicateIdException, WorkflowNonexistentDependencyReference,
   WorkflowInvalidReferenceValueProvider, WorkflowConditionalBranchExecutableDuplication (same node in
   passed and failed), WorkflowNoDependencyFreeStartPoint, WorkflowIncompatibleBranchDependencies
   (depends on both branches), WorkflowOutsideFanOutMemberReference, WorkflowCircularReference.
4. **argo_translator.translate** assembles `Workflow{metadata.generateName: <name>-, spec{entrypoint:
   main dag, arguments, templates, ttlStrategy, onExit}}`. Workflow arguments = each workflow input
   (value JSON-dumped; default or constant) + `logging` + `__s3_key_all_parameters` =
   `{{workflow.name}}/parameters` + `__s3_key_all_scripts` = `{{workflow.name}}/scripts`.
   Templates: main DAG, one DAG template per fan-out (`<fanout-id>-template`), `initialiser`,
   `pre-task`, one container template per Task Definition (name = argo-safe TD URL), `conditional`
   (if any), `fan-out-lister` + `fan-out-collector` (if any), `outputs` exit handler (if
   output_reference_values). With a trigger: emits two Results, `<out>-event-source.yaml` (EventSource)
   and `<out>.yaml` (Sensor wrapping the Workflow; sensor parameters map trigger dataKeys onto
   `spec.arguments.parameters.<index>.value`). YAML multi-line strings use `|` style.
5. **dag.py `Dag.create_dag_template`**: entrypoints depend on `initialiser`; dependents sorted by max
   chain depth. `depends` strings use `X.Succeeded` (or `(X.Succeeded || X.Failed)` if permitted to
   fail); `when` strings use `{{tasks.<cond>.outputs.parameters.conditional_outcome}} == 1|0`.
   Per Task with inputs or outputs: a `pre-<id>` DagTask (template pre-task) then `<id>` DagTask.
   Fan-out => three DagTasks: `<id>-lister`, `<id>-invoker` (withSequence count from lister's
   `entry_count`, parameters `entry_index={{item}}`, `parent_entry_index=...`), `<id>` (collector; named
   with the fan-out id so output references resolve). Inside fan-outs task names and S3 keys get
   `-{{inputs.parameters.entry_index}}` / `/{{...}}` suffixes per nesting level (parent_ prefixes).
   Resources -> `podSpecPatch` JSON `{containers:[{name:main,resources:{requests,limits}}]}`
   (cpu as `<n>m`, memory `<n>Mi`, gpu `nvidia.com/gpu`).
6. **tasks.py `create_task_templates`**: one template per TD. Inputs: artifact `input_parameter_files`
   at `/input/parameters/` from `s3://.../<wf>/parameters`; per filesystem input two params
   `<name>_s3_key`, `<name>_path` and an artifact mounted at that path; optional `script` artifact at
   `/script`; secret name/key params with defaults. Outputs: `/output/outputs.json` ->
   `<wf>/parameters/<task_name>-outputs.json` (no archive); `/output/files/` ->
   `<wf>/artifacts/<output_files_s3_key>`. Image = `repository/name:tag` (registry omitted).

---

## 6. Run-time internals (`formats/argo/internals/implementation/*`, run via `python -c` in the
internals image; `pydantic` only dependency)

All share an S3 prefix `<workflow>/parameters/` mounted at `/input` containing:
- `<source>-inputs.json`, `<source>-outputs.json` (source = `workflow`, task id, fan-out id, or
  `<id>-<iter>[-<iter>]` inside fan-outs)
- `types.json` + `<fanout>-inputs-types.json` + `<fanout>-outputs-types.json`, merged into
  `{source: {inputs|outputs: {param: type}}}`
Helpers in `util.py`: `get_all_parameter_data` (glob `*-*puts.json`), `get_all_types`,
`get_nested_entry_from_reference("a.b.c")`, `get_array_type_member_type`.

- **initialiser** (`initialise_workflow`): writes `workflow-inputs.json` (decoding JSON-encoded Argo
  values by type; trigger-provided values passed raw), `<task>-inputs.json` for constant/default task
  inputs (defaults first, constants override), `types.json`, and decodes base64 scripts to
  `/scripts/<td-name>`. Workflow params are passed base64 via `sprig.b64enc(workflow.parameters.json)`
  to dodge escaping.
- **pre_task** (`prepare_task`): resolves `reference_map` [{source,target}] for the next task using
  `parameter_source.get_parameter_sources` (handles `fan-out-entry` ancestry, fan-out sibling refs with
  iteration suffixes, `.status`). Filesystem values `/output/files/<rel>` become mount
  `/input/files/<source>/<rel>` with s3 key `<wf>/artifacts/<source>[/<iters>]/<rel>`; arrays of files
  use the common directory. Writes `/output/parameters/parameters.json`, `parameters.env.sh`,
  `parameters.env.bash4` and `/output/artifacts/artifact-paths.json` ({target:{s3_key,mount_path}}),
  read by the DAG via jsonpath. Reference-type params get the whole data dict plus `fan-out-entry` keys.
- **conditional** (`evaluate_test_and_exit`): `Evaluator` resolves a/b, coerces constants
  (`_process_string_value_by_type`: boolean "1"->True, int, float, iso datetime, array via json),
  filesystem types forced to string path, writes "1"/"0" to `/tmp/conditional.outcome`.
- **fan_out_lister** (`generate_list`): reads driving array, writes `<fanout>[-iters]-inputs.json`
  `{entry-0:..., entry-1:...}` (filesystem entries become {s3_key, mount_path}), types file, and
  `/tmp/entry_count`. Only `array/*` driving types (directory-driven is issue #136).
- **fan_out_collector** (`collect`): globs `<source>[-iters]-*-outputs.json`, extracts iteration from
  filename, aggregates to `array/<type>` or directory {s3_key, mount_path}; writes `parameters.json` ->
  `<fanout>[-iters]-outputs.json` and `types.json` -> `...-outputs-types.json`.
- **outputs** (exit handler, `collect_outputs`): builds `/output/outputs.json` for workflow
  `output_reference_values`; filesystem values become `{"s3": "<wf>/artifacts/<source>/<rel>"}`.

**Env var formatting** (`parameter_files.format_values_for_env_files`, shared with dev-tools): prefix
`GLOW_inputs_` / `GLOW_secrets_`; keys sanitised `[^a-z0-9_]`->`_`; strings single-quoted and escaped;
booleans 1/0; arrays: `.sh` gets `_0,_1...` suffixed vars, `.bash4` gets a bash array of base64 members,
DOCKER style (for `--env-file`) unquoted. NOTE: task-development-readme says `GLOW_input_`/`GLOW_secret_`
(singular) but code uses plural `GLOW_inputs_`/`GLOW_secrets_`.

**Task container contract**: read `/input/parameters/parameters.json|.env.sh|.env.bash4`
(and `secrets.*`), files under `/input/files/...`; write `/output/outputs.json` (flat object) and files
under `/output/files/` (paths <=256 chars). Honour `GLOW_log_level`.

---

## 7. Dev-tools (task author test harness)

`scripts/build-dev-tools.sh` copies `glow_translator_common/**/*.py` and v1 schemas into
`dev-tools/versions/v1/dev-tools/glow/`, tars to `.build/dev-tools/glow-task-dev-tools-v1.tgz` + md5
(meant for public S3; publishing never automated, issue #76). Consumers: repos from
`glow-task-definition` template with layout `src/task-definition.json`, `src/script.*`, `src/README.md`,
`tests/<case>/{inputs.json, secrets.json, additional-types.json, input/files/...}`.

`dev-tools/scripts/build.sh` builds image `sparkgeo/glow-task-definition-builder-v1`, copies TD to
`build/`, embeds README + script as base64, validates against schema.
`dev-tools/scripts/test.sh [--container_image_source local]`: per test dir validates inputs, generates
parameter files, extracts image/entrypoint/command, pulls image (docker socket mounted), runs the task
container with `/input`, `/output`, `/script` mounts, validates outputs.json. Bundled test TDs:
crs_extractor, polygon_area_calculator (osgeo/gdal image + python script), vector-reprojector,
mock_data_generator, secret_checker.

---

## 8. Tests

`scripts/test.sh` = `test-translator.sh` (coverage + pytest over `tests/`), schema generation pytest,
`validate-mock-workflows.sh` (docker-built `sparkgeo/glow-schema-builder-v1` regenerates
parameter-types.schema.json then validates mvp mock docs), `test-dev-tools.sh`, and
`tests/smoke_tests/test.sh` (docker image runs translator over `tests/smoke_tests/input/*/input.json`
using cached TDs; outputs to `tests/smoke_tests/output/`). Smoke inputs: calculate-polygon-area (the
canonical example with a conditional, resources, references, secrets), failure-logger, fetch-cdem-data,
filename-checker. Unit tests mirror the package tree (dag, tasks, dependencies, internals impls, parsers).

---

## 9. Infra and deployment

- Local: minikube, Argo Workflows 3.4.x installed in namespace `argo` with `--auth-mode=server`,
  Minio via helm as `argo-artifacts` with bucket `artifacts`, workflow-controller configmap
  `artifactRepository.s3` pointed at Minio, rolebinding admin for `argo:default`, secret `glow-defaults`.
  Argo Events needed for triggers (see sample_workflows/minio-trigger/setup). ENVIRONMENT.md has steps.
- AWS (aspirational): CDK + `@aws-quickstart/eks-blueprints` EKS (spot t3/m5.large, k8s 1.23), ALB,
  cert-manager, external-dns on `prescient.earth`, Argo helm chart 0.20.6 with Google SSO and IRSA role
  for an S3 artifact bucket. Stacks `glow-dev` (us-east-2), `glow-staging2` (us-west-2). Declared broken.
- Sample Argo YAMLs are design spikes proving: artifact-driven fan-out/fan-in, auto artifact listing,
  mutex concurrency limits, HTTP conditional, Minio trigger, nested workflows, suspend/resume polling,
  the 2 MiB output parameter limit (hence files + S3 everywhere), and a "mediator" task pattern that
  became pre-task.

---

## 10. Known gaps, bugs, and open issues (as of snapshot)

- #175 automate publishing Task Definitions to S3 registry (bucket deleted).
- #174 IaC broken; working version lives in enbridge_poc.
- #173 task name clash inside fan-outs (`Task` + iteration suffix can equal `Task_1`).
- #172 `max_parallelism` should accept reference values.
- #153 validate type heritage between linked parameters.
- #140 validate `fan_out_on` is array-typed (runtime NotImplementedError otherwise).
- #139 split validation out of `build_executables`.
- #138 Workflow Callers not implemented (needs an API/registry of workflows).
- #136 directory-driven fan-outs.
- #112 custom schema error messages (`"message"` keys present in schemas but unused by jsonschema).
- #103 schema/ python not in namespace-package layout.
- #76 publish dev-tools publicly.
- #69 private container registries (schema has secrets fields, nothing wires them to imagePullSecrets).
- #57 dynamic constants (`now`, `today`).
- Other observations: only one trigger per workflow; only Minio trigger; S3BucketEvent disabled;
  `WorkflowTranslationFormatUnavailable` only format is ARGO; secrets mismatch only logs warnings;
  singular/plural env prefix doc mismatch; `logging` is both a reserved workflow param and env var.

---

## 11. Ideas worth carrying into a v2

- The declarative dependency inference (references imply order; `waits_for_ids` as escape hatch) and the
  validation set in section 5.3 are the core value and were well tested.
- Typed parameter hierarchy with subtype compatibility is a strong idea that was only half-implemented.
- Keep the task container contract (`/input`, `/output`, env files, outputs.json) stable; it is the public
  surface task authors depend on.
- Pain points to design away: per-branch internals image tags; Argo's 2 MiB output limit forcing S3
  round-trips for every parameter; `python -c` code strings with Argo template interpolation inside the
  YAML (escaping hazards, see base64 workaround); fan-out naming via string suffixes; pydantic v1.
