from typing import Any

import pytest

from glow.expressions import Evaluator, ExpressionEvalError

ENV = {
    "uri": "s3://bucket/runs/r1/out.tif",
    "cog": "image/tiff; application=geotiff; profile=cloud-optimized",
    "n": 3,
}


def evaluate(expression: str) -> Any:
    return Evaluator(ENV).eval(expression)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("path.basename(uri)", "out.tif"),
        ("path.dirname(uri)", "s3://bucket/runs/r1"),
        ("path.dirname('s3://bucket/raw/')", "s3://bucket"),
        ("path.dirname('name.tif')", ""),
        ("path.stem(uri)", "out"),
        ("path.ext(uri)", ".tif"),
        ("path.ext('noext')", ""),
        ("path.join('', 'a.tif')", "a.tif"),
        ("path.join('s3://b/', '/a.tif')", "s3://b/a.tif"),
        ("media.base(cog)", "image/tiff"),
        ("media.base('*')", "*"),
        ("media.param(cog, 'profile')", "cloud-optimized"),
        ("media.param(cog, 'Application')", "geotiff"),
        ("media.param('image/png', 'profile')", ""),
        ("media.accepts('image/tiff; application=geotiff', cog)", True),
        ("media.accepts(['image/png', 'image/tiff'], cog)", True),
        ("media.accepts(['image/png'], cog)", False),
        ("media.accepts('*', 'text/plain')", True),
        ("media.matches(cog, 'image/tiff')", True),
        ("media.ext('image/jpeg; quality=high')", "jpg"),
        ("date('2024-01-05T10:20:30', '%Y-%m-%dT%H:%M:%S')", "2024-01-05T10:20:30Z"),
        ("date('5%', '%d%%')", "1900-01-05T00:00:00Z"),
        ("date('20240105', '%Y%m%d').getFullYear()", 2024),
        # `matches` is still the standard regular-expression method on strings.
        ("uri.matches('[.]tif$')", True),
        ("uri.matches('[.]png$')", False),
    ],
)
def test_function(expression: str, expected: Any) -> None:
    assert evaluate(expression) == expected


@pytest.mark.parametrize(
    ("expression", "error"),
    [
        ("path.basename(n)", "no such overload: path.basename"),
        ("path.join(uri)", "no such overload: path.join"),
        ("uri.basename()", "no such overload"),
        ("path.nope(uri)", "undeclared reference"),
        ("media.base('image/*')", "not a media type"),
        ("media.accepts([1], cog)", "no such overload"),
        ("media.ext('nonsense')", "no extension known"),
        ("date('2024-13-01', '%Y-%m-%d')", "not a valid date"),
        ("date('2023-366', '%Y-%j')", "not a valid date"),
        ("date('2024', '%Y%')", "ends with %"),
        ("date('2024', '%y')", "unsupported directive %y"),
        ("date('2024x', '%Y')", "has text after format"),
        ("date('x', '%Y')", "does not match format"),
        ("date(1, '%Y')", "no such overload: date"),
    ],
)
def test_function_error(expression: str, error: str) -> None:
    with pytest.raises(ExpressionEvalError, match=error):
        evaluate(expression)
