# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:6cd5cc61 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

Python 3.12 managed with uv.

```bash
uv sync                              # install
uv run ruff check                    # lint
uv run ruff format --check           # format check
uv run pytest                        # tests
uv run pytest tests/test_types.py --cov=glow.types --cov-fail-under=100   # types module stays at full coverage
uv run glow schema export --check    # committed schemas match the models
uv run glow schema export            # regenerate schemas after a model change
uv run glow validate <file>          # validate a workflow or toolpack manifest
uv run glow plan <file>              # validated workflow graph with edge types (--json for the IR)
uv run glow compile <file> --allow-local-images -o out.yaml   # compile to an Argo Workflow
UPDATE_GOLDEN=1 uv run pytest tests/compile   # refresh the golden files after a compiler change, then review the diff
argo lint --offline tests/compile/golden/*.yaml   # CI lints every golden file with the argo CLI
uv run glow run <file> -i name=value   # run locally with Docker (--dry-run prints the plan)
GLOW_RUNNER_TESTS=1 uv run pytest tests/runner -m "not integration"   # runner tests in Docker (needs local/glow-exec:dev)
uv run glow toolpack lint toolpacks/*/manifest.yaml   # lint toolpack manifests
uv run glow toolpack lock --check    # registry.lock.yaml matches the manifests
uv run glow toolpack lock            # regenerate the lock after a manifest change
```

## Architecture Overview

- `src/glow/models/`: pydantic models for workflow files and toolpack manifests. They are the source of truth.
- `src/glow/schemas/`: JSON Schemas generated from the models. Never edit by hand; CI checks they match.
- `src/glow/validation.py`: YAML loading and validation (JSON Schema first, then model cross-field rules).
- `src/glow/expressions/`: `${{ }}` span finder (`syntax.py`); `cel.py` parses with cel-python, collects references, applies the cost limit and evaluates (`Evaluator`); `typecheck.py` infers GLOW types of whole expressions; `functions.py` holds `date`, `path.*` and `media.*`; `refs.py` (`analyze`) is the interface the validator uses.
- `src/glow/validate/`: semantic checks after the schema pass: `tools.py` (uses, with keys, constant with values against the input schema glow-exec uses), `scopes.py` (symbol tables), `graph.py` (later-step refs, cycles, order), `edges.py` (edge types), `errors.py` (Appendix B messages, `GLOW-Exxx` codes).
- `src/glow/ir.py`: JSON round-trippable IR of a validated workflow; `src/glow/plan.py` renders it for `glow plan`.
- `src/glow/compile/`: IR to Argo Workflow with Hera. `argo.py` (templates, DAG tasks, glow-exec pod shape), `scope.py` (references a block cannot see yet, `GLOW-E050`), `naming.py` (Argo-safe names, collisions `GLOW-E052`), `encoding.py` (base64 of `raw-with`, tool specs, scripts). Golden files in `tests/compile/golden/`.
- `src/glow/registry.py`, `src/glow/lock.py`, `src/glow/manifests.py`: resolve `uses:` to a tool and image via `toolpacks/registry.lock.yaml`.
- `src/glow/builtins/`: in-engine built-ins. `catalog.py` declares `fs.group` and `fs.glob` with the same `Tool` model as manifests; `fs.py` implements them; `run_builtin` runs one, `glow-builtin <name>` is the engine image's tool command.
- `src/glow/storage.py`: local and S3 (optional `s3` extra) listing and writing, used by built-ins and the runner.
- `src/glow/runner/`: `glow run`. `local.py` (scheduler, fan-out on threads, Argo-style fan-in, run layout), `docker.py` (`docker run` with glow-exec mounted, mount planning), `inputs.py` (`--input` values). See `docs/runner.md`.
- `images/engine/`, `images/sandbox/`: engine and sandbox Dockerfiles, built by `make images`.
- `src/glow/types.py`: edge types (`file`, `bundle`, `group`, scalars, arrays), media-type parsing and matching, `is_assignable` (ok / runtime_check / mismatch). Pure: no validator or CLI imports.
- `src/glow/categories.py`: media type categories (`raster`, `vector`) that a data input can accept with `category:`. The validator matches against the expanded media types; the compiler expands them into `media_type` for glow-exec.
- `src/glow/cli.py`: typer CLI (`glow validate`, `glow plan`, `glow compile`, `glow run`, `glow builtin`, `glow schema export`, `glow toolpack lint|lock`).
- `docs/toolpacks.md`: toolpack layout, versioning and tool contract.
- `examples/`, `toolpacks/`: example workflows and toolpack manifests, all validated by the tests.
- `docs/decisions.md`: adopted design decisions.

## Conventions & Patterns

- Models use `extra="forbid"`. Cross-field rules raise `PydanticCustomError` with a type starting with `glow_`.
- No em-dashes in code or docs.
