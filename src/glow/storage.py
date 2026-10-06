"""Read, write and list objects under a local directory or an S3 prefix.

The same URIs glow-exec accepts: an absolute path, a `file://` URI or an
`s3://` URI. Other schemes are refused, so a workflow cannot make the engine
fetch arbitrary URLs. S3 needs the optional `s3` extra (boto3), imported on
first use so that local runs need no AWS setup. As in glow-exec,
`AWS_ENDPOINT_URL_S3` selects another endpoint and `GLOW_S3_PATH_STYLE=true`
turns on path-style addressing.
"""

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

# Bounds every S3 request, so a stalled endpoint cannot hang a run.
S3_CONNECT_TIMEOUT = 10
S3_READ_TIMEOUT = 60
S3_MAX_ATTEMPTS = 5


class StorageError(ValueError):
    """Raised for an unsupported URI or a failed storage operation."""


class Store(Protocol):
    def list(self, uri: str) -> list[str]:
        """Slash-separated paths, relative to `uri`, of every object under it, sorted."""
        ...

    def write_text(self, uri: str, text: str) -> None: ...

    def read_text(self, uri: str) -> str: ...


def join(prefix: str, *parts: str) -> str:
    """`prefix` and `parts` with exactly one `/` between them, as glow-exec joins."""
    out = prefix.rstrip("/")
    for part in parts:
        out += "/" + part.strip("/")
    return out


def is_s3(uri: str) -> bool:
    return uri.startswith("s3://")


def local_path(uri: str) -> Path:
    """The path of a local URI. Raises `StorageError` for anything else."""
    path = uri.removeprefix("file://")
    if not os.path.isabs(path):
        raise StorageError(f"{uri!r} is not an absolute path, file:// URI or s3:// URI")
    return Path(os.path.normpath(path))


def for_uri(uri: str) -> Store:
    if is_s3(uri):
        return S3Store()
    local_path(uri)
    return LocalStore()


class LocalStore:
    def list(self, uri: str) -> list[str]:
        root = local_path(uri)
        if not root.is_dir():
            raise StorageError(f"{uri} is not a directory")
        return sorted(_walk(root, root))

    def write_text(self, uri: str, text: str) -> None:
        path = local_path(uri)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Through a temporary file, so a reader never sees a partial document.
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8"
        ) as tmp:
            tmp.write(text)
        os.chmod(tmp.name, 0o644)
        os.replace(tmp.name, path)

    def read_text(self, uri: str) -> str:
        return local_path(uri).read_text(encoding="utf-8")


def _walk(root: Path, directory: Path) -> Iterator[str]:
    # Symbolic links are skipped so a listing never leaves the directory.
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                yield from _walk(root, Path(entry.path))
            elif entry.is_file():
                yield Path(entry.path).relative_to(root).as_posix()


class S3Store:
    def __init__(self, client: Any = None) -> None:
        self._client = client if client is not None else _s3_client()

    def list(self, uri: str) -> list[str]:
        bucket, key = _split_s3(uri)
        prefix = key.rstrip("/") + "/" if key else ""
        paths = []
        with _s3_errors(uri):
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    relative = item["Key"][len(prefix) :]
                    # Keys ending in `/` are folder markers that some tools create.
                    if relative and not relative.endswith("/"):
                        paths.append(relative)
        return sorted(paths)

    def write_text(self, uri: str, text: str) -> None:
        bucket, key = _split_s3(uri)
        with _s3_errors(uri):
            self._client.put_object(
                Bucket=bucket, Key=key, Body=text.encode("utf-8"), ContentType="application/json"
            )

    def read_text(self, uri: str) -> str:
        bucket, key = _split_s3(uri)
        with _s3_errors(uri):
            response = self._client.get_object(Bucket=bucket, Key=key)
            return response["Body"].read().decode("utf-8")


@contextlib.contextmanager
def _s3_errors(uri: str) -> Iterator[None]:
    """Report botocore failures as `StorageError`, without the request details."""
    try:
        yield
    except Exception as exc:
        if not type(exc).__module__.startswith("botocore"):
            raise
        raise StorageError(f"{uri}: {exc}") from None


def _split_s3(uri: str) -> tuple[str, str]:
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    if not bucket:
        raise StorageError(f"{uri!r} has no bucket")
    return bucket, key


def _s3_client() -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise StorageError(
            "s3:// URIs need the s3 extra; install it with `uv sync --extra s3`"
        ) from exc
    path_style = os.environ.get("GLOW_S3_PATH_STYLE", "").lower() == "true"
    config = Config(
        connect_timeout=S3_CONNECT_TIMEOUT,
        read_timeout=S3_READ_TIMEOUT,
        retries={"max_attempts": S3_MAX_ATTEMPTS, "mode": "standard"},
        s3={"addressing_style": "path" if path_style else "auto"},
    )
    return boto3.client("s3", config=config)
