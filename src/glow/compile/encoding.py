"""Base64 encoding of values the compiler embeds in Argo parameters.

Argo reads every `{{ ... }}` in a parameter as one of its own template tags
and rejects tags it cannot resolve, so a `with` block holding `${{ }}`
expressions, a tool spec or a script cannot travel as plain text.
"""

import base64
import json
from typing import Any


def encode_json(value: Any) -> str:
    """Base64 of `value` as compact JSON, keys in their written order."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return encode_text(text)


def decode_json(encoded: str) -> Any:
    return json.loads(decode_text(encoded))


def encode_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_text(encoded: str) -> str:
    return base64.b64decode(encoded, validate=True).decode("utf-8")
