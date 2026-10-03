"""Load toolpack manifests from disk for the registry, lock and lint commands."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from glow.models import ToolpackManifest
from glow.validation import Problem, parse_yaml, read_document, validate_text

MANIFEST_FILENAME = "manifest.yaml"


class ManifestError(Exception):
    """A manifest failed validation. `problems` holds one entry per finding."""

    def __init__(self, path: Path, problems: list[Problem]) -> None:
        super().__init__(f"{path}: {len(problems)} problem(s) found")
        self.path = path
        self.problems = problems


@dataclass(frozen=True, slots=True)
class LoadedManifest:
    path: Path
    manifest: ToolpackManifest
    sha256: str


def load_manifest(path: Path) -> LoadedManifest:
    """Read and validate one manifest.

    Raises `DocumentReadError` when the file cannot be read and
    `ManifestError` when it is not a valid toolpack manifest.
    """
    text = read_document(path)
    result = validate_text(text)
    if result.kind == "workflow":
        raise ManifestError(path, [Problem("$.toolpack", "required property is missing")])
    if not result.ok:
        raise ManifestError(path, result.problems)
    manifest = ToolpackManifest.model_validate(parse_yaml(text))
    return LoadedManifest(path, manifest, manifest_sha256(manifest))


def manifest_sha256(manifest: ToolpackManifest) -> str:
    """Hash the manifest content, ignoring comments, key order and layout.

    Only keys written in the file count, so adding an optional model field
    with a default does not change the hash of existing manifests.
    """
    data = manifest.model_dump(mode="json", by_alias=True, exclude_unset=True)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def split_image(image: str) -> tuple[str, str | None]:
    """Split `repo@sha256:...` into repository and digest. Local images have no digest."""
    repository, separator, digest = image.partition("@")
    return (repository, digest) if separator else (image, None)
