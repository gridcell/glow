# glow-gdal: gdal.translate, gdal.dem.color_relief and gdal.info.
# ubuntu-full, not ubuntu-small, because the SST source data is netCDF and
# only the full image has the netCDF driver. The base is pinned by digest.
FROM ghcr.io/osgeo/gdal:ubuntu-full-3.13.3@sha256:2dd0f81ef927ff4c3d4dbe4f73c029dc86d4073974c0564f8e196f6e1412e2e0

COPY --chmod=0755 wrappers/ /usr/local/bin/

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin glow
USER 10001
WORKDIR /work
