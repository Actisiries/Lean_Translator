"""Pluggable LLM backends with optional token-usage accounting.

Supported:
  - mock      : offline demo
  - openai    : OpenAI SDK (+ CC Switch / any OpenAI-compatible base_url)
  - anthropic : Anthropic Messages API
  - http      : generic OpenAI-compatible HTTP endpoint
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Usage + errors
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Accumulated token counters for one formalization run (best-effort)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    # True when provider reported usage on at least one call
    reported: bool = False

    def add(
        self,
        prompt: int = 0,
        completion: int = 0,
        total: Optional[int] = None,
    ) -> None:
        self.prompt_tokens += max(0, int(prompt or 0))
        self.completion_tokens += max(0, int(completion or 0))
        if total is not None:
            self.total_tokens += max(0, int(total))
        else:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        self.calls += 1
        self.reported = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QuotaExceededError(RuntimeError):
    """Raised when the API reports no quota / billing / credit left."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.is_quota = True


_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "billing",
    "payment required",
    "credit",
    "out of credits",
    "quota exceeded",
    "quota_exceeded",
    "no remaining",
    "balance is not enough",
    "account has no",
    "exceeded your quota",
)


def _looks_like_quota(text: str, status: Optional[int] = None) -> bool:
    t = (text or "").lower()
    if status in (402, 403) and any(m in t for m in ("quota", "billing", "credit", "balance")):
        return True
    if status == 429 and any(m in t for m in ("quota", "billing", "insufficient")):
        return True  # 429 can also be rate-limit; only treat as quota if text says so
    return any(m in t for m in _QUOTA_MARKERS)


def _raise_if_quota(text: str, status: Optional[int] = None) -> None:
    if _looks_like_quota(text, status):
        raise QuotaExceededError(
            "API quota / credits exhausted (or billing problem). "
            "Check your provider or CC Switch account. "
            f"Detail: {(text or '')[:400]}",
            status_code=status,
        )



def llm_timeout_seconds(default: float = 300.0) -> float:
    """Per-request LLM HTTP timeout (seconds). Env: LLM_TIMEOUT."""
    try:
        return max(5.0, float(os.environ.get("LLM_TIMEOUT", default)))
    except ValueError:
        return default


class LLMTimeoutError(RuntimeError):
    """Raised when a single LLM API call exceeds LLM_TIMEOUT."""

    def __init__(self, message: str = "LLM request timed out"):
        super().__init__(message)
        self.is_timeout = True

class LLMClient(ABC):
    def __init__(self) -> None:
        self.usage = TokenUsage()
        # Kept on the client instead of read from the process environment at
        # request time, so concurrent web jobs cannot overwrite each other's
        # timeout setting.
        self.timeout_seconds = llm_timeout_seconds()

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        ...

    def reset_usage(self) -> None:
        self.usage = TokenUsage()


def get_llm(backend: Optional[str] = None) -> LLMClient:
    backend = (backend or os.environ.get("LLM_BACKEND") or "").lower()
    if not backend:
        if os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_KEY"):
            backend = "openai"
        elif os.environ.get("LLM_HTTP_URL"):
            backend = "http"
        else:
            backend = "mock"
    if backend == "mock":
        return MockLLM()
    if backend == "openai":
        return OpenAILLM()
    if backend == "anthropic":
        return AnthropicLLM()
    if backend == "http":
        return HTTPLLM()
    raise ValueError(f"Unknown LLM backend: {backend}")


