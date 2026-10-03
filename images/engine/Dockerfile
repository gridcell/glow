# glow-engine: the glow package, which runs built-in steps in a cluster. The
# compiler starts it as `/glow/exec run --tool <name>@1 -- glow-builtin <name>`:
# glow-exec stages the inputs, `glow-builtin` reads /work/inputs.json and
# writes /work/outputs.json, and glow-exec collects the outputs.
#
# Build from the repository root, which this file's .dockerignore trims:
#   docker build -f images/engine/Dockerfile -t local/engine:dev .
# The Python base is pinned by digest; dependencies come from uv.lock.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/glow
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra s3 --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --extra s3 --no-editable

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
COPY --from=build /opt/glow /opt/glow
ENV PATH=/opt/glow/bin:$PATH
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin glow
USER 10001
WORKDIR /work
ENTRYPOINT ["glow", "builtin"]
