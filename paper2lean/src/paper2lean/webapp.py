from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .web_api import analyze_request, history_request, llm_check_request, run_request, statistics_request, status_request
from .runtime import ActivityRegistry


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = PACKAGE_ROOT.parent
STATIC = PACKAGE_ROOT / "static"
DEFAULT_OUTPUT = Path(os.environ.get("PAPER2LEAN_OUTPUT", str(WORK_ROOT / "paper_runs")))
MAX_UPLOAD = 50 * 1024 * 1024
ACTIVITY = ActivityRegistry()


def _json(handler: BaseHTTPRequestHandler, status: int, value: object) -> None:
    raw = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0 or length > MAX_UPLOAD:
        raise ValueError("Request body is empty or exceeds 50 MiB.")
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _save_upload(name: str, content: bytes) -> Path:
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".tex", ".latex"}:
        raise ValueError("Upload must be a PDF or TeX file.")
    folder = DEFAULT_OUTPUT / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{uuid.uuid4().hex}{suffix}"
    target.write_bytes(content)
    return target


class Handler(BaseHTTPRequestHandler):
    server_version = "Paper2Lean/0.1"

    def log_message(self, format: str, *args) -> None:
        print("[paper2lean] " + format % args, flush=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._file("index.html")
        if parsed.path.startswith("/static/"):
            return self._file(parsed.path.removeprefix("/static/"))
        if parsed.path == "/api/health":
            return _json(self, 200, {"ok": True, "application": "paper2lean", "llm_backend": os.environ.get("LLM_BACKEND", "http"), "llm_url": os.environ.get("LLM_HTTP_URL", ""), "llm_model": os.environ.get("LLM_HTTP_MODEL", "default"), "llm_configured": bool(os.environ.get("LLM_HTTP_URL")), "llm_source": "environment"})
        if parsed.path == "/api/history":
            return _json(self, 200, history_request(output_dir=DEFAULT_OUTPUT))
        if parsed.path == "/api/statistics":
            return _json(self, 200, statistics_request(output_dir=DEFAULT_OUTPUT))
        if parsed.path == "/api/activity":
            request_id = parse_qs(parsed.query).get("request_id", [""])[0]
            return _json(self, 200, ACTIVITY.snapshot(request_id))
        if parsed.path == "/api/paper2lean/status":
            args = parse_qs(parsed.query)
            try:
                return _json(self, 200, status_request({"paper_id": args.get("paper_id", [""])[0]}, output_dir=DEFAULT_OUTPUT))
            except Exception as exc:
                return _json(self, 400, {"error": str(exc)})
        if parsed.path == "/api/status":
            args = parse_qs(parsed.query)
            try:
                return _json(self, 200, status_request({"paper_id": args.get("paper_id", [""])[0]}, output_dir=DEFAULT_OUTPUT))
            except Exception as exc:
                return _json(self, 400, {"error": str(exc)})
        self.send_error(404)

    def _file(self, name: str) -> None:
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        try:
            if self.path == "/api/analyze":
                return self._execute(_body(self), "analysis", analyze_request)
            if self.path == "/api/run":
                return self._run()
            if self.path == "/api/upload":
                return self._upload()
            if self.path == "/api/llm/check":
                return _json(self, 200, llm_check_request(_body(self)))
            self.send_error(404)
        except Exception as exc:
            _json(self, 400, {"error": str(exc)})

    def _run(self) -> None:
        payload = _body(self)
        paper_id = payload.get("paper_id")
        if not paper_id:
            raise ValueError("paper_id is required")
        self._execute(payload, "formalization", run_request)

    def _execute(self, payload: dict, phase: str, operation) -> None:
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
            raise ValueError("Invalid request_id")
        ACTIVITY.start(request_id, phase)

        def progress(event):
            if event.get("paper_id"):
                ACTIVITY.claim(request_id, event["paper_id"])
            ACTIVITY.event(request_id, event)

        try:
            if phase == "formalization":
                paper_id = str(payload.get("paper_id", ""))
                if not re.fullmatch(r"[a-f0-9]{16}", paper_id):
                    raise ValueError("Invalid paper_id")
                ACTIVITY.claim(request_id, paper_id)
            progress({"time": time.time(), "stage": "ingest" if phase == "analysis" else "formalization",
                      "status": "running", "backend": "local", "detail": "Request started"})
            result = operation(payload, output_dir=DEFAULT_OUTPUT, progress=progress)
        except Exception as exc:
            ACTIVITY.finish(request_id, str(exc))
            raise
        ACTIVITY.finish(request_id)
        try:
            _json(self, 200, result)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.log_message("Client disconnected; %s results saved.", phase)

    def _upload(self) -> None:
        payload = _body(self)
        name = str(payload.get("name") or "paper.tex")
        content = payload.get("content_base64")
        if not content:
            raise ValueError("content_base64 is required.")
        import base64
        path = _save_upload(name, base64.b64decode(content))
        payload["path"] = str(path)
        return self._execute(payload, "analysis", analyze_request)


def main(host: str | None = None, port: int | None = None, *, open_browser: bool = True) -> int:
    host = host or os.environ.get("PAPER2LEAN_HOST", "127.0.0.1")
    port = int(port or os.environ.get("PAPER2LEAN_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Paper2Lean independent web UI: {url}", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