# ----------------------------------------------------------------------
# Mock
# ----------------------------------------------------------------------
class MockLLM(LLMClient):
    """Rule-based mock that returns plausible Lean for common toy statements."""

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # Fake small usage so UI can demonstrate the counter
        approx_in = max(1, len(user) // 4)
        out = self._reply(user)
        approx_out = max(1, len(out) // 4)
        self.usage.add(prompt=approx_in, completion=approx_out)
        return out

    def _reply(self, user: str) -> str:
        lower = user.lower()

        if "return a json object" in lower or "decompose" in lower:
            return self._structure(user)

        if "rephrase the following" in lower or "rephrase the mathematical" in lower:
            return (
                "The statement asserts a basic dichotomy for natural numbers: "
                "every natural number is congruent to 0 or 1 modulo 2."
            )

        if (
            "produce only the declaration" in lower
            or "lean declaration only" in lower
            or "declaration only" in lower
        ):
            return self._statement(user)

        if (
            "write a complete proof" in lower
            or "declaration and the proof" in lower
            or "complete, idiomatic" in lower
        ):
            return self._proof(user)

        if "review the following lean" in lower or "improved complete lean" in lower:
            m = re.search(
                r"```\s*(?:lean(?:\s*4)?)\b\s*(.*?)```",
                user,
                re.DOTALL,
            )
            return m.group(1).strip() if m else user

        if "failed to compile" in lower or "fix it so that it type-checks" in lower:
            m = re.search(
                r"```\s*(?:lean(?:\s*4)?)\b\s*(.*?)```",
                user,
                re.DOTALL,
            )
            code = m.group(1).strip() if m else ""
            if "sorry" in code and "even" in code.lower():
                return """import Mathlib.Algebra.Ring.Parity
theorem nat_even_or_odd (n : ℕ) : Even n ∨ Odd n := Nat.even_or_odd n"""
            return code

        if "rank them from best" in lower or "rank best" in lower:
            n = len(re.findall(r"Candidate\s+\d+", user))
            return json.dumps(list(range(n)) if n else [0])

        if "write a short formal-style natural-language proof" in lower:
            return "Proof. Apply the corresponding Mathlib lemma; the claim follows directly."

        return "MOCK: no real LLM backend configured; no Lean code generated."

    def _structure(self, user: str) -> str:
        m = re.search(r'"""(.*?)"""', user, re.DOTALL)
        body = (m.group(1) if m else user).strip()
        first_line = body.split("\n")[0][:200]
        return json.dumps(
            {
                "definitions": [
                    "A natural number n is even if there exists k such that n = 2k.",
                    "A natural number n is odd if there exists k such that n = 2k+1.",
                ],
                "main_theorem": first_line or "Every natural number is either even or odd.",
                "lemmas": [],
                "proof_sketch": [
                    "Use induction on n, or apply the standard Mathlib lemma Nat.even_or_odd.",
                ],
                "domain": "number_theory",
                "difficulty": "easy",
            },
            indent=2,
        )

    def _statement(self, user: str) -> str:
        if "even" in user.lower() and "odd" in user.lower():
                return """import Mathlib.Algebra.Ring.Parity
theorem nat_even_or_odd (n : ℕ) : Even n ∨ Odd n"""
        return "MOCK: no Lean code generated."

    def _proof(self, user: str) -> str:
        if "even" in user.lower() and "odd" in user.lower():
            return """import Mathlib.Algebra.Ring.Parity
theorem nat_even_or_odd (n : ℕ) : Even n ∨ Odd n := Nat.even_or_odd n"""
        m = re.search(r"```\s*(?:lean(?:\s*4)?)\b\s*(.*?)```", user, re.DOTALL)
        decl = m.group(1).strip() if m else ""
        if ":=" in decl:
            return decl
        return decl


# ----------------------------------------------------------------------
# OpenAI (+ CC Switch)
# ----------------------------------------------------------------------
class OpenAILLM(LLMClient):
    """OpenAI SDK; set OPENAI_BASE_URL for CC Switch / compatible proxies."""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        super().__init__()
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get(
            "LLM_HTTP_KEY", "sk-placeholder"
        )
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL") or os.environ.get(
            "LLM_HTTP_URL"
        )
        if self.base_url and self.base_url.rstrip("/").endswith("/chat/completions"):
            self.base_url = self.base_url.rstrip("/").rsplit("/chat/completions", 1)[0]

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "openai package not installed. pip install openai"
            ) from e
        kwargs: dict[str, Any] = {"api_key": self.api_key or "sk-placeholder"}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = OpenAI(**kwargs)
        timeout = self.timeout_seconds
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as e:
            err = str(e)
            low = err.lower()
            if "timeout" in low or "timed out" in low:
                raise LLMTimeoutError(
                    f"LLM request timed out after {timeout:.0f}s. "
                    "Increase LLM_TIMEOUT or simplify the request."
                ) from e
            status = getattr(e, "status_code", None) or getattr(
                getattr(e, "response", None), "status_code", None
            )
            _raise_if_quota(err, status)
            raise

        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.add(
                prompt=getattr(u, "prompt_tokens", 0) or 0,
                completion=getattr(u, "completion_tokens", 0) or 0,
                total=getattr(u, "total_tokens", None),
            )
        message = resp.choices[0].message
        content = _content_text(getattr(message, "content", None))
        if not content.strip():
            content = _content_text(
                getattr(message, "reasoning_content", None)
                or getattr(message, "reasoning", None)
            )
        return content


