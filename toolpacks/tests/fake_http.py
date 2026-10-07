"""Run a wrapper with urllib.request.urlopen replaced by canned responses.

Usage: python3 fake_http.py <wrapper> <responses.json> <calls.json>

Each urlopen call takes the next entry of the responses file: a JSON value
is returned as the body, a `{"text": ...}` entry as text, and a
`{"status": <code>}` entry raises HTTPError. The calls, with their URL and
JSON body, are written to the calls file. The wrapper runs without network.
"""

import importlib.machinery
import importlib.util
import io
import json
import sys
import urllib.error
import urllib.request
from typing import Any

wrapper, responses_path, calls_path = sys.argv[1:4]
with open(responses_path) as source:
    responses = json.load(source)
calls: list[dict[str, Any]] = []


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str) -> None:
        super().__init__(body)
        self.url = url

    def geturl(self) -> str:
        return self.url


def urlopen(request: Any, timeout: float | None = None) -> Response:
    url = request if isinstance(request, str) else request.full_url
    data = None if isinstance(request, str) else request.data
    calls.append({"url": url, "body": json.loads(data) if data else None})
    response = responses[len(calls) - 1]
    if isinstance(response, dict) and "status" in response:
        raise urllib.error.HTTPError(url, response["status"], "fake", {}, io.BytesIO(b"fake"))
    if isinstance(response, dict) and "text" in response:
        return Response(response["text"].encode(), url)
    return Response(json.dumps(response).encode(), url)


urllib.request.urlopen = urlopen
loader = importlib.machinery.SourceFileLoader("wrapper", wrapper)
spec = importlib.util.spec_from_loader("wrapper", loader)
assert spec is not None
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
if hasattr(module, "time"):
    # No back-off waits between retries.
    module.time.sleep = lambda seconds: None
try:
    module.main()
finally:
    with open(calls_path, "w") as out:
        json.dump(calls, out)
