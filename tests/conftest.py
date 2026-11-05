"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _audit_to_tmp(tmp_path, monkeypatch):
    # Tool calls are audited; keep test runs out of the real ~/.micropaw/audit.jsonl
    import micropaw.utils.audit as audit
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "audit.jsonl")


class FakeModelServer:
    """A model server on 127.0.0.1: Ollama's /api/tags and /api/show, or /v1/models. Records every request."""

    def __init__(self, kind="ollama", models=("qwen3.6:27b",), key=None):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests: list[tuple[str, str]] = []  # (path, Authorization header)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _gate(self):
                outer.requests.append((self.path, self.headers.get("Authorization", "")))
                if key and self.headers.get("Authorization") != f"Bearer {key}":
                    self._send(401, {"error": "no key"})
                    return False
                return True

            def do_GET(self):
                if not self._gate():
                    return
                if self.path == "/api/tags" and kind == "ollama":
                    self._send(200, {"models": [{"name": m, "size": 5_000_000_000} for m in models]})
                elif self.path == "/v1/models" and kind in ("openai", "ollama"):
                    self._send(200, {"data": [{"id": m} for m in models]})
                else:
                    self._send(404, {})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self._gate():
                    self._send(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def model_server():
    made: list[FakeModelServer] = []

    def make(**kw) -> FakeModelServer:
        made.append(FakeModelServer(**kw))
        return made[-1]

    yield make
    for m in made:
        m.close()


@pytest.fixture
def lan_home(tmp_path, monkeypatch):
    """An empty MicroPaw home: config writes land in tmp_path, and the environment has no OLLAMA_HOST."""
    import micropaw.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    return tmp_path / "config.json"
