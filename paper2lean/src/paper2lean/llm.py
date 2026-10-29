from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .runtime import BudgetExceeded, UnitBudget


class LLMDispatcher:
    """Stage-aware adapter for the existing HTTP client or a compatible client."""
    def __init__(self, client: Any = None, *, url: str | None = None, model: str | None = None, api_key: str | None = None):
        self.client = client
        self.url = (url if url is not None else os.environ.get("LLM_HTTP_URL", "")).rstrip("/")
        if self.url and not self.url.endswith("/chat/completions"):
            self.url += "/chat/completions"
        self.model = model or os.environ.get("LLM_HTTP_MODEL", "default")
        self.api_key = api_key if api_key is not None else os.environ.get("LLM_HTTP_KEY", "")
        self.last_error = ""
        self.calls = 0

    @property
    def available(self) -> bool:
        return self.client is not None or bool(self.url)

    def ask(self, stage: str, system: str, payload: dict[str, Any], *, temperature: float = 0.1,
            max_tokens: int = 4096, json_output: bool = True,
            budget: UnitBudget | None = None) -> tuple[Any, bool]:
        if budget:
            budget.remaining()
        if not self.client and not self.url:
            return None, False
        instruction = ("Return only one valid JSON object.\n" if json_output else "Return only the requested Lean code.\n")
        content = instruction + "\nSTAGE: " + stage + "\nINPUT:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        self.calls += 1
        raw = ""
        attempts = max(1, int(os.environ.get("PAPER2LEAN_LLM_RETRIES", "2")))
        for attempt in range(attempts):
            timeout = float(os.environ.get("LLM_TIMEOUT", "300"))
            if budget:
                timeout = min(timeout, budget.remaining())
            try:
                if self.client:
                    raw = self.client.chat(messages, temperature=temperature, max_tokens=max_tokens)
                else:
                    headers = {"Content-Type": "application/json"}
                    if self.api_key:
                        headers["Authorization"] = f"Bearer {self.api_key}"
                    request = urllib.request.Request(self.url, data=json.dumps({"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}).encode(), headers=headers, method="POST")
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        chunks = []
                        while True:
                            if budget:
                                remaining = min(timeout, budget.remaining())
                                # Refresh the socket timeout as the absolute unit
                                # deadline approaches, including streamed bytes.
                                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                                if sock:
                                    sock.settimeout(remaining)
                            chunk = response.read1(65536)
                            if not chunk:
                                break
                            chunks.append(chunk)
                        body = json.loads(b"".join(chunks).decode("utf-8", errors="replace"))
                    raw = body["choices"][0]["message"]["content"]
                if budget:
                    budget.remaining()
                self.last_error = ""
                break
            except BudgetExceeded:
                raise
            except urllib.error.HTTPError as exc:
                detail = exc.read(4096).decode("utf-8", errors="replace") if exc.fp else str(exc)
                self.last_error = f"LLM HTTP {exc.code}: {detail[:500]}"
                if exc.code != 524 or attempt + 1 >= attempts:
                    return ({"_llm_error": self.last_error} if json_output else ""), True
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if budget:
                    budget.remaining()
                self.last_error = f"LLM request failed: {exc}"
                if attempt + 1 >= attempts:
                    return ({"_llm_error": self.last_error} if json_output else ""), True
            delay = min(2.0 * (attempt + 1), 5.0)
            if budget:
                delay = min(delay, budget.remaining())
            time.sleep(delay)
        if not json_output:
            return raw.strip(), True
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(text), True
        except json.JSONDecodeError as exc:
            return {"_parse_error": str(exc), "raw": raw}, True

    def probe(self) -> tuple[bool, str]:
        if not self.url and not self.client:
            return False, "No endpoint configured"
        if self.client:
            return True, "Client configured"
        try:
            headers = {"Content-Type": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            request = urllib.request.Request(self.url, data=json.dumps({"model": self.model, "messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 4, "temperature": 0}).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=float(os.environ.get("LLM_TIMEOUT", "10"))) as response:
                return response.status < 400, f"HTTP {response.status}"
        except Exception as exc:
            return False, str(exc)
