import pytest

from glow.compile.naming import MAX_NAME_CHARS, NameTable, argo_name
from glow.validate import Code


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("per_item", "per-item"),
        ("Cog", "cog"),
        ("gdal.dem.color_relief", "gdal-dem-color-relief"),
        ("_private_", "private"),
        ("a$b c", "a-b-c"),
    ],
)
def test_argo_name(text: str, expected: str) -> None:
    assert argo_name(text) == expected


def test_same_owner_may_claim_twice() -> None:
    table = NameTable("task")
    assert table.claim("cog", "cog") == "cog"
    assert table.claim("cog", "cog") == "cog"
    assert table.errors == []


def test_collision_names_both_owners() -> None:
    table = NameTable("task")
    table.claim("cog", "cog")
    table.claim("cog", "Cog")
    (error,) = table.errors
    assert error.code == Code.NAME_COLLISION
    assert error.location == "Cog"
    assert "'Cog' and 'cog' both become Argo task 'cog'" in error.render()


@pytest.mark.parametrize("name", ["", "x" * (MAX_NAME_CHARS + 1)])
def test_unusable_names_are_errors(name: str) -> None:
    table = NameTable("template")
    table.claim(name, "owner")
    assert [error.code for error in table.errors] == [Code.NAME_COLLISION]
