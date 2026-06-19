"""
Deep Key Probe — runs ONLY on keys that already passed basic validation (VALID).

Tests performed per key
-----------------------
1. Model access  — 1-token chat completion against every model in PROBE_MODELS
                   With exponential backoff on 429 (up to MAX_RETRIES).
2. Rate limits   — Reads remaining/reset headers from the first accessible model response.
3. Permissions   — Feature checks:
                     fine_tuning  : GET  /v1/fine_tuning/jobs
                     embeddings   : POST /v1/embeddings
                     moderation   : POST /v1/moderations
                   Model-list inference (no extra calls):
                     dall_e       : dall-e-2 / dall-e-3 in models list
                     whisper      : whisper-1 in models list
                     tts          : tts-1 / tts-1-hd in models list
4. Org / project — Read openai-organization and openai-project response headers.

Result schema (dict returned by probe_key / awaited from _run_probe)
---------------------------------------------------------------------
{
  "model_access": { model_id: "ACCESSIBLE" | "PERMISSION_DENIED" |
                               "NOT_AVAILABLE" | "RATE_LIMITED" |
                               "NETWORK_ERROR" | "ERR_{code}" },
  "rate_limits":  { rpm_limit, rpm_remaining, rpm_reset,
                    tpm_limit, tpm_remaining, tpm_reset },
  "permissions":  { fine_tuning, embeddings, moderation,
                    dall_e, whisper, tts,
                    org_id, project_id },
  "probe_ms": int,   # total wall time
}
"""

import asyncio
import time
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://api.openai.com"

PROBE_MODELS: list[str] = [
    "gpt-3.5-turbo",
    "gpt-4",
    "gpt-4-turbo",
    "gpt-4o",
    "gpt-4o-mini",
    "o1-mini",
    "o3-mini",
]

MAX_RETRIES    = 3      # per model on 429
PROBE_TIMEOUT  = 12.0   # seconds per request
PROBE_CONC     = 8      # concurrent sub-requests during probe (stay polite)

_BACKOFF = [1, 2, 4]    # seconds to wait before each retry attempt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _access_status(r: httpx.Response) -> str:
    """Map an HTTP response to a short model-access status string."""
    if r.status_code == 200:
        return "ACCESSIBLE"
    if r.status_code in (403, 401):
        return "PERMISSION_DENIED"
    if r.status_code == 404:
        try:
            code = r.json().get("error", {}).get("code", "")
            if code in ("model_not_found", "invalid_model"):
                return "NOT_AVAILABLE"
        except Exception:
            pass
        return "NOT_AVAILABLE"
    if r.status_code == 429:
        try:
            code = r.json().get("error", {}).get("code", "")
            if code == "insufficient_quota":
                return "NO_CREDITS"
        except Exception:
            pass
        return "RATE_LIMITED"
    return f"ERR_{r.status_code}"


def _rate_headers(r: httpx.Response) -> dict:
    """Extract the six rate-limit headers from any response."""
    h = r.headers
    return {
        "rpm_limit":      h.get("x-ratelimit-limit-requests",     "-"),
        "rpm_remaining":  h.get("x-ratelimit-remaining-requests",  "-"),
        "rpm_reset":      h.get("x-ratelimit-reset-requests",      "-"),
        "tpm_limit":      h.get("x-ratelimit-limit-tokens",        "-"),
        "tpm_remaining":  h.get("x-ratelimit-remaining-tokens",    "-"),
        "tpm_reset":      h.get("x-ratelimit-reset-tokens",        "-"),
    }


def _org_headers(r: httpx.Response) -> tuple[str, str]:
    """Return (org_id, project_id) from response headers."""
    org = r.headers.get("openai-organization", "-")
    proj = r.headers.get("openai-project", "-")
    return org, proj


# ---------------------------------------------------------------------------
# Stage 1: per-model access test (with retry / backoff)
# ---------------------------------------------------------------------------

async def _test_model(
    client: httpx.AsyncClient,
    key:    str,
    model:  str,
    sem:    asyncio.Semaphore,
) -> tuple[str, httpx.Response | None]:
    """
    Returns (status_string, response_or_None).
    Retries on RATE_LIMITED up to MAX_RETRIES with exponential backoff.
    """
    for attempt in range(MAX_RETRIES + 1):
        async with sem:
            try:
                r = await client.post(
                    f"{BASE_URL}/v1/chat/completions",
                    headers=_auth(key),
                    json={
                        "model":       model,
                        "messages":    [{"role": "user", "content": "."}],
                        "max_tokens":  1,
                        "temperature": 0,
                    },
                    timeout=PROBE_TIMEOUT,
                )
                st = _access_status(r)
                if st == "RATE_LIMITED" and attempt < MAX_RETRIES:
                    # Parse retry-after or use backoff table
                    wait = float(r.headers.get("retry-after", _BACKOFF[attempt]))
                    await asyncio.sleep(min(wait, 30))
                    continue
                return st, (r if st == "ACCESSIBLE" else None)
            except Exception:
                return "NETWORK_ERROR", None
    return "RATE_LIMITED", None


