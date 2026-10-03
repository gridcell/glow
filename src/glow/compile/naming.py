"""Argo-safe names for tasks and templates.

Argo template and task names may hold lowercase letters, digits and `-`,
must start and end with a letter or digit, and are at most 128 characters.
Step ids are identifiers, so `per_item` becomes `per-item`. Two ids that
differ only in case map to the same name, which `NameTable` reports.
"""

import re

from glow.validate.errors import Code, GlowError

MAX_NAME_CHARS = 128

_UNSAFE = re.compile(r"[^a-z0-9-]")


def argo_name(text: str) -> str:
    """Lowercase `text`, replace every character outside `[a-z0-9-]` by `-` and trim `-`."""
    return _UNSAFE.sub("-", text.lower()).strip("-")


class NameTable:
    """Hands out Argo names and records an error for each collision.

    `owner` identifies what a name stands for, such as a step id; asking for
    the same name twice with the same owner is not a collision.
    """

    def __init__(self, what: str) -> None:
        self.what = what
        self.owners: dict[str, str] = {}
        self.errors: list[GlowError] = []

    def claim(self, name: str, owner: str) -> str:
        if not name or len(name) > MAX_NAME_CHARS:
            self._error(owner, f"'{name}' is not a usable Argo {self.what} name")
        elif self.owners.setdefault(name, owner) != owner:
            other = self.owners[name]
            self._error(owner, f"'{owner}' and '{other}' both become Argo {self.what} '{name}'")
        return name

    def _error(self, owner: str, message: str) -> None:
        hint = "rename one of them; Argo names are lowercase with '-' for '_'"
        self.errors.append(GlowError(Code.NAME_COLLISION, owner, message, hint=hint))
