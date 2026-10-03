import binascii

import pytest

from glow.compile.encoding import decode_json, decode_text, encode_json, encode_text


def test_json_round_trip_keeps_expressions_and_key_order() -> None:
    value = {"z": "${{ steps.a.outputs.b }}", "a": [1, {"k": "é"}], "n": None}
    encoded = encode_json(value)
    assert "{{" not in encoded
    assert decode_json(encoded) == value
    assert list(decode_json(encoded)) == ["z", "a", "n"]


def test_text_round_trip() -> None:
    script = "echo '{{ not argo }}'\n"
    assert decode_text(encode_text(script)) == script


def test_decode_rejects_invalid_base64() -> None:
    with pytest.raises(binascii.Error):
        decode_text("not base64!")
