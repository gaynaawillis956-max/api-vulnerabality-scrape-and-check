# OpenAI Key Manager

Validate large batches of OpenAI API keys concurrently, categorise results,
and export detailed CSV reports — with a responsive Tkinter GUI and an
optional headless CLI mode.

---

## Quick start

```bash
cd key_manager_final
pip3 install -r requirements.txt   # or skip — main.py auto-installs on first run
python3 main.py                    # launches the GUI
```

## CLI mode

```bash
python3 main.py --cli              # reads raw_keys.txt in the current directory
python3 main.py --cli keys.txt     # reads a specific file
```

---

## What it does per key

| Check | Detail |
|-------|--------|
| `GET /v1/models` | Primary validity probe |
| Response body validation | Rejects proxies returning bogus 200s |
| Tier detection | FREE vs PAID (RPM / TPM headers + model list) |
| Rate-limit headers | `x-ratelimit-limit-requests`, `x-ratelimit-limit-tokens` |
| Latency measurement | Milliseconds per key |

---

## Result categories

| Status | Meaning |
|--------|---------|
| `VALID/PAID` | Active key with paid-tier access |
| `VALID/FREE` | Active key on free tier |
| `INVALID` | 401 — revoked or wrong key |
| `RATE_LIMITED` | 429 — temporarily blocked |
| `NETWORK_ERROR` | Connection timeout or DNS failure |
| `HTTP_xxx` | Unexpected HTTP status |

---

## Output files

```
results/
  valid/keys.txt          all valid keys (one per line)
  invalid/keys.txt        invalid keys
  rate_limited/keys.txt   rate-limited keys
  errors/keys.txt         network / unexpected errors
  reports/report_<ts>.csv full CSV report with tier, RPM, TPM, latency, models
```

---

## Module layout

| File | Responsibility |
|------|---------------|
| `main.py` | Entry point — GUI or `--cli` dispatch |
| `installer.py` | Auto-installs `httpx` and `tqdm` on first run |
| `scanner.py` | Regex key extraction from text / files / directories |
| `parser.py` | Async HTTP validation engine (30 concurrent workers) |
| `gui.py` | Tkinter GUI with thread-safe queue, stop button, live stats |
| `utils.py` | Shared paths, CSV writer, key masking, logger |

---

## Requirements

- Python 3.10+
- `httpx>=0.27.0`
- `tqdm>=4.60.0`
- Tkinter (included with standard Python on Windows/macOS)
