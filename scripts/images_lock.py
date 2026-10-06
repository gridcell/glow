"""Pin each toolpack manifest to its pushed image digest and regenerate the lock.

Run through `make images-lock REGISTRY=<registry>` after `make images` and
`make images-push`. The lock is derived from the manifests, so the digest is
written to each manifest's `image:` line and `glow toolpack lock` runs after.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from glow.lock import write_lock

TOOLPACKS = Path(__file__).resolve().parent.parent / "toolpacks"
# Toolpack -> image repository name. prescient ships in the stac image for now.
REPOSITORIES = {"gdal": "glow-gdal", "stac": "glow-stac", "prescient": "glow-stac"}
REGISTRY = re.compile(r"^[a-z0-9][a-z0-9._:/-]*[a-z0-9]$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_LINE = re.compile(r"^image:.*$", re.MULTILINE)


def pushed_digest(docker: str, repository: str) -> str:
    """The registry digest of `<repository>:dev`, known once the tag is pushed."""
    result = subprocess.run(
        [docker, "image", "inspect", "--format", "{{json .RepoDigests}}", f"{repository}:dev"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        sys.exit(f"cannot inspect {repository}:dev; run make images-push first\n{result.stderr}")
    for reference in json.loads(result.stdout) or []:
        name, _, digest = reference.partition("@")
        if name == repository and DIGEST.match(digest):
            return digest
    sys.exit(f"{repository}:dev has no pushed digest; run make images-push first")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True, help="For example ghcr.io/sparkgeo.")
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    if not REGISTRY.match(args.registry):
        sys.exit(f"invalid registry {args.registry!r}")
    for toolpack, name in REPOSITORIES.items():
        repository = f"{args.registry}/{name}"
        reference = f"{repository}@{pushed_digest(args.docker, repository)}"
        path = TOOLPACKS / toolpack / "manifest.yaml"
        text, count = IMAGE_LINE.subn(f"image: {reference}", path.read_text(), count=1)
        if count != 1:
            sys.exit(f"{path}: no top-level image: line")
        path.write_text(text)
        print(f"{path}: image: {reference}")
    print(f"wrote {write_lock(TOOLPACKS)}")


if __name__ == "__main__":
    main()
