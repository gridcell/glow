# glow-stac: stac.item, stac.publish and, until a Prescient image exists,
# prescient.render_from_color_table. The base is pinned by digest.
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# The rasterio wheel bundles GDAL but links the system libexpat, which the
# slim base does not ship.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --only-binary=:all: --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY --chmod=0755 wrappers/ /usr/local/bin/

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin glow
USER 10001
WORKDIR /work
