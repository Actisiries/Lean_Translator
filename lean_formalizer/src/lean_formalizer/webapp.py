#!/usr/bin/env python3
"""
Web UI for the NL/PDF → Lean 4 formalization pipeline.

Pure Python standard library — no Flask/FastAPI required.
Opens directly in the browser.

  python run_web.py
  # → http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
import uuid
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from lean_formalizer.pipeline import FormalizationPipeline
from lean_formalizer.llm import (
    get_llm,
    LLMClient,
    QuotaExceededError,
    LLMTimeoutError,
    probe_http_endpoint,
)
from lean_formalizer.memory import MemoryBank
from lean_formalizer.lean_verify import lean_available, lean_project_dir, project_index_summary
from lean_formalizer import autoconfig as _autoconfig
from lean_formalizer import __version__

STATIC_DIR = _HERE / "static"
DEFAULT_HOST = os.environ.get("WEB_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WEB_PORT", "8765"))
MEMORY_DIR = os.environ.get("MEMORY_DIR", str(Path.cwd() / "memory"))
# Repo root: package lives in src/lean_formalizer → parents[2] is project root
ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "run_web.py").exists():
    ROOT = Path.cwd()

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
# Candidate source files are shared inside <LEAN_PROJECT>/FormalizerScratch.
# Generation may overlap, but Lean verification must own these files exclusively.
_verification_lock = threading.Lock()
# Ring buffer of recent formalization summaries (this server process only)
_HISTORY_MAX = 30
_history: list[dict[str, Any]] = []
_history_lock = threading.Lock()


def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _summarize_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """Build a compact history entry from a finished job."""
    from datetime import datetime, timezone

    result = job.get("result") or {}
    structured = result.get("structured") or {}
    usage = result.get("token_usage") or {}
    ver = result.get("verification") or {}
    stage_trace = result.get("stage_trace") or []
    failed_stages = [
        s for s in stage_trace if s.get("ok") is False
    ]
    claim = (
        (structured.get("main_theorem") or "")
        or (result.get("message") or "")
        or ""
    )
    status = job.get("status") or "unknown"
    outcome = "error"
    if status == "error":
        outcome = "error"
    elif result.get("timed_out") or result.get("quota_exceeded"):
        outcome = "timeout" if result.get("timed_out") else "quota"
    elif result.get("success"):
        outcome = "verified"
    elif status == "done":
        outcome = "failed"
    elif status == "running":
        outcome = "running"

    return {
        "job_id": job_id,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "outcome": outcome,
        "success": bool(result.get("success")),
        "timed_out": bool(result.get("timed_out")),
        "quota_exceeded": bool(result.get("quota_exceeded")),
        "domain": result.get("domain") or structured.get("domain") or "general",
        "difficulty": structured.get("difficulty") or "",
        "claim": (claim or "")[:220],
        "message": (result.get("message") or job.get("error") or "")[:300],
        "code_len": len((result.get("best_code") or "").strip()),
        "has_code": bool((result.get("best_code") or "").strip()),
        "repair_count": result.get("repair_count") or 0,
        "n_candidates": len(result.get("candidates") or []),
        "n_stages": len(stage_trace),
        "failed_stages": [
            {
                "stage": s.get("stage"),
                "error": (s.get("error") or s.get("preview") or "")[:120],
                "len": s.get("len"),
            }
            for s in failed_stages[:8]
        ],
        "stage_summary": [
            {
                "stage": s.get("stage"),
                "ok": s.get("ok", True),
                "len": s.get("len"),
            }
            for s in stage_trace[:20]
        ],
        "tokens": {
            "prompt": usage.get("prompt_tokens") or 0,
            "completion": usage.get("completion_tokens") or 0,
            "total": usage.get("total_tokens") or 0,
            "calls": usage.get("calls") or 0,
        },
        "verification_summary": (ver.get("summary") or "")[:200],
        "error": (job.get("error") or "")[:300],
    }


def _push_history(job_id: str, job: dict[str, Any]) -> None:
    """Append a summary of a finished job to the ring buffer."""
    if job.get("status") not in ("done", "error"):
        return
    entry = _summarize_job(job_id, job)
    with _history_lock:
        _history.append(entry)
        while len(_history) > _HISTORY_MAX:
            _history.pop(0)


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _html_response(handler: BaseHTTPRequestHandler, html: str, status: int = 200) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _configured_backend(data: dict | None = None) -> str:
    """Choose HTTP only when a usable endpoint was supplied/configured."""
    data = data or {}
    requested = (data.get("llm_backend") or "").strip().lower()
    # A submitted form can explicitly clear its endpoint.  Do not silently
    # reuse a server HTTP endpoint in that case: the requested HTTP run must
    # visibly fall back to mock rather than contacting an unexpected API.
    request_has_http_url = "llm_http_url" in data
    http_url = (
        data.get("llm_http_url", "")
        if request_has_http_url
        else os.environ.get("LLM_HTTP_URL", "")
    ).strip()
    if requested == "http" and not http_url:
        return "mock"
    if requested:
        return requested
    env_backend = (os.environ.get("LLM_BACKEND") or "").strip().lower()
    if env_backend == "http" and not http_url:
        return "mock"
    if env_backend:
        return env_backend
    return "http" if http_url else "mock"


def _http_connection_config(data: dict | None = None) -> tuple[str, str]:
    """Return the effective HTTP endpoint and optional key for a request."""
    data = data or {}
    if "llm_http_url" in data:
        return (
            str(data.get("llm_http_url") or "").strip(),
            str(data.get("llm_http_key") or "").strip(),
        )
    return (
        (os.environ.get("LLM_HTTP_URL") or "").strip(),
        (os.environ.get("LLM_HTTP_KEY") or "").strip(),
    )


def _backend_status(data: dict | None = None) -> dict[str, Any]:
    """Resolve the usable backend without submitting an LLM completion."""
    data = data or {}
    configured = _configured_backend(data)
    requested = (data.get("llm_backend") or "").strip().lower()
    server_http_configured = bool((os.environ.get("LLM_HTTP_URL") or "").strip())
    if configured != "http":
        reason = (
            "HTTP was selected, but no endpoint is configured. Using mock."
            if requested == "http"
            else "Offline mock backend."
        )
        return {
            "backend": configured,
            "model": _model_for_backend(configured, data),
            "http_available": False,
            "server_http_configured": server_http_configured,
            "reason": reason,
        }

    url, key = _http_connection_config(data)
    ok, reason = probe_http_endpoint(url, key)
    backend = "http" if ok else "mock"
    return {
        "backend": backend,
        "model": _model_for_backend(backend, data),
        "http_available": ok,
        "server_http_configured": server_http_configured,
        "reason": reason if ok else f"{reason} Using mock.",
    }


def _model_for_backend(backend: str, data: dict | None = None) -> str:
    data = data or {}
    if backend == "http":
        return str(data.get("llm_http_model") or os.environ.get("LLM_HTTP_MODEL") or "default")
    if backend == "openai":
        return str(data.get("openai_model") or os.environ.get("OPENAI_MODEL") or "gpt-4o")
    if backend == "anthropic":
        return str(data.get("anthropic_model") or os.environ.get("ANTHROPIC_MODEL") or "claude-sonnet-4-20250514")
    return "offline memory replay"


def _job_timeout_seconds(data: dict | None = None) -> float:
    """Return the single wall-clock budget used by both job and pipeline."""
    data = data or {}
    try:
        raw = data.get("job_timeout") or os.environ.get("JOB_TIMEOUT") or "1200"
        return max(30.0, float(raw))
    except (TypeError, ValueError):
        return 1200.0


def _llm_timeout_seconds(data: dict | None, job_limit: float) -> float:
    """Cap one LLM request so it cannot outlive its formalization job."""
    data = data or {}
    try:
        raw = data.get("llm_timeout") or os.environ.get("LLM_TIMEOUT") or "300"
        configured = max(5.0, float(raw))
    except (TypeError, ValueError):
        configured = 300.0
    return min(configured, job_limit)


def _lean_compile_timeout_seconds(data: dict | None, job_limit: float) -> float:
    """Configured cap for one Lean check; job deadline remains authoritative."""
    data = data or {}
    try:
        raw = data.get("lean_compile_timeout") or os.environ.get("LEAN_TIMEOUT") or "120"
        configured = max(15.0, float(raw))
    except (TypeError, ValueError):
        configured = 120.0
    return min(configured, job_limit)


def _request_int(data: dict, key: str, default: int, minimum: int) -> int:
    """Read an integer request option without treating valid zero as missing."""
    raw = data.get(key)
    try:
        value = default if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _build_llm_from_request(data: dict) -> LLMClient:
    backend = _backend_status(data)["backend"]
    overrides = {}
    mapping = {
        "openai_api_key": "OPENAI_API_KEY",
        "openai_model": "OPENAI_MODEL",
        "openai_base_url": "OPENAI_BASE_URL",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "anthropic_model": "ANTHROPIC_MODEL",
        "llm_http_url": "LLM_HTTP_URL",
        "llm_http_key": "LLM_HTTP_KEY",
        "llm_http_model": "LLM_HTTP_MODEL",
    }
    for form_key, env_key in mapping.items():
        val = data.get(form_key)
        if val:
            overrides[env_key] = str(val)
    old = {k: os.environ.get(k) for k in overrides}
    old_backend = os.environ.get("LLM_BACKEND")
    try:
        for k, v in overrides.items():
            os.environ[k] = v
        os.environ["LLM_BACKEND"] = backend
        return get_llm(backend)
    finally:
        for k, prev in old.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        if old_backend is None:
            os.environ.pop("LLM_BACKEND", None)
        else:
            os.environ["LLM_BACKEND"] = old_backend


def _run_pipeline(
    data: dict,
    text: Optional[str],
    pdf_path: Optional[str],
    progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict:
    job_limit = _job_timeout_seconds(data)
    llm = _build_llm_from_request(data)
    # The client owns this value for the whole job.  Do not mutate process-wide
    # environment variables here: simultaneous jobs would race each other.
    llm.timeout_seconds = _llm_timeout_seconds(data, job_limit)
    num_cand = _request_int(data, "num_candidates", 2, 1)
    max_rep = _request_int(data, "max_repair_rounds", 3, 0)
    candidate_workers = min(
        num_cand, _request_int(data, "max_parallel_candidates", 1, 1)
    )
    compile_workers = min(num_cand, _request_int(data, "lean_compile_workers", 1, 1))
    compile_timeout = _lean_compile_timeout_seconds(data, job_limit)
    temp = float(data.get("temperature") or 0.2)

    # Merge optional formal statement with NL proof so structure/LLM see one document.
    # UI often puts the theorem in "target_statement" and the proof sketch in "text".
    target = (data.get("target_statement") or "").strip()
    body = (text or "").strip()
    if target and body and target not in body:
        text = f"{target}\n\n{body}"
    elif target and not body:
        text = target

    domain = data.get("domain") or "general"
    save = str(data.get("save_to_memory", "true")).lower() in ("1", "true", "yes")
    max_pages = data.get("max_pages")
    max_pages_i = int(max_pages) if max_pages not in (None, "") else None

    pipe = FormalizationPipeline(
        llm=llm,
        memory_dir=MEMORY_DIR,
        num_candidates=num_cand,
        max_repair_rounds=max_rep,
        temperature=temp,
        max_parallel_candidates=candidate_workers,
        lean_compile_workers=compile_workers,
        lean_compile_timeout=compile_timeout,
        verification_lock=_verification_lock,
        progress_callback=progress_callback,
    )
    # Wall-clock deadline for cooperative cancellation between stages.
    import time as _time
    deadline = _time.monotonic() + job_limit
    try:
        result = pipe.formalize(
            text=text,
            pdf_path=pdf_path,
            domain=domain,
            max_pages=max_pages_i,
            save_to_memory=save,
            target_statement=target or None,
            deadline_monotonic=deadline,
        )
        d = result.to_dict()
        # Attach live usage from the client if missing
        if hasattr(llm, "usage") and not d.get("token_usage"):
            d["token_usage"] = llm.usage.to_dict()
        return d
    except QuotaExceededError as e:
        usage = llm.usage.to_dict() if hasattr(llm, "usage") else {}
        return {
            "success": False,
            "best_code": "",
            "message": str(e),
            "quota_exceeded": True,
            "token_usage": usage,
            "domain": domain,
            "candidates": [],
            "memory_hits": [],
            "repair_count": 0,
            "stage_trace": list(getattr(pipe, "_stage_trace", []) or []),
            "verification": {
                "compiled": False,
                "summary": "Stopped: API quota / credits exhausted.",
                "math_feedback": str(e),
                "issues": [],
                "suggestions": [
                    "Check billing on the provider or CC Switch",
                    "Switch to mock backend for offline testing",
                ],
                "raw_message": str(e),
                "lean_available": False,
            },
        }
    except LLMTimeoutError as e:
        usage = llm.usage.to_dict() if hasattr(llm, "usage") else {}
        return {
            "success": False,
            "best_code": "",
            "message": str(e),
            "timed_out": True,
            "token_usage": usage,
            "domain": domain,
            "candidates": [],
            "memory_hits": [],
            "repair_count": 0,
            "stage_trace": list(getattr(pipe, "_stage_trace", []) or []),
            "verification": {
                "compiled": False,
                "summary": "Stopped: LLM API call timed out.",
                "math_feedback": str(e),
                "issues": [],
                "suggestions": [
                    "Increase LLM_TIMEOUT (seconds)",
                    "Use simple claims / mock backend",
                    "Check CC Switch / network latency",
                ],
                "raw_message": str(e),
                "lean_available": False,
            },
        }


def _set_job(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        cur = _jobs.get(job_id) or {}
        cur.update(fields)
        _jobs[job_id] = cur
        snapshot = dict(cur)
    # Record history when a job finishes
    if fields.get("status") in ("done", "error"):
        try:
            _push_history(job_id, snapshot)
        except Exception as ex:
            _log(f"[history] push failed: {ex}")


def _memory_hit_is_relevant(query: str, entry_nl: str) -> bool:
    """Reject weak TF-IDF matches so mock mode does not claim wrong theorems."""
    from lean_formalizer.memory import _normalize_nl

    q = _normalize_nl(query or "")
    n = _normalize_nl(entry_nl or "")
    if not q or not n:
        return False
    if q == n or q in n or n in q:
        return True
    q_toks = set(q.split())
    n_toks = set(n.split())
    if not q_toks:
        return False
    overlap = len(q_toks & n_toks) / max(len(q_toks), 1)
    return overlap >= 0.45


def _fast_mock_result(text: str, domain: str) -> dict:
    """Guaranteed-fast offline result (no multi-candidate loop)."""
    from lean_formalizer.memory import MemoryBank
    from lean_formalizer.lean_verify import compile_lean, lean_available
    from lean_formalizer.diagnose import build_report

    bank = MemoryBank(str(ROOT / "memory"))
    hits = bank.retrieve(text or "", domain=domain or "general", top_k_success=3)
    relevant = [
        e
        for e in hits.get("success") or []
        if _memory_hit_is_relevant(text or "", e.natural_language)
    ]
    if relevant:
        top = relevant[0]
        code = top.lean_code
        theorem = top.natural_language
        sketch = [top.natural_language_proof or "retrieved from memory"]
        ok, msg = compile_lean(code)
        ver = build_report(
            compiled=ok,
            raw_message=msg,
            code=code,
            natural_language=theorem,
            lean_available=lean_available(),
            llm=None,
        )
        return {
            "success": ok,
            "best_code": code,
            "structured": {
                "definitions": [],
                "main_theorem": theorem,
                "lemmas": [],
                "proof_sketch": sketch if isinstance(sketch, list) else [str(sketch)],
                "domain": domain or "general",
                "raw_text": text,
            },
            "candidates": [
                {
                    "lean_code": code,
                    "statement_only": code.split(":=", 1)[0].strip(),
                    "proof": code,
                    "source_stage": "mock_fast",
                    "error_log": [] if ok else [msg],
                    "compiled": ok,
                    "rank_score": 1.0,
                    "notes": "fast mock path",
                }
            ],
            "memory_hits": [
                {"type": "success", "id": e.id, "nl": e.natural_language[:120]}
                for e in relevant
            ],
            "repair_count": 0,
            "message": ver.summary,
            "domain": domain or "general",
            "verification": ver.to_dict(),
        }
    reason = (
        "Mock backend is offline-only: it can replay verified memory entries "
        "but cannot formalize new claims. Configure a real LLM backend "
        "(OpenAI/Anthropic/HTTP) or use a claim already in the library."
    )
    theorem = text.strip()[:500]
    ver = build_report(
        compiled=False,
        raw_message=reason,
        code="",
        natural_language=theorem,
        lean_available=lean_available(),
        llm=None,
    )
    return {
        "success": False,
        "best_code": "",
        "structured": {
            "definitions": [],
            "main_theorem": theorem,
            "lemmas": [],
            "proof_sketch": [],
            "domain": domain or "general",
            "raw_text": text,
        },
        "candidates": [],
        "memory_hits": [],
        "repair_count": 0,
        "message": ver.summary,
        "domain": domain or "general",
        "verification": ver.to_dict(),
        "token_usage": {},
        "stage_trace": [],
    }


def _start_job(data: dict, text: Optional[str], pdf_path: Optional[str]) -> str:
    job_id = str(uuid.uuid4())
    backend = _backend_status(data)["backend"]
    model = _model_for_backend(backend, data)
    job_limit = _job_timeout_seconds(data)
    _set_job(
        job_id,
        status="running",
        result=None,
        error=None,
        step="queued",
        backend=backend,
        model=model,
        progress=[],
        current_event={"stage": "queued", "ok": True},
        job_timeout_seconds=job_limit,
        llm_timeout_seconds=_llm_timeout_seconds(data, job_limit),
        lean_compile_timeout_seconds=_lean_compile_timeout_seconds(data, job_limit),
    )

    def report_progress(entry: dict[str, Any]) -> None:
        with _jobs_lock:
            job = _jobs.get(job_id) or {}
            if job.get("status") != "running":
                return
            progress = list(job.get("progress") or [])
            progress.append(entry)
            # Bound polling payload while retaining a useful trail.
            _jobs[job_id] = {
                **job,
                "progress": progress[-20:],
                "step": entry.get("stage") or job.get("step", "pipeline"),
                "current_event": dict(entry),
            }

    def worker() -> None:
        domain = data.get("domain") or "general"
        try:
            _log(f"[job {job_id[:8]}] start backend={backend!r}")
            _set_job(job_id, step="started", current_event={"stage": "started", "ok": True})

            # Offline mock: skip heavy multi-candidate pipeline (was hanging for some users)
            if backend == "mock" and text and not pdf_path:
                _set_job(job_id, step="mock_fast", current_event={"stage": "mock_fast", "ok": True})
                result = _fast_mock_result(text, domain)
                _set_job(
                    job_id,
                    status="done",
                    result=result,
                    error=None,
                    step="done",
                    current_event={"stage": "done", "ok": bool(result.get("success"))},
                )
                _log(f"[job {job_id[:8]}] done success={result.get('success')}")
                return
                # Still try full pipeline briefly for richer structure, but never block forever
                try:
                    data_full = dict(data)
                    data_full["num_candidates"] = min(int(data.get("num_candidates") or 2), 2)
                    data_full["max_repair_rounds"] = min(int(data.get("max_repair_rounds") or 1), 1)
                    _set_job(job_id, step="pipeline")
                    # Run with a hard timeout via join
                    box: dict[str, Any] = {}

                    def run_full() -> None:
                        try:
                            box["result"] = _run_pipeline(
                                data_full, text=text, pdf_path=None,
                                progress_callback=report_progress,
                            )
                        except Exception as ex:
                            box["error"] = str(ex)

                    t = threading.Thread(target=run_full, daemon=True)
                    # The fast mock result is terminal. Starting this optional
                    # pipeline in the background used to mutate a completed
                    # job after the UI had already received its result.
                    # Keep it unstarted until mock enrichment has a real job
                    # lifecycle of its own.
                    if t.is_alive():
                        _log(f"[job {job_id[:8]}] full pipeline timeout → using fast mock")
                    elif box.get("result"):
                        result = box["result"]
                    elif box.get("error"):
                        _log(f"[job {job_id[:8]}] pipeline error (using fast mock): {box['error']}")
                except Exception as ex:
                    _log(f"[job {job_id[:8]}] pipeline wrapper error: {ex}")

                _set_job(job_id, status="done", result=result, error=None, step="done")
                _log(f"[job {job_id[:8]}] done success={result.get('success')}")
                return

            _set_job(job_id, step="pipeline", current_event={"stage": "pipeline", "ok": True})
            _log(
                f"[job {job_id[:8]}] job_limit={job_limit:.0f}s "
                f"llm_timeout={_llm_timeout_seconds(data, job_limit):.0f}s "
                f"lean_timeout={_lean_compile_timeout_seconds(data, job_limit):.0f}s"
            )
            # This worker is the job. Do not use a second timed join here:
            # Python cannot stop that child thread, which previously caused a
            # terminal timeout result while the pipeline kept compiling.
            result = _run_pipeline(
                data, text=text, pdf_path=pdf_path,
                progress_callback=report_progress,
            )
            _set_job(
                job_id,
                status="done",
                result=result,
                error=None,
                step="done",
                current_event={"stage": "done", "ok": bool(result.get("success"))},
            )
            code_len = len((result.get("best_code") or "").strip())
            n_trace = len(result.get("stage_trace") or [])
            _log(
                f"[job {job_id[:8]}] done success={result.get('success')} "
                f"code_len={code_len} stages={n_trace} "
                f"msg={(result.get('message') or '')[:80]}"
            )
            for e in result.get("stage_trace") or []:
                ok = "ok" if e.get("ok", True) else "FAIL"
                prev = (e.get("preview") or e.get("error") or "")[:100]
                _log(f"  [{ok}] {e.get('stage')}: len={e.get('len', '?')} {prev}")
        except Exception as e:
            traceback.print_exc()
            _set_job(
                job_id,
                status="error",
                result=None,
                error=str(e),
                trace=traceback.format_exc(),
                step="error",
                current_event={"stage": "error", "ok": False, "error": str(e)},
            )
            _log(f"[job {job_id[:8]}] error: {e}")
        finally:
            if pdf_path:
                try:
                    os.unlink(pdf_path)
                except OSError:
                    pass

    threading.Thread(target=worker, daemon=True).start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    server_version = f"LeanFormalizer/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))
        sys.stderr.flush()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Connection", "close")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            index = STATIC_DIR / "index.html"
            if index.exists():
                _html_response(self, index.read_text(encoding="utf-8"))
            else:
                _html_response(self, "<h1>index.html missing</h1>", 500)
            return

        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            fpath = (STATIC_DIR / rel).resolve()
            if not str(fpath).startswith(str(STATIC_DIR.resolve())) or not fpath.is_file():
                self.send_error(404)
                return
            data = fpath.read_bytes()
            ctype = "application/octet-stream"
            if fpath.suffix == ".css":
                ctype = "text/css"
            elif fpath.suffix == ".js":
                ctype = "application/javascript"
            elif fpath.suffix == ".html":
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/health":
            backend_status = _backend_status()
            backend = backend_status["backend"]
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "lean_available": lean_available(),
                    "llm_backend": backend,
                    "llm_model": _model_for_backend(backend),
                    "http_available": backend_status["http_available"],
                    "server_http_configured": backend_status["server_http_configured"],
                    "backend_reason": backend_status["reason"],
                    "lean_project": str(lean_project_dir() or ""),
                    "mathlib_index": "available" if lean_project_dir() else "unavailable",
                    "memory_dir": MEMORY_DIR,
                },
            )
            return

        if path == "/api/stats":
            try:
                bank = MemoryBank(MEMORY_DIR)
                info = bank.stats()
                info["lean_available"] = lean_available()
                backend_status = _backend_status()
                backend = backend_status["backend"]
                info["llm_backend"] = backend
                info["llm_model"] = _model_for_backend(backend)
                info["http_available"] = backend_status["http_available"]
                info["backend_reason"] = backend_status["reason"]
                info["lean_project"] = str(lean_project_dir() or "")
                info["mathlib_index"] = "available" if lean_project_dir() else "unavailable"
                info["memory_dir"] = MEMORY_DIR
                info["version"] = __version__
                # in-memory job counters (this server process only)
                with _jobs_lock:
                    jobs = list(_jobs.values())
                info["jobs_total"] = len(jobs)
                info["jobs_done"] = sum(1 for j in jobs if j.get("status") == "done")
                info["jobs_error"] = sum(1 for j in jobs if j.get("status") == "error")
                info["jobs_running"] = sum(1 for j in jobs if j.get("status") == "running")
                ok_results = 0
                fail_results = 0
                timeouts = 0
                for j in jobs:
                    r = j.get("result") or {}
                    if j.get("status") != "done":
                        continue
                    if r.get("timed_out"):
                        timeouts += 1
                    elif r.get("success"):
                        ok_results += 1
                    else:
                        fail_results += 1
                info["session_verified"] = ok_results
                info["session_failed"] = fail_results
                info["session_timed_out"] = timeouts
                with _history_lock:
                    info["history_count"] = len(_history)
                _json_response(self, 200, info)
            except Exception as e:
                _json_response(self, 500, {"error": str(e)})
            return

        if path == "/api/history":
            try:
                # Optional ?limit=N
                limit = 20
                if "?" in self.path:
                    from urllib.parse import urlparse, parse_qs

                    qs = parse_qs(urlparse(self.path).query)
                    if "limit" in qs:
                        try:
                            limit = max(1, min(50, int(qs["limit"][0])))
                        except (TypeError, ValueError):
                            limit = 20
                with _history_lock:
                    items = list(reversed(_history[-limit:]))
                # Also include currently running jobs (brief)
                with _jobs_lock:
                    running = [
                        {
                            "job_id": jid,
                            "status": j.get("status"),
                            "outcome": "running",
                            "step": j.get("step"),
                            "claim": "",
                            "message": "in progress…",
                        }
                        for jid, j in _jobs.items()
                        if j.get("status") == "running"
                    ]
                _json_response(
                    self,
                    200,
                    {
                        "count": len(items),
                        "max": _HISTORY_MAX,
                        "items": items,
                        "running": running,
                        "note": (
                            "History is kept in memory for this server process only "
                            f"(last {_HISTORY_MAX} finished tries). Restart clears it."
                        ),
                    },
                )
            except Exception as e:
                _json_response(self, 500, {"error": str(e)})
            return

        if path == "/api/memory/stats":
            try:
                bank = MemoryBank(MEMORY_DIR)
                _json_response(self, 200, bank.stats())
            except Exception as e:
                _json_response(self, 500, {"error": str(e)})
            return

        if path == "/api/doctor":
            backend_status = _backend_status()
            backend = backend_status["backend"]
            info: dict[str, Any] = {
                "lean_available": lean_available(),
                "llm_backend": backend,
                "llm_model": _model_for_backend(backend),
                "http_available": backend_status["http_available"],
                "backend_reason": backend_status["reason"],
                "memory_dir": MEMORY_DIR,
                "python": sys.version,
            }
            try:
                # Test the backend the server will actually use.  Testing a
                # hard-coded MockLLM made Doctor report success even when an
                # HTTP/OpenAI backend was misconfigured or unauthorized.
                llm = get_llm(backend)
                reply = llm.chat(
                    [{"role": "user", "content": "Reply with the single word: pong"}]
                )
                info["llm_smoke"] = reply.strip()[:120]
                info["llm_ok"] = True
            except Exception as e:
                info["llm_ok"] = False
                info["llm_error"] = str(e)
            _json_response(self, 200, info)
            return

        if path == "/api/autoconfig":
            try:
                info = _autoconfig.build_autoconfig(repo_root=ROOT)
                _json_response(self, 200, info)
            except Exception as e:
                _json_response(self, 500, {"error": str(e)})
            return

        if path.startswith("/api/job/"):
            job_id = path[len("/api/job/") :].strip("/")
            with _jobs_lock:
                job = _jobs.get(job_id)
            if not job:
                _json_response(self, 404, {"error": "job not found", "job_id": job_id})
            else:
                _json_response(self, 200, {"job_id": job_id, **job})
            return

        self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        _log(f"[POST] {path}")
        try:
            if path == "/api/formalize":
                self._handle_formalize_async()
                return
            if path == "/api/backend/check":
                _json_response(self, 200, _backend_status(_read_json_body(self)))
                return
            if path == "/api/formalize/upload":
                self._handle_formalize_upload_async()
                return
            if path == "/api/formalize/sync":
                # Optional synchronous path (for curl / debugging)
                self._handle_formalize_sync()
                return
            if path == "/api/memory/seed":
                self._handle_seed()
                return
            if path == "/api/autoconfig/apply":
                body = _read_json_body(self)
                overwrite = bool(body.get("overwrite"))
                prefer = body.get("lean_project") or None
                info = _autoconfig.build_autoconfig(repo_root=ROOT, prefer_project=prefer)
                env = dict(info.get("suggested_env") or {})
                if body.get("openai_api_key"):
                    env["OPENAI_API_KEY"] = str(body["openai_api_key"])
                if body.get("openai_model"):
                    env["OPENAI_MODEL"] = str(body["openai_model"])
                if body.get("openai_base_url"):
                    env["OPENAI_BASE_URL"] = str(body["openai_base_url"])
                    env["LLM_BACKEND"] = env.get("LLM_BACKEND") or "openai"
                path_env = _autoconfig.write_local_config_env(ROOT, env, overwrite=overwrite)
                path_ps1 = _autoconfig.write_local_config_ps1(ROOT, env, overwrite=overwrite)
                for k, v in env.items():
                    if v:
                        os.environ[k] = v
                _json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "wrote": [str(path_env), str(path_ps1)],
                        "env": {
                            k: ("***" if "KEY" in k and v else v) for k, v in env.items()
                        },
                        "notes": info.get("notes"),
                    },
                )
                return
            if path == "/api/echo":
                data = _read_json_body(self)
                _json_response(self, 200, {"echo": data, "ok": True})
                return
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            _json_response(self, 500, {"error": str(e), "trace": traceback.format_exc()})

    def _handle_formalize_async(self) -> None:
        data = _read_json_body(self)
        text = (data.get("text") or "").strip()
        target = (data.get("target_statement") or "").strip()
        if not text and target:
            text = target
        backend_status = _backend_status(data)
        backend = backend_status["backend"]
        data["llm_backend"] = backend
        _log(f"[formalize-async] backend={backend!r} text_len={len(text)}")
        if not text:
            _json_response(self, 400, {"error": "text or target_statement is required"})
            return
        job_id = _start_job(data, text=text, pdf_path=None)
        _json_response(
            self, 200,
            {
                "job_id": job_id,
                "status": "running",
                "job_timeout_seconds": _job_timeout_seconds(data),
            },
        )

    def _handle_formalize_sync(self) -> None:
        data = _read_json_body(self)
        text = (data.get("text") or "").strip()
        if not text:
            _json_response(self, 400, {"error": "text is required"})
            return
        try:
            result = _run_pipeline(data, text=text, pdf_path=None)
            _json_response(self, 200, result)
        except Exception as e:
            traceback.print_exc()
            _json_response(self, 500, {"error": str(e)})

    def _handle_formalize_upload_async(self) -> None:
        # Minimal multipart parser (filename + fields) without cgi
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            _json_response(self, 400, {"error": "expected multipart/form-data"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        data, pdf_path = _parse_multipart(ctype, raw)
        text = (data.get("text") or "").strip()
        if not text and not pdf_path:
            _json_response(self, 400, {"error": "provide text or a PDF file"})
            return
        data["llm_backend"] = _backend_status(data)["backend"]
        job_id = _start_job(data, text=text or None, pdf_path=pdf_path)
        _json_response(
            self, 200,
            {
                "job_id": job_id,
                "status": "running",
                "job_timeout_seconds": _job_timeout_seconds(data),
            },
        )

    def _handle_seed(self) -> None:
        examples = [
            {
                "natural_language": "Every natural number is either even or odd.",
                "lean_code": (
                    "import Mathlib.Algebra.Ring.Parity\n"
                    "theorem nat_even_or_odd (n : ℕ) : Even n ∨ Odd n := Nat.even_or_odd n"
                ),
                "domain": "number_theory",
                "difficulty": "easy",
                "tags": ["parity", "nat"],
            },
        ]
        bank = MemoryBank(MEMORY_DIR)
        for ex in examples:
            bank.save_success(**ex)
        _json_response(self, 200, {"seeded": len(examples), **bank.stats()})


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict, Optional[str]]:
    """Very small multipart parser for our form fields + optional PDF."""
    data: dict[str, str] = {}
    pdf_path: Optional[str] = None
    if "boundary=" not in content_type:
        return data, None
    boundary = content_type.split("boundary=", 1)[1].strip().strip('"').encode()
    parts = body.split(b"--" + boundary)
    for part in parts:
        if not part or part in (b"--\r\n", b"--", b"\r\n", b"--\r\n\r\n"):
            continue
        if part.startswith(b"--"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers = header_blob.decode("utf-8", errors="replace")
        # name=
        name = None
        for line in headers.split("\r\n"):
            if "name=" in line:
                # Content-Disposition: form-data; name="text"; filename="x.pdf"
                for chunk in line.split(";"):
                    chunk = chunk.strip()
                    if chunk.startswith("name="):
                        name = chunk.split("=", 1)[1].strip().strip('"')
                    elif chunk.startswith("filename="):
                        fname = chunk.split("=", 1)[1].strip().strip('"')
                        if name == "pdf" or (fname and fname.lower().endswith(".pdf")):
                            tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                            tmp.write(content)
                            tmp.close()
                            pdf_path = tmp.name
                            name = None  # don't also put binary in data
                break
        if name:
            data[name] = content.decode("utf-8", errors="replace")
    return data, pdf_path


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Lean Formalizer Web UI")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)

    if not (STATIC_DIR / "index.html").exists():
        print(f"ERROR: {STATIC_DIR / 'index.html'} not found", file=sys.stderr)
        return 1

    # Auto-detect sibling Lake project (Lean_Work/lean_formalizer + mathlib_project)
    # when LEAN_PROJECT is still unset. ROOT is the package repo root.
    if not (os.environ.get("LEAN_PROJECT") or os.environ.get("MATHLIB_PROJECT")):
        try:
            info = _autoconfig.build_autoconfig(repo_root=ROOT)
            env = info.get("suggested_env") or {}
            if env.get("LEAN_PROJECT"):
                _autoconfig.write_local_config_env(ROOT, env, overwrite=False)
                os.environ.setdefault("LEAN_PROJECT", env["LEAN_PROJECT"])
                print(f"[autoconfig] Detected LEAN_PROJECT={env['LEAN_PROJECT']}")
                for n in info.get("notes") or []:
                    print(f"[autoconfig] {n}")
        except Exception as ex:
            print(f"[autoconfig] skipped: {ex}")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Lean Formalizer Web UI  v{__version__}")
    print(f"  Open:   {url}")
    print(f"  Memory: {MEMORY_DIR}")
    proj = lean_project_dir()
    print(f"  Lean:   {'yes' if lean_available() else 'no (synthetic checker)'}")
    print(f"  Project:{' ' + str(proj) if proj else ' (set LEAN_PROJECT — sibling mathlib_project not found)'}")
    print(f"  LLM:    {os.environ.get('LLM_BACKEND', 'mock')}")
    print("Press Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
