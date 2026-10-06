import pytest

from glow.categories import CATEGORY_MEDIA_TYPES, Category, expand, expand_declaration
from glow.types import MediaType

GEOPACKAGE = "application/geopackage+sqlite3"


def test_every_category_has_media_types_that_parse() -> None:
    assert set(CATEGORY_MEDIA_TYPES) == set(Category)
    for media_types in CATEGORY_MEDIA_TYPES.values():
        assert media_types
        for text in media_types:
            assert str(MediaType.parse(text)) == text


def test_a_format_with_two_roles_is_in_both_categories() -> None:
    assert GEOPACKAGE in CATEGORY_MEDIA_TYPES["raster"]
    assert GEOPACKAGE in CATEGORY_MEDIA_TYPES["vector"]


def test_expand_keeps_own_media_types_first_without_repeats() -> None:
    accepted = expand("image/png", ["raster", "vector"])
    assert accepted[0] == "image/png"
    assert accepted.count(GEOPACKAGE) == 1
    assert "application/geo+json" in accepted
    assert expand(None, None) == []
    assert expand(["a/b", "c/d"], None) == ["a/b", "c/d"]


def test_expand_rejects_an_unknown_category() -> None:
    with pytest.raises(ValueError, match="unknown category 'pointcloud'"):
        expand(None, "pointcloud")


def test_expand_declaration_folds_category_into_media_type() -> None:
    decl = {
        "type": "object",
        "properties": {"scene": {"type": "file", "category": "raster"}},
        "additionalProperties": {"type": "file", "category": "vector", "media_type": "a/b"},
        "items": {"type": "file"},
    }
    result = expand_declaration(decl)
    assert result["properties"]["scene"] == {
        "type": "file",
        "media_type": list(CATEGORY_MEDIA_TYPES["raster"]),
    }
    assert result["additionalProperties"]["media_type"][0] == "a/b"
    assert "category" not in result["additionalProperties"]
    assert result["items"] == {"type": "file"}
    assert "category" in decl["properties"]["scene"], "the input is not changed"