# ----------------------------------------------------------------------
# Anthropic
# ----------------------------------------------------------------------
class AnthropicLLM(LLMClient):
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        super().__init__()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "anthropic package not installed. pip install anthropic"
            ) from e
        timeout = self.timeout_seconds
        client = anthropic.Anthropic(api_key=self.api_key, timeout=timeout)
        system = ""
        user_msgs = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user_msgs.append(m)
        try:
            resp = client.messages.create(
                model=self.model,
                system=system or "You are an expert Lean 4 formalizer.",
                messages=user_msgs,  # type: ignore
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            err = str(e)
            if "timeout" in err.lower() or "timed out" in err.lower():
                raise LLMTimeoutError(
                    f"LLM request timed out after {timeout:.0f}s"
                ) from e
            _raise_if_quota(err, getattr(e, "status_code", None))
            raise

        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage.add(
                prompt=getattr(u, "input_tokens", 0) or 0,
                completion=getattr(u, "output_tokens", 0) or 0,
            )
        return resp.content[0].text  # type: ignore



def normalize_chat_completions_url(url: str) -> str:
    """Ensure OpenAI-compatible chat endpoint ends with /v1/chat/completions."""
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    low = u.lower()
    if low.endswith("/chat/completions"):
        return u
    if low.endswith("/v1"):
        return u + "/chat/completions"
    if "/v1/" in low and not low.endswith("/chat/completions"):
        # already has some /v1/... path — still append if not completions
        return u + "/chat/completions"
    # bare host or host:port
    return u + "/v1/chat/completions"


def probe_http_endpoint(
    url: str,
    api_key: str = "",
    timeout: float = 2.0,
) -> tuple[bool, str]:
    """Check an OpenAI-compatible endpoint without generating a response.

    The probe uses ``/v1/models`` rather than chat completions, so opening the
    UI never sends mathematical input or consumes inference tokens.  Endpoints
    which reject the supplied key are considered unavailable.
    """
    chat_url = normalize_chat_completions_url(url)
    if not chat_url:
        return False, "No HTTP endpoint configured."
    models_url = chat_url.rsplit("/chat/completions", 1)[0] + "/models"
    request = urllib.request.Request(
        models_url,
        headers={
            "Accept": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.5, timeout)) as response:
            status = getattr(response, "status", response.getcode())
        if 200 <= int(status) < 300:
            return True, "Connected."
        return False, f"Endpoint returned HTTP {status}."
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "Endpoint rejected the API key."
        return False, f"Endpoint returned HTTP {exc.code}."
    except urllib.error.URLError as exc:
        return False, f"Could not reach endpoint: {getattr(exc, 'reason', exc)}"
    except TimeoutError:
        return False, "Endpoint connection timed out."
    except Exception as exc:
        return False, f"Could not reach endpoint: {exc}"


def _content_text(content: Any) -> str:
    """Extract text from OpenAI-compatible content fields (str or blocks)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("text", "output_text"):
                    parts.append(str(block.get("text") or ""))
                elif block.get("text"):
                    parts.append(str(block.get("text")))
        return "\n".join(parts)
    return str(content)


# ----------------------------------------------------------------------
# Generic OpenAI-compatible HTTP
# ----------------------------------------------------------------------
class HTTPLLM(LLMClient):
    def __init__(
        self,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__()
        raw = url or os.environ.get("LLM_HTTP_URL")
        if not raw:
            raise RuntimeError("LLM_HTTP_URL not set")
        self.url = normalize_chat_completions_url(raw)
        self.api_key = api_key or os.environ.get("LLM_HTTP_KEY", "")
        self.model = model or os.environ.get("LLM_HTTP_MODEL", "default")

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
            _raise_if_quota(raw, e.code)
            raise RuntimeError(f"HTTP LLM request failed ({e.code}): {raw[:500]}") from e
        except TimeoutError as e:
            raise LLMTimeoutError(
                f"LLM request timed out after {self.timeout_seconds:.0f}s"
            ) from e
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            if "timed out" in reason.lower() or "timeout" in reason.lower():
                raise LLMTimeoutError(
                    f"LLM request timed out after {self.timeout_seconds:.0f}s"
                ) from e
            raise RuntimeError(f"HTTP LLM request failed: {e}") from e

        usage = body.get("usage") or {}
        if usage:
            self.usage.add(
                prompt=usage.get("prompt_tokens") or usage.get("input_tokens") or 0,
                completion=usage.get("completion_tokens")
                or usage.get("output_tokens")
                or 0,
                total=usage.get("total_tokens"),
            )
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            message = {}
        text = _content_text(message.get("content"))
        if not text.strip():
            text = _content_text(
                message.get("reasoning_content") or message.get("reasoning")
            )
        if text.strip():
            return text
        try:
            return _content_text(body["content"])
        except Exception as e:
            raise RuntimeError(f"Unexpected LLM response shape: {body}") from e
