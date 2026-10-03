"""Find the names an expression refers to.

This is a conservative tokenizer, not a CEL parser. It reports every path
rooted at an identifier, such as `inputs.source`, `steps.cog.outputs.result`,
`g.files[0].path` or a `let` name, and says whether the whole expression is
one such path. Anything else is left for the CEL type checker, so callers
treat a non-path expression as having an unknown type.

`analyze` is the only interface the validator uses, so a CEL-based
implementation can replace this module without touching the validator.
"""

import re
from dataclasses import dataclass

# A path segment: a field name, a constant index, or None for an index that
# is only known at runtime, such as `xs[i]`.
Segment = str | int | None

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"\d[\w.]*")
_INDEX = re.compile(r"\[\s*(\d+)\s*\]")
_STRING_INDEX = re.compile(r"""\[\s*(?:'([^'\\]*)'|"([^"\\]*)")\s*\]""")
_STRING_PREFIX = re.compile(r"(?i:r|b|rb|br)")
_KEYWORDS = frozenset({"true", "false", "null", "in"})
# CEL macros whose first argument names a variable local to the call.
_MACROS = frozenset({"all", "exists", "exists_one", "map", "filter"})


@dataclass(frozen=True, slots=True)
class Reference:
    """One path such as `steps.cog.outputs.result`.

    `root` is the first identifier (`inputs`, `steps`, or a loop or `let`
    name) and `path` the segments after it. `text` is the path as written.
    """

    root: str
    path: tuple[Segment, ...]
    text: str


@dataclass(frozen=True, slots=True)
class Analysis:
    """What an expression refers to.

    `whole_path` is set when the expression is exactly one reference, so its
    type is the type of that path.
    """

    references: tuple[Reference, ...]
    whole_path: Reference | None


def analyze(expression: str) -> Analysis:
    """Analyze the text inside one `${{ ... }}` span."""
    text = expression.strip()
    scanner = _Scanner(text)
    scanner.run()
    references = tuple(ref for ref in scanner.references if ref.root not in scanner.bound)
    whole = None
    if len(references) == 1 and references[0].text == text:
        whole = references[0]
    return Analysis(references, whole)


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.position = 0
        self.references: list[Reference] = []
        self.bound: set[str] = set()

    def run(self) -> None:
        while self.position < len(self.text):
            char = self.text[self.position]
            if char in "'\"":
                self._skip_string(raw=False)
            elif (number := _NUMBER.match(self.text, self.position)) is not None:
                self.position = number.end()
            elif (ident := _IDENT.match(self.text, self.position)) is not None:
                self._identifier(ident)
            else:
                self.position += 1

    def _identifier(self, ident: re.Match[str]) -> None:
        name = ident.group()
        end = ident.end()
        if end < len(self.text) and self.text[end] in "'\"" and _STRING_PREFIX.fullmatch(name):
            self.position = end
            self._skip_string(raw="r" in name.lower())
            return
        if name in _KEYWORDS or self._after_dot(ident.start()) or self._before_call(end):
            self.position = end
            return
        self._path(name, ident.start(), end)

    def _path(self, root: str, start: int, position: int) -> None:
        segments: list[Segment] = []
        while position < len(self.text):
            char = self.text[position]
            if char == ".":
                member = _IDENT.match(self.text, position + 1)
                if member is None:
                    break
                if self._before_call(member.end()):
                    self._bind_macro_variable(member.group(), member.end())
                    break
                segments.append(member.group())
                position = member.end()
            elif (index := _INDEX.match(self.text, position)) is not None:
                segments.append(int(index.group(1)))
                position = index.end()
            elif (key := _STRING_INDEX.match(self.text, position)) is not None:
                segments.append(key.group(1) if key.group(1) is not None else key.group(2))
                position = key.end()
            elif char == "[":
                # The index is an expression: end the path here and scan the
                # index for references of its own.
                self.references.append(
                    Reference(root, (*segments, None), self.text[start:position])
                )
                self.position = position + 1
                return
            else:
                break
        self.references.append(Reference(root, tuple(segments), self.text[start:position]))
        self.position = position

    def _bind_macro_variable(self, method: str, open_paren: int) -> None:
        if method not in _MACROS:
            return
        variable = _IDENT.match(self.text, self._skip_space(open_paren + 1))
        if variable is not None and self.text[self._skip_space(variable.end()) :].startswith(","):
            self.bound.add(variable.group())

    def _skip_string(self, *, raw: bool) -> None:
        quote = self.text[self.position]
        if self.text.startswith(quote * 3, self.position):
            quote *= 3
        position = self.position + len(quote)
        while position < len(self.text):
            if not raw and self.text[position] == "\\":
                position += 2
            elif self.text.startswith(quote, position):
                self.position = position + len(quote)
                return
            else:
                position += 1
        self.position = len(self.text)

    def _skip_space(self, position: int) -> int:
        while position < len(self.text) and self.text[position].isspace():
            position += 1
        return position

    def _after_dot(self, position: int) -> bool:
        position -= 1
        while position >= 0 and self.text[position].isspace():
            position -= 1
        return position >= 0 and self.text[position] == "."

    def _before_call(self, position: int) -> bool:
        position = self._skip_space(position)
        return position < len(self.text) and self.text[position] == "("
