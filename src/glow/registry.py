"""Resolve `uses:` references to tools, images and digests.

The registry is the `toolpacks/` directory: one manifest per toolpack major
plus `registry.lock.yaml`. Workflow files write `<toolpack>.<tool>@<major>`;
built-ins such as `fs.group` are written without `@major`.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from glow.builtins import BUILTINS
from glow.lock import LockEntry, LockError, read_lock
from glow.manifests import LoadedManifest, load_manifest, split_image
from glow.models import Tool
from glow.validation import DocumentReadError

_USES = re.compile(r"(?P<name>[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)(?:@(?P<major>[1-9][0-9]*))?")
MAX_USES_CHARS = 128


class RegistryError(Exception):
    """A `uses` reference cannot be resolved."""


class MalformedUsesError(RegistryError):
    pass


class UnknownToolError(RegistryError):
    pass


class UnknownMajorError(RegistryError):
    def __init__(self, name: str, major: int, available: list[int]) -> None:
        listed = ", ".join(str(m) for m in available)
        super().__init__(f"tool '{name}' has no major version {major}; available: {listed}")
        self.name = name
        self.major = major
        self.available = available


@dataclass(frozen=True, slots=True)
class ResolvedTool:
    """A resolved `uses` reference. Built-ins have no toolpack, image or digest."""

    uses: str
    tool: Tool
    toolpack: str | None = None
    image: str | None = None
    digest: str | None = None
    manifest_sha256: str | None = None

    @property
    def builtin(self) -> bool:
        return self.toolpack is None

    @property
    def is_local(self) -> bool:
        """A local development image; the compiler needs `--allow-local-images` for it."""
        return not self.builtin and self.digest is None

    @property
    def image_ref(self) -> str | None:
        """The image reference to run, pinned by digest when there is one."""
        if self.image is None or self.digest is None:
            return self.image
        return f"{self.image}@{self.digest}"


def parse_uses(uses: str) -> tuple[str, int | None]:
    """Split `name@major` into name and major. Raises `MalformedUsesError`."""
    match = _USES.fullmatch(uses) if len(uses) <= MAX_USES_CHARS else None
    if match is None:
        shown = uses[:MAX_USES_CHARS]
        raise MalformedUsesError(
            f"malformed uses {shown!r}: expected <toolpack>.<tool>@<major> or a built-in name"
        )
    major = match.group("major")
    return match.group("name"), None if major is None else int(major)


class Registry:
    def __init__(self, tools: Mapping[str, Mapping[int, ResolvedTool]]) -> None:
        self._tools = tools

    @classmethod
    def load(cls, root: Path) -> "Registry":
        """Load `<root>/registry.lock.yaml` and the manifests it names.

        Raises `LockError` when the lock is missing, malformed, or out of date
        with a manifest, and `ManifestError` when a manifest is invalid.
        """
        lock = read_lock(root)
        base = root.resolve()
        manifests: dict[str, LoadedManifest] = {}
        tools: dict[str, dict[int, ResolvedTool]] = {}
        for key, entry in lock.tools.items():
            if entry.manifest not in manifests:
                manifests[entry.manifest] = _load_locked_manifest(base, entry.manifest)
            name, major = key.rsplit("@", 1)
            resolved = _resolve_entry(key, entry, manifests[entry.manifest])
            tools.setdefault(name, {})[int(major)] = resolved
        return cls(tools)

    def resolve(self, uses: str) -> ResolvedTool:
        name, major = parse_uses(uses)
        if name in BUILTINS:
            if major is not None:
                raise MalformedUsesError(f"built-in '{name}' takes no @major: write '{name}'")
            return ResolvedTool(uses=uses, tool=BUILTINS[name])
        if major is None:
            raise MalformedUsesError(f"'{name}' needs a major version, such as '{name}@1'")
        majors = self._tools.get(name)
        if not majors:
            raise UnknownToolError(f"unknown tool '{name}'")
        if major not in majors:
            raise UnknownMajorError(name, major, sorted(majors))
        return majors[major]


def _load_locked_manifest(base: Path, relative: str) -> LoadedManifest:
    path = (base / relative).resolve()
    # A symlink could point the lock at a file outside the registry.
    if not path.is_relative_to(base):
        raise LockError(f"manifest {relative} is outside {base}")
    try:
        return load_manifest(path)
    except DocumentReadError as exc:
        raise LockError(str(exc)) from exc


def _resolve_entry(key: str, entry: LockEntry, loaded: LoadedManifest) -> ResolvedTool:
    manifest = loaded.manifest
    name, major = key.rsplit("@", 1)
    tool = next((t for t in manifest.tools if t.name == name), None)
    consistent = (
        loaded.sha256 == entry.manifest_sha256
        and manifest.toolpack == entry.toolpack
        and str(manifest.version) == major
        and split_image(manifest.image) == (entry.image, entry.digest)
    )
    if tool is None or not consistent:
        raise LockError(
            f"lock entry {key} does not match {entry.manifest}; run `glow toolpack lock`"
        )
    return ResolvedTool(
        uses=key,
        tool=tool,
        toolpack=entry.toolpack,
        image=entry.image,
        digest=entry.digest,
        manifest_sha256=entry.manifest_sha256,
    )