# ---------------------------------------------------------------------------
# Stage 2: feature permission checks
# ---------------------------------------------------------------------------

async def _check_fine_tuning(
    client: httpx.AsyncClient, key: str, sem: asyncio.Semaphore
) -> bool:
    async with sem:
        try:
            r = await client.get(
                f"{BASE_URL}/v1/fine_tuning/jobs",
                headers=_auth(key),
                timeout=PROBE_TIMEOUT,
            )
            return r.status_code == 200
        except Exception:
            return False


async def _check_embeddings(
    client: httpx.AsyncClient, key: str, sem: asyncio.Semaphore
) -> tuple[bool, httpx.Response | None]:
    async with sem:
        try:
            r = await client.post(
                f"{BASE_URL}/v1/embeddings",
                headers=_auth(key),
                json={"input": ".", "model": "text-embedding-ada-002"},
                timeout=PROBE_TIMEOUT,
            )
            return r.status_code == 200, (r if r.status_code == 200 else None)
        except Exception:
            return False, None


async def _check_moderation(
    client: httpx.AsyncClient, key: str, sem: asyncio.Semaphore
) -> tuple[bool, httpx.Response | None]:
    async with sem:
        try:
            r = await client.post(
                f"{BASE_URL}/v1/moderations",
                headers=_auth(key),
                json={"input": "."},
                timeout=PROBE_TIMEOUT,
            )
            return r.status_code == 200, (r if r.status_code == 200 else None)
        except Exception:
            return False, None


# ---------------------------------------------------------------------------
# Probe orchestrator
# ---------------------------------------------------------------------------

async def _run_probe(
    client:           httpx.AsyncClient,
    key:              str,
    sem:              asyncio.Semaphore,
    models_from_stage1: list,
) -> dict[str, Any]:
    """
    Async orchestrator — called directly from parser._check_key() so it
    shares the existing AsyncClient and semaphore.
    """
    t0 = time.perf_counter()

    # Launch all model tests and feature checks concurrently
    model_coros  = {m: _test_model(client, key, m, sem) for m in PROBE_MODELS}
    ft_coro      = _check_fine_tuning(client, key, sem)
    emb_coro     = _check_embeddings(client, key, sem)
    mod_coro     = _check_moderation(client, key, sem)

    (
        *model_results_raw,
        ft_ok,
        (emb_ok, emb_resp),
        (mod_ok, mod_resp),
    ) = await asyncio.gather(
        *[model_coros[m] for m in PROBE_MODELS],
        ft_coro,
        emb_coro,
        mod_coro,
    )

    # Build model_access dict
    model_access: dict[str, str] = {}
    first_ok_resp: httpx.Response | None = None
    org_id = "-"
    project_id = "-"

    for model, (status, resp) in zip(PROBE_MODELS, model_results_raw):
        model_access[model] = status
        if resp is not None and first_ok_resp is None:
            first_ok_resp = resp
            org_id, project_id = _org_headers(resp)

    # Rate limits from best response available
    rate_limits: dict[str, str] = {}
    for resp in (first_ok_resp, emb_resp, mod_resp):
        if resp is not None:
            rate_limits = _rate_headers(resp)
            if org_id == "-":
                org_id, project_id = _org_headers(resp)
            break

    if not rate_limits:
        rate_limits = {
            "rpm_limit": "-", "rpm_remaining": "-", "rpm_reset": "-",
            "tpm_limit": "-", "tpm_remaining": "-", "tpm_reset": "-",
        }

    # Model-list inferred permissions (no extra API calls)
    ml_lower = [m.lower() for m in (models_from_stage1 or [])]
    dall_e   = any("dall-e" in m for m in ml_lower)
    whisper  = any("whisper" in m for m in ml_lower)
    tts      = any(m.startswith("tts") for m in ml_lower)

    permissions = {
        "fine_tuning": ft_ok,
        "embeddings":  emb_ok,
        "moderation":  mod_ok,
        "dall_e":      dall_e,
        "whisper":     whisper,
        "tts":         tts,
        "org_id":      org_id,
        "project_id":  project_id,
    }

    return {
        "model_access": model_access,
        "rate_limits":  rate_limits,
        "permissions":  permissions,
        "probe_ms":     int((time.perf_counter() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# Convenience sync wrapper (CLI / standalone use)
# ---------------------------------------------------------------------------

def probe_key_sync(key: str, models_from_stage1: list | None = None) -> dict[str, Any]:
    """
    Synchronous entry point for use outside an existing event loop.
    Creates its own AsyncClient and event loop.
    """
    async def _run() -> dict[str, Any]:
        sem = asyncio.Semaphore(PROBE_CONC)
        limits = httpx.Limits(
            max_connections=PROBE_CONC,
            max_keepalive_connections=PROBE_CONC,
        )
        async with httpx.AsyncClient(limits=limits) as client:
            return await _run_probe(client, key, sem, models_from_stage1 or [])

    return asyncio.run(_run())
