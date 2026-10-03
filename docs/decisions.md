# Decisions

These are the proposed answers from section 9 of the GLOW v2 plan
(`glowv2_plan.pdf`). They are adopted so that work can start. Each one stays
"adopted, pending confirmation" until the team confirms it.

| # | Decision | Status |
| --- | --- | --- |
| 1 | Python with Hera and cel-python for the CLI, validator, compiler and control API | Adopted, pending confirmation |
| 2 | Go for glow-exec only | Adopted, pending confirmation |
| 3 | CEL for expressions, not jq | Adopted, pending confirmation |
| 4 | Declarative YAML workflow files, not a Python SDK | Adopted, pending confirmation |
| 5 | Toolpack versioning as `name@major` | Adopted, pending confirmation |

## 1. Python with Hera and cel-python

The CLI, validator, compiler and control API are Python. Hera builds the Argo
Workflow objects and cel-python evaluates and type checks expressions.

- **Why:** Matches the team's Python stack and the existing Prescient API.
- **Trade-off:** The cel-python type checker is weaker than cel-go. Some edge
  checks can move from static validation to runtime. If this becomes a
  problem, add a small Go check service that the Python validator calls.

## 2. Go for glow-exec only

The step runtime, glow-exec, is Go. Nothing else is.

- **Why:** glow-exec must be one static binary that an init container copies
  into arbitrary tool images. Python cannot provide that cleanly.

## 3. CEL over jq

Expressions inside `${{ }}` are CEL.

- **Why:** CEL is sandboxed, has no I/O, has a cost limit and has a static
  type checker. The type checker lets glue expressions be checked in the same
  pass as the rest of the workflow.
- **Trade-off:** jq is more expressive for JSON reshaping. CEL cannot build
  maps with dynamic keys; tools such as `stac.item` handle those cases.

## 4. YAML over an SDK

Workflows are declarative YAML files.

- **Why:** YAML gives static validation, clean diffs, and a plan that can be
  shown before cluster money is spent.
- **Trade-off:** An SDK in the style of Flyte or Modal is simpler to build and
  equally natural for a model to write.

## 5. name@major versioning

Workflow files refer to tools as `name@major`, for example
`gdal.translate@1`. The registry maps each major version to an image digest.
Minor changes are additive.

- **Why:** Simpler than semver for authors.
- **Trade-off:** Needs discipline: any breaking change needs a new major
  version.

## Expression checking in the validator

These follow from decisions 1 and 3 and are recorded here because they change
what `glow validate` accepts.

- **Parser and checker.** cel-python parses every `${{ }}`. It has no type
  checker, so GLOW has its own (`src/glow/expressions/typecheck.py`) over GLOW
  types. It types paths, literals, operators, the `map`, `filter`, `all`,
  `exists` and `exists_one` macros, and the GLOW functions. A map literal
  with constant keys is an object with those properties.
- **Downgrades to runtime.** The checker returns `unknown`, which makes the
  edge a runtime check, for: functions and methods it does not know; map
  literals with computed keys; `?:` whose branches have different types;
  lists whose members have different types; any operation on a `file`,
  `bundle` or `group` value, because at runtime such a value is a path string
  in one place and a map in another; durations; and bytes.
- **Cost limit.** cel-python has no cost limit, so the validator bounds the
  expression instead: at most 2000 characters, 200 operations, 300 levels of
  nesting, and 2 levels of nested macros (`GLOW-E006`). glow-exec also applies
  cel-go's runtime cost limit.
- **Functions not in glow-exec yet.** `path.dirname`, `media.accepts`,
  `media.base` and `media.param` exist only in the Python evaluator. They are
  not in the shared fixture. glow-exec must add them before a workflow that
  uses them can run.
- **`media.matches` and `type/*`.** glow-exec accepts a declared type such as
  `image/*`. The Python evaluator uses `glow.types`, which has no type tree,
  so `image/*` is an error there.
- **Shadowed namespaces.** `path` and `media` are function namespaces. glow-exec
  rejects them as variable names, but the validator does not yet reject an
  `as` or `let` name of `path` or `media`.
