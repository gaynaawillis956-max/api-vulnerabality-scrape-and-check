"""
Key extraction from raw text, files, and directory trees.

Combines the best regex from v3/q (length-gated sk- pattern) with directory
scanning capability not present in earlier builds.
"""
import re
from pathlib import Path

# Matches both project keys (sk-proj-...) and legacy user keys (sk-...).
# Minimum 20 chars after the prefix; duplicates and short fragments rejected below.
_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{20,}")
_MIN_LEN  = 45   # real OpenAI keys are ≥ 51 chars; 45 rejects noise safely

# File extensions worth scanning in directory mode
_SCAN_EXTS = {".txt", ".env", ".log", ".csv", ".json", ".conf", ".ini", ""}


def extract_keys(text: str) -> list:
    """Return a deduplicated, length-filtered list of OpenAI keys found in text."""
    keys, _ = extract_keys_with_stats(text)
    return keys


def extract_keys_with_stats(text: str) -> tuple:
    """
    Return (unique_keys, duplicates_removed).
    duplicates_removed counts keys that appeared more than once in the raw text.
    """
    raw      = [k for k in _PATTERN.findall(text) if len(k) >= _MIN_LEN]
    seen: set  = set()
    out:  list = []
    for k in raw:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out, len(raw) - len(out)


def from_file(path: str | Path) -> list:
    """Extract keys from a single file. Returns [] on any read error."""
    try:
        return extract_keys(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []


def from_directory(dirpath: str | Path) -> list:
    """Recursively scan a directory tree and return all unique keys found."""
    seen: set  = set()
    keys: list = []
    for f in Path(dirpath).rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in _SCAN_EXTS:
            continue
        for k in from_file(f):
            if k not in seen:
                seen.add(k)
                keys.append(k)
    return keys
