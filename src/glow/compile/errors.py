"""The error the compiler raises.

Compile errors use the same `GlowError` format and codes as validation, so an
author sees one kind of message whichever stage rejects the workflow.
"""

from glow.validate.errors import GlowError


class CompileError(Exception):
    """The workflow is valid but cannot be compiled. `errors` lists every reason."""

    def __init__(self, errors: list[GlowError]) -> None:
        super().__init__("\n".join(error.render() for error in errors))
        self.errors = errors
