"""Locate `${{ ... }}` expression spans in string values.

Expressions are opaque at this stage: this module only finds the spans and
returns their inner text. Parsing and type checking happen in a later pass.
"""

from dataclasses import dataclass

OPEN = "${{"
CLOSE = "}}"


class ExpressionSyntaxError(ValueError):
    """Raised when a `${{` has no matching `}}`."""


@dataclass(frozen=True, slots=True)
class ExpressionSpan:
    """One `${{ ... }}` occurrence.

    `start` and `end` are offsets into the original string, with `end`
    exclusive, so `value[start:end]` is the full `${{ ... }}` text.
    `inner` is the text between the delimiters with surrounding whitespace
    removed.
    """

    start: int
    end: int
    inner: str


def find_expressions(value: str) -> list[ExpressionSpan]:
    """Return every `${{ ... }}` span in `value`, in order of appearance.

    The first `}}` after an opening `${{` closes the span. A `}}` inside a
    quoted string literal in the expression therefore ends it early; that
    case is left to the expression parser.
    """
    spans: list[ExpressionSpan] = []
    position = 0
    while (start := value.find(OPEN, position)) != -1:
        inner_start = start + len(OPEN)
        close = value.find(CLOSE, inner_start)
        if close == -1:
            raise ExpressionSyntaxError(f"unterminated expression starting at offset {start}")
        end = close + len(CLOSE)
        spans.append(ExpressionSpan(start=start, end=end, inner=value[inner_start:close].strip()))
        position = end
    return spans
