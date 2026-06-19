import csv
import json
import logging
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Result directory layout  (anchored to this file's directory so the app
# works correctly no matter which folder you run the command from)
# ---------------------------------------------------------------------------
_ROOT          = Path(__file__).parent
BASE_DIR       = _ROOT / "results"
VALID_DIR      = BASE_DIR / "valid"
INVALID_DIR    = BASE_DIR / "invalid"
RATE_DIR       = BASE_DIR / "rate_limited"
NO_CREDITS_DIR = BASE_DIR / "no_credits"
ERROR_DIR      = BASE_DIR / "errors"
REPORT_DIR     = BASE_DIR / "reports"

_ALL_DIRS = [
    BASE_DIR, VALID_DIR, INVALID_DIR,
    RATE_DIR, NO_CREDITS_DIR, ERROR_DIR, REPORT_DIR,
]


def setup_dirs() -> None:
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Key helpers
# ---------------------------------------------------------------------------
def mask(key: str) -> str:
    """Return a partially masked key safe for display/logging."""
    return key[:16] + "********"


def result_path(status: str) -> Path:
    """Map a result status string to its output file path."""
    if status == "VALID":
        return VALID_DIR      / "keys.txt"
    if status == "INVALID":
        return INVALID_DIR    / "keys.txt"
    if status == "RATE_LIMITED":
        return RATE_DIR       / "keys.txt"
    if status == "NO_CREDITS":
        return NO_CREDITS_DIR / "keys.txt"
    return ERROR_DIR / "keys.txt"   # RESTRICTED, HTTP_xxx, NETWORK_ERROR, …


def normalize_status(status: str) -> str:
    """Collapse any scan result status into one of the 5 stored categories."""
    if status in ("VALID", "INVALID", "RATE_LIMITED", "NO_CREDITS"):
        return status
    if status == "RESTRICTED":
        return "ERROR"
    return "ERROR"


def append_key(path: Path, key: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(key + "\n")


def remove_key(path: Path, key: str) -> None:
    """Remove every occurrence of *key* from a result file."""
    if not path.exists():
        return
    lines = [
        ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if ln.strip() != key
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_known_statuses() -> dict:
    """
    Read all previously saved result files and return {key: status}.
    Used to detect status changes between scan runs.
    """
    mapping = {
        "VALID":        VALID_DIR      / "keys.txt",
        "INVALID":      INVALID_DIR    / "keys.txt",
        "RATE_LIMITED": RATE_DIR       / "keys.txt",
        "NO_CREDITS":   NO_CREDITS_DIR / "keys.txt",
        "ERROR":        ERROR_DIR      / "keys.txt",
    }
    known: dict = {}
    for status, path in mapping.items():
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key = line.strip()
            if key:
                known[key] = status
    return known


def sort_result_files() -> None:
    """
    Re-write each result .txt file with its keys sorted alphabetically.
    Called after a scan completes so files stay tidy across runs.
    """
    for path in (
        VALID_DIR      / "keys.txt",
        INVALID_DIR    / "keys.txt",
        RATE_DIR       / "keys.txt",
        NO_CREDITS_DIR / "keys.txt",
        ERROR_DIR      / "keys.txt",
    ):
        if not path.exists():
            continue
        keys = sorted(set(
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if line.strip()
        ))
        path.write_text("\n".join(keys) + ("\n" if keys else ""), encoding="utf-8")


# ---------------------------------------------------------------------------
# CSV report
# ---------------------------------------------------------------------------
_CSV_FIELDS = [
    # Core validation
    "key", "status", "tier",
    "rpm", "tpm",
    "latency_ms", "chat_latency_ms",
    "recommended_model",
    "model_count", "models",
    # Deep probe — model access
    "probe_gpt35", "probe_gpt4", "probe_gpt4t",
    "probe_gpt4o", "probe_gpt4o_mini",
    "probe_o1mini", "probe_o3mini",
    # Deep probe — rate limit headers
    "rpm_remaining", "rpm_reset",
    "tpm_remaining", "tpm_reset",
    # Deep probe — permissions
    "perm_fine_tuning", "perm_embeddings", "perm_moderation",
    "perm_dall_e", "perm_whisper", "perm_tts",
    "org_id", "project_id",
    # Deep probe timing
    "probe_ms",
]

# Map PROBE_MODELS order to CSV column names
_PROBE_MODEL_COLS = [
    "probe_gpt35", "probe_gpt4", "probe_gpt4t",
    "probe_gpt4o", "probe_gpt4o_mini",
    "probe_o1mini", "probe_o3mini",
]


_STATUS_ORDER = {"VALID": 0, "RATE_LIMITED": 1, "INVALID": 2}


def write_csv_report(results: list, path: Path) -> None:
    # Sort: VALID first → RATE_LIMITED → INVALID → errors; within VALID sort by latency
    def _sort_key(r: dict) -> tuple:
        st  = r.get("status", "")
        pri = _STATUS_ORDER.get(st, 3) if not st.startswith("VALID") else _STATUS_ORDER.get("VALID", 0)
        if st.startswith("VALID"):
            pri = 0
        lat = r.get("latency_ms") or 9999999
        try:
            lat = int(lat)
        except (TypeError, ValueError):
            lat = 9999999
        return (pri, lat)

    sorted_results = sorted(results, key=_sort_key)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for r in sorted_results:
            mlist = r.get("models") or []
            p     = r.get("probe") or {}
            ma    = p.get("model_access") or {}
            rl    = p.get("rate_limits")  or {}
            perm  = p.get("permissions")  or {}

            # Flatten probe model access into ordered columns
            from probe import PROBE_MODELS as _PM
            probe_cols = {
                col: ma.get(model, "-")
                for col, model in zip(_PROBE_MODEL_COLS, _PM)
            }

            row = {
                "key":               mask(r["key"]),
                "status":            r["status"],
                "tier":              r.get("tier", "-"),
                "rpm":               r.get("rpm", "-"),
                "tpm":               r.get("tpm", "-"),
                "latency_ms":        r.get("latency_ms", "-"),
                "chat_latency_ms":   r.get("chat_latency_ms", "-"),
                "recommended_model": r.get("recommended_model", "-"),
                "model_count":       len(mlist),
                "models":            ", ".join(mlist[:5]),
                # rate limit remaining/reset
                "rpm_remaining":     rl.get("rpm_remaining", "-"),
                "rpm_reset":         rl.get("rpm_reset",     "-"),
                "tpm_remaining":     rl.get("tpm_remaining", "-"),
                "tpm_reset":         rl.get("tpm_reset",     "-"),
                # permissions
                "perm_fine_tuning":  _bool(perm.get("fine_tuning")),
                "perm_embeddings":   _bool(perm.get("embeddings")),
                "perm_moderation":   _bool(perm.get("moderation")),
                "perm_dall_e":       _bool(perm.get("dall_e")),
                "perm_whisper":      _bool(perm.get("whisper")),
                "perm_tts":          _bool(perm.get("tts")),
                "org_id":            perm.get("org_id",      "-"),
                "project_id":        perm.get("project_id",  "-"),
                "probe_ms":          p.get("probe_ms",       "-"),
            }
            row.update(probe_cols)
            writer.writerow(row)


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------

def _bool(v) -> str:
    """Convert bool/None probe permission value to YES / NO / - for display."""
    if v is True:  return "YES"
    if v is False: return "NO"
    return "-"


def write_json_report(results: list, path: Path) -> None:
    """
    Write a structured JSON report preserving the full probe sub-dict.
    Keys are masked; all other fields are serialised as-is.
    """
    from collections import Counter
    summary = dict(Counter(r.get("status", "UNKNOWN") for r in results))

    def _serialise(r: dict) -> dict:
        out = dict(r)
        out["key"] = mask(r["key"])
        mlist = out.get("models") or []
        out["model_count"] = len(mlist)
        return out

    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "total":     len(results),
        "summary":   summary,
        "results":   [_serialise(r) for r in results],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def setup_logger(name: str = "key_manager") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh = logging.FileHandler(_ROOT / "key_manager.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger
