"""HTTP client for sending chat completions to the Qubrid API."""

from __future__ import annotations

import json
from os import getenv
import re
import time
from collections.abc import Iterator
from typing import Any

import requests
from dotenv import load_dotenv
from requests.exceptions import ConnectionError as RequestsConnectionError

from config.settings import (
    MAX_TOKENS,
    MODEL_NAME,
    QUBRID_BASE_URL,
    SYSTEM_RESPONSE_RULES,
    TEMPERATURE,
)

load_dotenv()


def _friendly_error_message(response: requests.Response | None, detail: str) -> str:
    """Build a short user-facing error message for failed API calls."""
    if response is None:
        return detail

    try:
        payload = response.json()
    except ValueError:
        payload = {}

    api_message = (
        payload.get("error", {}).get("message")
        or payload.get("message")
        or payload.get("detail")
        or response.text.strip()
    )
    if api_message:
        return f"{detail} ({response.status_code}): {api_message}"
    return f"{detail} ({response.status_code})."


def _sanitize_assistant_content(content: str) -> str:
    """Strip leaked tool-call markup from assistant output."""
    leaked_tool_markup = bool(
        re.search(r"<minimax:tool_call\b.*?</minimax:tool_call>", content, flags=re.IGNORECASE | re.DOTALL)
        or re.search(r"<invoke\b.*?</invoke>", content, flags=re.IGNORECASE | re.DOTALL)
    )

    cleaned = re.sub(
        r"<minimax:tool_call\b.*?</minimax:tool_call>",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<invoke\b.*?</invoke>",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"</?parameter[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?minimax:tool_call[^>]*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if not leaked_tool_markup:
        return cleaned

    lowered = cleaned.lower()
    if (not cleaned) or "let me first extract" in lowered or "tool_call" in lowered or "invoke name" in lowered:
        return (
            "I can analyze the uploaded file, but I could not read usable text from it in the current setup. "
            "Upload a text-extractable document or paste the key sections you want reviewed, and I will analyze them directly."
        )
    return cleaned


_SEARCH_RE = re.compile(r"\[SEARCH:\s*(.+?)\]", re.IGNORECASE | re.DOTALL)
_MAX_AGENT_ROUNDS = 5

# Keywords that signal the user wants current / real-time information.
_SEARCH_INTENT = re.compile(
    r"\b(news|latest|current|today|right now|recent|just happened|"
    r"live|trending|price|weather|score|stock|crypto|bitcoin|ethereum|"
    r"who won|election|breaking|update|2025|2026|this week|this month|"
    r"tell me about .{0,30}today|what.s happening)\b",
    re.IGNORECASE,
)


def _raw_stream(
    messages: list[dict[str, Any]], system_prompt: str, api_key: str
) -> Iterator[str]:
    """Core SSE streaming — yields raw delta tokens from the API."""
    full_message_list = [{"role": "system", "content": system_prompt}] + [
        {"role": m.get("role", ""), "content": m.get("content", "")}
        for m in messages
    ]
    payload = {
        "model": MODEL_NAME,
        "messages": full_message_list,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with requests.post(
        f"{QUBRID_BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        stream=True,
        timeout=120,
    ) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line if isinstance(raw_line, bytes) else raw_line.encode()
            if not line.startswith(b"data: "):
                continue
            data = line[6:]
            if data.strip() == b"[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                if delta:
                    yield delta
            except (json.JSONDecodeError, IndexError, KeyError):
                continue


# Number of characters to buffer before deciding if the model is issuing a search request.
# The model outputs [SEARCH: ...] as the very first content when searching, so 20 chars
# is enough to distinguish it from a normal prose response.
_PROBE_CHARS = 20


def chat_agent(messages: list[dict[str, Any]], persona_system: str) -> Iterator[str]:
    """Agentic streaming loop.

    Tokens are streamed live.  A small probe buffer detects ``[SEARCH: ...]``
    in the first characters of each turn; if found, the web search executes
    and the loop continues.  Otherwise tokens pass straight through.
    """
    from backend.tools import AGENT_SYSTEM_ADDENDUM, web_search

    api_key = getenv("QUBRID_API_KEY", "").strip()
    if not api_key:
        yield "Error: Missing `QUBRID_API_KEY` in your `.env` file."
        return

    # ── Client-side search intent detection ──────────────────────────────
    # Extract the last user message text (handles both str and multimodal list).
    last_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            raw = m.get("content", "")
            if isinstance(raw, list):
                last_user_text = " ".join(
                    p.get("text", "") for p in raw if p.get("type") == "text"
                )
            else:
                last_user_text = str(raw)
            break

    needs_search = bool(_SEARCH_INTENT.search(last_user_text))

    if needs_search:
        # Run the search BEFORE the model responds so results are in context.
        query = last_user_text[:120].strip()
        yield f"🔍 Searching the web…\n\n"
        results = web_search(query)
        augmented = list(messages) + [{
            "role": "user",
            "content": (
                f"[Real-time web search results for your question]:\n\n{results}\n\n"
                "Use these results to answer accurately. Today's date context is provided above."
            ),
        }]
        system_prompt = (
            f"{persona_system}\n\n{SYSTEM_RESPONSE_RULES}\n{AGENT_SYSTEM_ADDENDUM}"
        )
        try:
            yield from _raw_stream(augmented, system_prompt, api_key)
        except Exception as exc:
            yield f"\n\n*Search-augmented response failed: {exc}*"
        return
    # ─────────────────────────────────────────────────────────────────────

    # No search needed — stream the response directly (with model-initiated
    # search as a fallback for cases we didn't catch client-side).
    system_prompt = f"{persona_system}\n\n{SYSTEM_RESPONSE_RULES}\n{AGENT_SYSTEM_ADDENDUM}"
    context = list(messages)

    for _ in range(_MAX_AGENT_ROUNDS):
        try:
            token_gen = _raw_stream(context, system_prompt, api_key)
        except Exception as exc:
            yield f"\n\n*Agent error: {exc}*"
            return

        # --- probe phase: buffer first _PROBE_CHARS to detect [SEARCH: ---
        probe = ""
        search_found = False

        for token in token_gen:
            probe += token

            m = _SEARCH_RE.search(probe)
            if m:
                for _ in token_gen:  # drain remainder
                    pass
                search_found = True
                break

            if len(probe) >= _PROBE_CHARS:
                yield probe
                yield from token_gen
                return

        if not search_found:
            if probe:
                yield probe
            return

        # --- model-initiated tool execution ---
        query = _SEARCH_RE.search(probe).group(1).strip()
        yield f"🔍 Searching: *{query}*…\n\n"
        results = web_search(query)
        context.append({"role": "assistant", "content": probe})
        context.append({
            "role": "user",
            "content": (
                f"[Web search results for '{query}']:\n\n{results}\n\n"
                "Now answer the user's question using these results."
            ),
        })

    yield from chat_stream(context, persona_system)


def chat_stream(messages: list[dict[str, Any]], persona_system: str) -> Iterator[str]:
    """Stream chat completion tokens from Qubrid, yielding text chunks as they arrive."""
    api_key = getenv("QUBRID_API_KEY", "").strip()
    if not api_key:
        yield "Error: Missing `QUBRID_API_KEY` in your `.env` file."
        return

    system_prompt = f"{persona_system}\n\n{SYSTEM_RESPONSE_RULES}"
    full_message_list = [{"role": "system", "content": system_prompt}] + [
        {"role": message.get("role", ""), "content": message.get("content", "")}
        for message in messages
    ]
    payload = {
        "model": MODEL_NAME,
        "messages": full_message_list,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with requests.post(
            f"{QUBRID_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=120,
        ) as response:
            response.raise_for_status()
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                line = raw_line if isinstance(raw_line, bytes) else raw_line.encode()
                if not line.startswith(b"data: "):
                    continue
                data = line[6:]
                if data.strip() == b"[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
                    if delta:
                        yield delta
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue
    except RequestsConnectionError:
        yield "\n\n*Could not reach Qubrid AI. Check your internet connection.*"
    except requests.RequestException as exc:
        yield f"\n\n*Request error: {exc}*"


def chat(messages: list[dict[str, Any]], persona_system: str) -> dict[str, Any]:
    """Send the full conversation context to Qubrid and return a normalized payload."""
    api_key = getenv("QUBRID_API_KEY", "").strip()
    if not api_key:
        return {
            "error": "Missing `QUBRID_API_KEY` in your `.env` file.",
            "latency_ms": 0.0,
        }

    system_prompt = f"{persona_system}\n\n{SYSTEM_RESPONSE_RULES}"
    full_message_list = [{"role": "system", "content": system_prompt}] + [
        {"role": message.get("role", ""), "content": message.get("content", "")}
        for message in messages
    ]

    payload = {
        "model": MODEL_NAME,
        "messages": full_message_list,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    start_time = time.time()
    response: requests.Response | None = None
    try:
        response = requests.post(
            f"{QUBRID_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        latency_ms = round((time.time() - start_time) * 1000, 2)
        response.raise_for_status()
        data = response.json()

        choices = data.get("choices") or []
        if not choices:
            return {
                "error": "Qubrid returned no completion choices.",
                "latency_ms": latency_ms,
            }

        message = choices[0].get("message") or {}
        usage = data.get("usage") or {}
        return {
            "content": _sanitize_assistant_content(message.get("content", "").strip()),
            "tokens_used": int(usage.get("total_tokens", 0) or 0),
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "latency_ms": latency_ms,
        }
    except RequestsConnectionError:
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return {
            "error": (
                "Qubrid endpoint could not be reached. "
                "Check your internet connection and confirm `QUBRID_BASE_URL` is correct."
            ),
            "latency_ms": latency_ms,
        }
    except requests.RequestException as exc:
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return {
            "error": _friendly_error_message(response, f"Unable to reach Qubrid AI: {exc}"),
            "latency_ms": latency_ms,
        }
    except ValueError as exc:
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return {
            "error": f"Qubrid returned an unreadable response: {exc}",
            "latency_ms": latency_ms,
        }
