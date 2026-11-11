import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

from pipeline.config import load_settings  # noqa: E402
from pipeline.database import connect  # noqa: E402
from pipeline.scraper_base import PoliteClient, Response  # noqa: E402


class ScriptedTransport:
    """Returns queued responses per URL (last one repeats); a response may be an Exception to raise."""

    def __init__(self, script: dict):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[str] = []

    def send(self, url, headers, timeout):
        self.calls.append(url)
        q = self.script.get(url)
        if not q:
            return Response(404, {"content-type": "text/plain"}, "")
        item = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(item, Exception):
            raise item
        return item


def json_resp(obj, status=200):
    return Response(status, {"content-type": "application/json"}, json.dumps(obj))


def html_resp(text, status=200, headers=None):
    return Response(status, {"content-type": "text/html", **(headers or {})}, text)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    yield c
    c.close()


@pytest.fixture
def make_client(conn, tmp_path):
    def _make(transport, **kw):
        sleeps = []
        kw.setdefault("min_interval_s", 1.0)
        client = PoliteClient(transport, conn, tmp_path, "test-agent", sleep=sleeps.append, max_retries=3, **kw)
        client.sleeps = sleeps
        return client
    return _make


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("MIN_INTERVAL_S", "0")
    return load_settings()
