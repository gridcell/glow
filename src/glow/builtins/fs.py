"""The `fs.group` and `fs.glob` built-ins over local directories and S3 prefixes.

Both list every object under `root`, recursively, and match the path relative
to `root`. Files are resolved file maps `{uri, media_type, kind}` whose URI
keeps the original basename, so staging preserves it.
"""

import re
from typing import Any

from glow import storage

# The pattern is user input compiled by `re`, which has no time limit; a
# length bound keeps the worst case small.
MAX_PATTERN_LENGTH = 1000


class BuiltinError(ValueError):
    """Raised when a built-in's inputs are invalid or its listing fails."""


def group(
    root: str, pattern: str, key: str, media_type: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Group the files whose relative path matches `pattern` by the `key` capture.

    Groups are sorted by key and their files by relative path. `captures`
    holds the named captures of the group's first file.
    """
    regex = _compile(pattern)
    if key not in regex.groupindex:
        raise BuiltinError(f"pattern {pattern!r} has no named capture {key!r}")
    groups: dict[str, dict[str, Any]] = {}
    for relative in _list(root):
        match = regex.search(relative)
        if match is None or match.group(key) is None:
            continue
        value = match.group(key)
        entry = groups.get(value)
        if entry is None:
            captures = {name: text for name, text in match.groupdict().items() if text is not None}
            entry = groups[value] = {"key": value, "captures": captures, "files": []}
        entry["files"].append(_file(root, relative, media_type))
    return {"groups": [groups[value] for value in sorted(groups)]}


def glob(root: str, pattern: str) -> dict[str, list[dict[str, Any]]]:
    """The files whose relative path matches the glob `pattern`, sorted.

    `*` and `?` stay within one path segment, `**` crosses segments and
    `**/` also matches no directory at all.
    """
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise BuiltinError(f"pattern is longer than {MAX_PATTERN_LENGTH} characters")
    regex = re.compile(glob_to_regex(pattern))
    return {
        "files": [
            _file(root, relative, None) for relative in _list(root) if regex.fullmatch(relative)
        ]
    }


def glob_to_regex(pattern: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return "".join(parts)


def _compile(pattern: str) -> re.Pattern[str]:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise BuiltinError(f"pattern is longer than {MAX_PATTERN_LENGTH} characters")
    try:
        return re.compile(pattern)
    except re.error as exc:
        message = f"pattern {pattern!r} is not a valid regular expression: {exc}"
        raise BuiltinError(message) from None


def _list(root: str) -> list[str]:
    try:
        return storage.for_uri(root).list(root)
    except storage.StorageError as exc:
        raise BuiltinError(f"root: {exc}") from None
    except OSError as exc:
        raise BuiltinError(f"cannot list {root}: {exc.strerror or exc}") from None


def _file(root: str, relative: str, media_type: str | None) -> dict[str, Any]:
    file: dict[str, Any] = {"uri": storage.join(root, relative), "kind": "file"}
    if media_type is not None:
        file["media_type"] = media_type
    return file
