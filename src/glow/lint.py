"""`glow toolpack lint`: checks on one toolpack manifest.

Schema and model validation cover the structural rules: tool names are
`<toolpack>.<name>` and unique, images are digest references or the local
development image, and every `media_type_from` names an enum input whose
values all appear in `media_types`. Lint adds the rules that need the rest
of the system, and reports local images as warnings.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from glow.builtins import BUILTINS
from glow.manifests import ManifestError, load_manifest
from glow.validation import Problem, format_path

Level = Literal["error", "warning"]


@dataclass(frozen=True, order=True, slots=True)
class Finding:
    level: Level
    problem: Problem

    def __str__(self) -> str:
        return f"{self.level}: {self.problem}"


def lint_manifest(path: Path) -> list[Finding]:
    """Lint one manifest. Raises `DocumentReadError` when it cannot be read."""
    try:
        manifest = load_manifest(path).manifest
    except ManifestError as exc:
        return [Finding("error", problem) for problem in exc.problems]
    findings = [
        Finding(
            "error",
            Problem(format_path(["tools", index, "name"]), f"'{tool.name}' is a built-in name"),
        )
        for index, tool in enumerate(manifest.tools)
        if tool.name in BUILTINS
    ]
    if manifest.is_local:
        message = (
            f"{manifest.image} is a local development image; "
            "the compiler refuses it unless --allow-local-images is passed"
        )
        findings.append(Finding("warning", Problem("$.image", message)))
    return findings
