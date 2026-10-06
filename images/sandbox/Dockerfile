# glow-sandbox: Python and bash for inline `run` (bash) and `script` (python3)
# steps. The step's script is written to /work/script and runs under
# glow-exec, which validates the declared outputs. The compiled pod decodes
# the script with `sh` and `base64`, which the base image provides.
# The base is pinned by digest.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin glow
USER 10001
WORKDIR /work
