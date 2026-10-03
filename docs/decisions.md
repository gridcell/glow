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
