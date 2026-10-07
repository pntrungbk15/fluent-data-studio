"""A minimal client for OpenAI-compatible chat endpoints (OpenAI, Azure-style gateways, Ollama, LM Studio, vLLM,
llama.cpp server, Gemini's compatibility endpoint, …), using only the standard library.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

__all__ = ["LlmSettings", "LlmClient", "LlmError", "PROVIDER_PRESETS", "extract_json"]


class LlmError(RuntimeError):
    """A request that failed, with a message meant for the user."""


@dataclass
class LlmSettings:
    """Endpoint settings. ``base_url`` ends before ``/chat/completions`` (for example ``http://localhost:11434/v1``).

    ``api_key`` is optional for local servers. ``json_mode`` asks the server for a JSON object response
    (``response_format``); servers that reject it are retried without it.
    """

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.1
    timeout: float = 180.0
    json_mode: bool = True
    max_tokens: int = 4096

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())

    def to_dict(self, include_key: bool = False) -> Dict[str, Any]:
        data = asdict(self)
        if not include_key:
            data.pop("api_key")
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LlmSettings":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        return cls(**known)


#: Ready-made endpoints; the user still chooses the model (and enters a key for hosted services).
PROVIDER_PRESETS: Dict[str, Dict[str, str]] = {
    "Ollama (local)": {"base_url": "http://localhost:11434/v1", "model": "llama3.2"},
    "LM Studio (local)": {"base_url": "http://localhost:1234/v1", "model": ""},
    "llama.cpp server (local)": {"base_url": "http://localhost:8080/v1", "model": "local"},
    "OpenAI": {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "Google Gemini (OpenAI-compatible)": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                                          "model": "gemini-2.5-flash"},
    "Custom": {"base_url": "", "model": ""},
}


class LlmClient:
    """``LlmClient(settings).chat(messages)`` returns the assistant's text; network and server errors raise LlmError."""

    def __init__(self, settings: LlmSettings) -> None:
        self.settings = settings

    def _url(self, path: str) -> str:
        return self.settings.base_url.rstrip("/") + path

    def _request(self, url: str, payload: Optional[Dict[str, Any]], timeout: float) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", "")
                if isinstance(detail, dict):
                    detail = detail.get("message", "")
            except (ValueError, OSError, AttributeError):
                pass
            raise LlmError(f"the model server answered {exc.code}" + (f": {detail}" if detail else "")) from None
        except (socket.timeout, TimeoutError):
            raise LlmError(f"the model did not answer within {timeout:.0f} s") from None
        except (urllib.error.URLError, OSError) as exc:
            raise LlmError(f"cannot reach {self.settings.base_url}: {getattr(exc, 'reason', exc)}") from None
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise LlmError("the model server did not return JSON") from None

    def chat(self, messages: List[Dict[str, str]], json_output: bool = True) -> str:
        if not self.settings.configured:
            raise LlmError("no language model is configured")
        payload: Dict[str, Any] = {"model": self.settings.model, "messages": messages,
                                   "temperature": float(self.settings.temperature),
                                   "max_tokens": int(self.settings.max_tokens), "stream": False}
        if json_output and self.settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            reply = self._request(self._url("/chat/completions"), payload, self.settings.timeout)
        except LlmError as exc:
            if "response_format" in payload and " 400" in f" {exc}":
                payload.pop("response_format")
                reply = self._request(self._url("/chat/completions"), payload, self.settings.timeout)
            else:
                raise
        try:
            return str(reply["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            raise LlmError("unexpected response from the model server") from None

    def list_models(self) -> List[str]:
        reply = self._request(self._url("/models"), None, 15)
        return sorted(str(m.get("id")) for m in reply.get("data", []) if isinstance(m, dict) and m.get("id"))

    def test(self) -> str:
        """A short round trip; returns the model's reply."""
        return self.chat([{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}])


def extract_json(text: str) -> Any:
    """The first JSON object in ``text`` (models sometimes wrap it in prose or code fences)."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start >= 0:
        depth, in_string, escape = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the model's reply")
