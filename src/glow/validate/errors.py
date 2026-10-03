"""Validation error codes and the Appendix B message format.

Every error has a stable code and a location such as `thumb.with.source`.
Edge errors render the plan's Appendix B layout:

    error: thumb.with.source [GLOW-E030]
      expects: file[image/tiff; application=geotiff | image/jp2]
      got:     array<group>  from ${{ steps.items.outputs.groups }}
      hint:    for_each over the array, or .map(...) to extract one value per member

The message is the API an author or agent uses to self-correct, so the codes
and the first line stay stable.
"""

from dataclasses import dataclass
from enum import StrEnum

MAX_TEXT_CHARS = 200


class Code(StrEnum):
    UNKNOWN_TOOL = "GLOW-E001"
    UNKNOWN_WITH_KEY = "GLOW-E002"
    MISSING_REQUIRED_INPUT = "GLOW-E003"
    INVALID_MEDIA_TYPE = "GLOW-E004"
    MALFORMED_EXPRESSION = "GLOW-E005"
    UNDEFINED_NAME = "GLOW-E010"
    LATER_STEP = "GLOW-E011"
    BLOCK_MEMBER = "GLOW-E012"
    UNDECLARED_OUTPUT = "GLOW-E013"
    CYCLE = "GLOW-E020"
    TYPE_MISMATCH = "GLOW-E030"
    FOR_EACH_NOT_ARRAY = "GLOW-E031"
    IF_NOT_BOOLEAN = "GLOW-E032"
    REGISTRY_UNAVAILABLE = "GLOW-E040"


@dataclass(frozen=True, slots=True)
class GlowError:
    """One validation error.

    `expects` and `got` are rendered types; `source` is the `${{ }}` text the
    value came from. `message` holds any further explanation.
    """

    code: Code
    location: str
    message: str | None = None
    expects: str | None = None
    got: str | None = None
    source: str | None = None
    hint: str | None = None

    def render(self) -> str:
        lines = [f"error: {_printable(self.location)} [{self.code}]"]
        if self.expects is not None:
            lines.append(f"  expects: {_printable(self.expects)}")
        if self.got is not None:
            origin = f"  from {_printable(self.source)}" if self.source else ""
            lines.append(f"  got:     {_printable(self.got)}{origin}")
        if self.message:
            lines.extend(f"  {_printable(line)}" for line in self.message.splitlines())
        if self.hint:
            lines.append(f"  hint:    {_printable(self.hint)}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.render()


def _printable(text: str) -> str:
    """One line of workflow-supplied text, safe to print to a terminal.

    Control characters could rewrite the terminal, so they are replaced, and
    long values are clipped.
    """
    flat = " ".join(text.split())
    safe = "".join(char if char.isprintable() else "?" for char in flat)
    if len(safe) > MAX_TEXT_CHARS:
        return safe[: MAX_TEXT_CHARS - 3] + "..."
    return safe
