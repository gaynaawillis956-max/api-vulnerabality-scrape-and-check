"""
OpenAI Key Manager — entry point.

Usage:
    python3 main.py                        # launches the GUI (default)
    python3 main.py --cli [file]           # headless CLI scan
    python3 main.py --cli [file] --deep    # CLI + deep probe (model access, permissions)
    python3 main.py --cli [file] --json    # CLI + write JSON report alongside CSV
"""

# ---------------------------------------------------------------------------
# Bootstrap: install missing packages before ANY other import
# ---------------------------------------------------------------------------
import importlib
import subprocess
import sys

def _bootstrap() -> None:
    needed = []
    for pkg in ("httpx", "tqdm"):
        try:
            importlib.import_module(pkg)
        except ImportError:
            needed.append(pkg)
    if needed:
        print(f"[installer] Installing: {', '.join(needed)} …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *needed, "-q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("[installer] Done.\n")

_bootstrap()
# ---------------------------------------------------------------------------


def _cli(args: list) -> None:
    """Minimal CLI mode for scripting / headless environments."""
    from pathlib import Path

    from scanner import from_file
    from parser  import scan
    from utils   import (
        setup_dirs, mask, append_key, write_csv_report, write_json_report,
        result_path, timestamp, setup_logger,
        REPORT_DIR,
    )

    # Parse flags
    deep      = "--deep" in args
    emit_json = "--json" in args
    file_args = [a for a in args if not a.startswith("--")]

    logger = setup_logger()
    setup_dirs()

    # Resolve input file
    if file_args:
        path = Path(file_args[0])
        if not path.exists():
            logger.error("File not found: %s", path)
            sys.exit(1)
        keys = from_file(path)
    else:
        fallback = Path("raw_keys.txt")
        if not fallback.exists():
            logger.error("No file given and raw_keys.txt not found. "
                         "Usage: python main.py --cli [file] [--deep] [--json]")
            sys.exit(1)
        keys = from_file(fallback)

    if not keys:
        logger.error("No API keys found in input.")
        sys.exit(1)

    mode_note = " [DEEP PROBE]" if deep else ""
    print(f"\nFound {len(keys)} unique keys - scanning with 30 concurrent workers...{mode_note}\n")

    counts = {"VALID": 0, "NO_CREDITS": 0, "INVALID": 0, "RATE_LIMITED": 0, "OTHER": 0}

    def callback(done: int, total: int, r: dict) -> None:
        status = r["status"]
        masked = mask(r["key"])

        if status == "VALID":
            counts["VALID"] += 1
            tier      = r.get("tier", "?")
            rec_model = r.get("recommended_model", "-")
            print(f"  [VALID/{tier}]  {masked}  RPM:{r.get('rpm')}  "
                  f"chat:{r.get('chat_latency_ms')}ms  model:{rec_model}")
            probe = r.get("probe")
            if probe:
                _print_probe(probe)
        elif status == "NO_CREDITS":
            counts["NO_CREDITS"] += 1
            print(f"  [NO_CREDITS]  {masked}  (valid key, quota exhausted)")
        elif status == "INVALID":
            counts["INVALID"] += 1
            print(f"  [INVALID]     {masked}")
        elif status == "RATE_LIMITED":
            counts["RATE_LIMITED"] += 1
            print(f"  [RATE_LIMITED]{masked}")
        else:
            counts["OTHER"] += 1
            print(f"  [{status}] {masked}")

        append_key(result_path(status), r["key"])
        print(f"  ({done}/{total})", end="\r")

    results = scan(keys, callback=callback, deep=deep)

    ts       = timestamp()
    csv_path = REPORT_DIR / f"report_{ts}.csv"
    write_csv_report(results, csv_path)

    summary_lines = [
        f"\n{'-'*54}",
        f"  Valid:        {counts['VALID']}",
        f"  No-Credits:   {counts['NO_CREDITS']}",
        f"  Invalid:      {counts['INVALID']}",
        f"  Rate-limited: {counts['RATE_LIMITED']}",
        f"  Errors:       {counts['OTHER']}",
        f"  CSV report  -> {csv_path}",
    ]

    if emit_json or deep:
        json_path = REPORT_DIR / f"report_{ts}.json"
        write_json_report(results, json_path)
        summary_lines.append(f"  JSON report -> {json_path}")

    summary_lines.append(f"{'-'*54}\n")
    print("\n".join(summary_lines))


def _print_probe(probe: dict) -> None:
    """Print a condensed deep-probe summary to stdout (ASCII-safe for Windows console)."""
    pad = "               "
    ma  = probe.get("model_access") or {}
    if ma:
        _sym = {"ACCESSIBLE": "OK", "PERMISSION_DENIED": "NO",
                "NOT_AVAILABLE": "--", "RATE_LIMITED": "RL", "NO_CREDITS": "NC"}
        parts = [
            f"{m.replace('gpt-3.5-turbo','3.5').replace('gpt-4o-mini','4o-mini').replace('gpt-4o','4o').replace('gpt-4-turbo','4t').replace('gpt-4','4')}:"
            f"{_sym.get(s,'??')}"
            for m, s in ma.items()
        ]
        print(f"{pad}Models: {'  '.join(parts)}")

    rl = probe.get("rate_limits") or {}
    if rl.get("rpm_limit", "-") != "-":
        print(f"{pad}RPM {rl.get('rpm_remaining')}/{rl.get('rpm_limit')} "
              f"(reset {rl.get('rpm_reset')})  "
              f"TPM {rl.get('tpm_remaining')}/{rl.get('tpm_limit')} "
              f"(reset {rl.get('tpm_reset')})")

    perm = probe.get("permissions") or {}
    if perm:
        def yn(v): return "YES" if v is True else "NO" if v is False else "-"
        print(f"{pad}Perms: embed={yn(perm.get('embeddings'))}  "
              f"ft={yn(perm.get('fine_tuning'))}  "
              f"dall-e={yn(perm.get('dall_e'))}  "
              f"whisper={yn(perm.get('whisper'))}  "
              f"tts={yn(perm.get('tts'))}")
        org  = perm.get("org_id",     "-")
        proj = perm.get("project_id", "-")
        if org != "-" or proj != "-":
            print(f"{pad}Org: {org}  Project: {proj}")


def _gui() -> None:
    from gui import launch
    launch()


if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--cli":
        _cli(argv[1:])
    else:
        _gui()
