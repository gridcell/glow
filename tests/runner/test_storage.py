"""Local storage, and S3 against a real bucket when GLOW_TEST_S3_BUCKET is set."""

import os
import secrets
from pathlib import Path

import pytest

from glow import storage


def test_join_uses_one_slash() -> None:
    assert storage.join("s3://b/run/", "/steps/", "x") == "s3://b/run/steps/x"
    assert storage.join("/tmp/a", "b") == "/tmp/a/b"


def test_local_round_trip(tmp_path: Path) -> None:
    store = storage.for_uri(str(tmp_path))
    uri = storage.join(f"file://{tmp_path}", "a", "b.json")
    store.write_text(uri, "{}")
    assert store.read_text(uri) == "{}"
    assert store.list(str(tmp_path)) == ["a/b.json"]
    assert (tmp_path / "a" / "b.json").stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("uri", ["relative/path", "http://example.com/x", "gs://b/x"])
def test_unsupported_uris(uri: str) -> None:
    with pytest.raises(storage.StorageError):
        storage.for_uri(uri)


@pytest.mark.skipif(
    not os.environ.get("GLOW_TEST_S3_BUCKET"), reason="GLOW_TEST_S3_BUCKET is not set"
)
def test_s3_round_trip() -> None:
    pytest.importorskip("boto3")
    prefix = f"s3://{os.environ['GLOW_TEST_S3_BUCKET']}/glow-test-{secrets.token_hex(4)}"
    store = storage.for_uri(prefix)
    store.write_text(storage.join(prefix, "x", "sst_20240101.nc"), "a")
    store.write_text(storage.join(prefix, "y.txt"), "b")
    assert store.list(prefix) == ["x/sst_20240101.nc", "y.txt"]
    assert store.read_text(storage.join(prefix, "y.txt")) == "b"
