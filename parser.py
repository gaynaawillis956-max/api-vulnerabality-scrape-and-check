"""
Async OpenAI key validation engine.

Two-stage validation for every key:
  Stage 1 — GET /v1/models
    Confirms the key is authentic and retrieves the account's model list + rate limits.
    A 200 here does NOT guarantee the key has billing credits.

  Stage 2 — POST /v1/chat/completions  (gpt-4o-mini, 1 token)
    Confirms real credit / quota availability.
    Distinguishes VALID (active) from NO_CREDITS (quota exhausted).

Result statuses
  VALID        — authenticated + has credits, ready to use
  NO_CREDITS   — authenticated, models visible, but quota/billing exhausted
  RESTRICTED   — authenticated, but chat completions blocked for other reason
  INVALID      — bad key (401)
  RATE_LIMITED — rate-limited at the models check stage (429)
  HTTP_xxx     — unexpected HTTP status
  ERR_BODY_xxx — malformed /v1/models body despite 200
  NETWORK_ERROR— connection/timeout failure
"""
import asyncio
import threading
import time
from typing import Any, Callable

import httpx
import probe as _probe

MODELS_ENDPOINT = "https://api.openai.com/v1/models"
CHAT_ENDPOINT   = "https://api.openai.com/v1/chat/completions"
CONCURRENCY     = 30
TIMEOUT         = 15.0

# Model preference order for Roo Code / completions use
_MODEL_PRIORITY = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-4",
    "o1",
    "o1-mini",
    "o3-mini",
    "gpt-3.5-turbo",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_models(r: httpx.Response) -> tuple[list | None, str | None]:
    """Validate the /v1/models response body. Returns (model_ids, None) or (None, reason)."""
    ct = (r.headers.get("content-type") or "").lower()
    if "json" not in ct and r.text and r.text.lstrip()[:1] not in "{[":
        return None, "BAD_CONTENT"
    try:
        data = r.json()
    except ValueError:
        return None, "JSON_PARSE"
    if not isinstance(data, dict) or data.get("object") != "list":
        return None, "NOT_MODELS_LIST"
    items = data.get("data") or []
    ids = [x.get("id") for x in items if isinstance(x, dict) and x.get("id")]
    return (ids, None) if ids else (None, "NO_IDS")


_PREMIUM = {"gpt-4", "gpt-5", "o1-", "o3-", "o4-"}

def _detect_tier(rpm: str, tpm: str, models: list) -> str:
    if str(rpm).isdigit() and int(rpm) > 3:
        return "PAID"
    if str(tpm).isdigit() and int(tpm) > 5000:
        return "PAID"
    if any(any(p in m.lower() for p in _PREMIUM) for m in models if m):
        return "PAID"
    return "FREE"


def _recommend_model(models: list) -> str:
    """Return the best model from *models* for use in Roo Code / chat completions."""
    lower_map = {m.lower(): m for m in models if m}
    for pref in _MODEL_PRIORITY:
        for lm, orig in lower_map.items():
            if pref in lm:
                return orig
    return models[0] if models else "gpt-4o-mini"


def _make_error(key: str, status: str, ms: int) -> dict[str, Any]:
    return {
        "key": key, "status": status,
        "tier": "-", "rpm": "-", "tpm": "-",
        "latency_ms": ms, "chat_latency_ms": -1,
        "recommended_model": "-", "models": [],
    }


# ---------------------------------------------------------------------------
# Stage 2: chat completion verify
# ---------------------------------------------------------------------------

