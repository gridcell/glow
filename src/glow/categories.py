"""Media type categories for data inputs.

A tool input may accept a category such as `raster` instead of listing every
format. A category is a named set of media types; a format with two roles,
such as GeoPackage, is in both. Matching is unchanged: an input with a
category accepts the union of its `media_type` and the category's media types.

Add a media type to a category when a tool that accepts the category can read
it. This module is pure data with no glow imports, so the models, the type
module and the compiler can all use it.
"""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any


class Category(StrEnum):
    RASTER = "raster"
    VECTOR = "vector"


GEOPACKAGE = "application/geopackage+sqlite3"

CATEGORY_MEDIA_TYPES: Mapping[str, tuple[str, ...]] = {
    Category.RASTER: (
        "image/tiff; application=geotiff",
        "image/jp2",
        "application/x-netcdf",
        "application/x-hdf5",
        "application/vnd.gdal.vrt+xml",
        GEOPACKAGE,
    ),
    Category.VECTOR: (
        "application/geo+json",
        "application/vnd.flatgeobuf",
        GEOPACKAGE,
    ),
}


def as_list(value: str | Iterable[str] | None) -> list[str]:
    """A `media_type` or `category` value as a list: one string, a list, or none."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def expand(
    media_type: str | Iterable[str] | None, category: str | Iterable[str] | None
) -> list[str]:
    """The media types an input accepts: its own, then each category's, without repeats.

    Raises `ValueError` for an unknown category.
    """
    accepted = as_list(media_type)
    for name in as_list(category):
        if name not in CATEGORY_MEDIA_TYPES:
            raise ValueError(f"unknown category '{name}'")
        accepted.extend(CATEGORY_MEDIA_TYPES[name])
    return list(dict.fromkeys(accepted))


def expand_declaration(decl: Mapping[str, Any]) -> dict[str, Any]:
    """An input declaration with `category` folded into `media_type`.

    glow-exec only knows `media_type`, so the compiler hands it the expanded
    list. Nested `items`, `properties` and `additionalProperties` are expanded
    too. A declaration without a category is returned as a copy.
    """
    result = dict(decl)
    if "category" in result:
        result["media_type"] = expand(result.get("media_type"), result.pop("category"))
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), Mapping):
            result[key] = expand_declaration(result[key])
    if isinstance(result.get("properties"), Mapping):
        result["properties"] = {
            name: expand_declaration(prop) for name, prop in result["properties"].items()
        }
    return result
