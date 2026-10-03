"""Find the names an expression refers to.

`analyze` parses the expression with CEL (`cel.py`) and reports every path
rooted at an identifier the expression does not bind itself, such as
`inputs.source`, `steps.cog.outputs.result`, `g.files[0].path` or a `let`
name. A macro variable like `i` in `xs.map(i, i.name)` is not a reference,
and neither is the receiver of `path.basename(x)` or `media.ext(x)`.
"""

from dataclasses import dataclass, field

import lark

from glow.expressions.cel import Reference, Segment, chain, collect, parse, source_text


@dataclass(frozen=True, slots=True)
class Analysis:
    """What an expression refers to.

    `whole_path` is set when the expression is exactly one reference with
    constant segments, so its type is the type of that path. `tree` is the
    parsed `expression`, for the type checker; its positions index into
    `expression`.
    """

    references: tuple[Reference, ...]
    whole_path: Reference | None
    tree: lark.Tree | None = field(default=None, compare=False, repr=False)
    expression: str = field(default="", compare=False, repr=False)


def analyze(expression: str) -> Analysis:
    """Analyze the text inside one `${{ ... }}` span.

    Raises `ExpressionSyntaxError` when it is not valid CEL and
    `ExpressionTooCostlyError` when it is over the cost limit.
    """
    text = expression.strip()
    tree = parse(text)
    references = tuple(collect(tree, text))
    whole = None
    found = chain(tree)
    if found is not None and not found.dynamic and len(references) == 1:
        reference = references[0]
        if source_text(found.node, text) == reference.text:
            whole = reference
    return Analysis(references, whole, tree, text)


__all__ = ["Analysis", "Reference", "Segment", "analyze"]