async def _chat_verify(
    client: httpx.AsyncClient,
    key:    str,
    sem:    asyncio.Semaphore,
) -> tuple[str, int]:
    """
    Fire a minimal 1-token chat completion and return (result, latency_ms).

    result is one of:
      "ACTIVE"     — HTTP 200, key has real credits
      "NO_CREDITS" — HTTP 429 with insufficient_quota in body
      "RESTRICTED" — any other failure (model restriction, org issue, etc.)
    """
    async with sem:
        t0 = time.perf_counter()
        try:
            r = await client.post(
                CHAT_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "model":       "gpt-4o-mini",
                    "messages":    [{"role": "user", "content": "."}],
                    "max_tokens":  1,
                    "temperature": 0,
                },
                timeout=TIMEOUT,
            )
            ms = int((time.perf_counter() - t0) * 1000)

            if r.status_code == 200:
                return "ACTIVE", ms

            if r.status_code == 429:
                body = ""
                try:
                    body = r.text or ""
                except Exception:
                    pass
                if "insufficient_quota" in body:
                    return "NO_CREDITS", ms
                return "RESTRICTED", ms

            return "RESTRICTED", ms

        except Exception:
            ms = int((time.perf_counter() - t0) * 1000)
            return "RESTRICTED", ms


# ---------------------------------------------------------------------------
# Stage 1 + 2 combined
# ---------------------------------------------------------------------------

async def _check_key(
    client: httpx.AsyncClient,
    key:    str,
    sem:    asyncio.Semaphore,
    deep:   bool = False,
) -> dict[str, Any]:

    # ── Stage 1: models endpoint ─────────────────────────────────────
    t0 = time.perf_counter()
    try:
        r = await client.get(
            MODELS_ENDPOINT,
            headers={"Authorization": f"Bearer {key}"},
            timeout=TIMEOUT,
        )
        ms = int((time.perf_counter() - t0) * 1000)
    except Exception:
        ms = int((time.perf_counter() - t0) * 1000)
        return _make_error(key, "NETWORK_ERROR", ms)

    if r.status_code == 401:
        return _make_error(key, "INVALID", ms)
    if r.status_code == 429:
        return _make_error(key, "RATE_LIMITED", ms)
    if r.status_code != 200:
        return _make_error(key, f"HTTP_{r.status_code}", ms)

    models, err = _parse_models(r)
    if err:
        return _make_error(key, f"ERR_BODY_{err}", ms)

    rpm = r.headers.get("x-ratelimit-limit-requests", "0")
    tpm = r.headers.get("x-ratelimit-limit-tokens", "0")
    tier = _detect_tier(rpm, tpm, models)
    rec  = _recommend_model(models)

    # ── Stage 2: chat verify ─────────────────────────────────────────
    chat_result, chat_ms = await _chat_verify(client, key, sem)

    if chat_result == "ACTIVE":
        status = "VALID"
    elif chat_result == "NO_CREDITS":
        status = "NO_CREDITS"
    else:
        status = "RESTRICTED"

    result: dict[str, Any] = {
        "key":               key,
        "status":            status,
        "tier":              tier,
        "rpm":               rpm,
        "tpm":               tpm,
        "latency_ms":        ms,
        "chat_latency_ms":   chat_ms,
        "recommended_model": rec,
        "models":            models,
        "probe":             None,
    }

    # Optional deep probe (only for truly active keys)
    if deep and status == "VALID":
        result["probe"] = await _probe._run_probe(client, key, sem, models)

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def _run_async(
    keys:     list,
    callback: Callable | None,
    stop:     threading.Event | None,
    deep:     bool = False,
) -> list:
    sem    = asyncio.Semaphore(CONCURRENCY)
    limits = httpx.Limits(
        max_connections=CONCURRENCY,
        max_keepalive_connections=CONCURRENCY,
    )
    results: list = []
    total = len(keys)

    async with httpx.AsyncClient(limits=limits) as client:
        pending = [_check_key(client, k, sem, deep) for k in keys]
        done_n  = 0
        for coro in asyncio.as_completed(pending):
            if stop and stop.is_set():
                break
            r = await coro
            done_n += 1
            results.append(r)
            if callback:
                callback(done_n, total, r)

    return results


def scan(
    keys:     list,
    callback: Callable | None        = None,
    stop:     threading.Event | None = None,
    deep:     bool                   = False,
) -> list:
    """
    Synchronous entry point — called from the GUI worker thread.

    callback(done: int, total: int, result: dict) is invoked after each key
    completes; safe to enqueue into a thread-safe queue from here.
    """
    return asyncio.run(_run_async(keys, callback, stop, deep))
