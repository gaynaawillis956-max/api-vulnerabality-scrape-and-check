"""
Tkinter GUI for OpenAI Key Manager.

Bootstrap runs FIRST — safe to run directly:  python3 gui.py

Features
--------
• Auto-dedup  : strips duplicate keys before every scan; reports how many removed
• Smart save  : before writing, checks previous status of each key
    - same status      → skip (no duplicate lines in output files)
    - status changed   → remove from old file, write to new file, log the change
    - never seen       → append normally
• Re-check Valids : one-click button loads results/valid/keys.txt and re-scans
• Auto-sort   : after every scan, all result files are sorted + deduped on disk
• Models shown: valid keys show full model list in the log

Threading model (v8 pattern — zero direct GUI calls from worker thread):
  Worker thread  → parser.scan() → puts dicts into self._q
  Main thread    → root.after(100, _poll) drains self._q and updates widgets
"""

# ---------------------------------------------------------------------------
# Step 1: auto-install missing packages BEFORE any external import
# ---------------------------------------------------------------------------
import importlib
import subprocess
import sys

def _bootstrap() -> None:
    needed = [p for p in ("httpx", "tqdm") if not _can_import(p)]
    if needed:
        print(f"[installer] Installing: {', '.join(needed)} …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *needed, "-q"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("[installer] Done.\n")

def _can_import(pkg: str) -> bool:
    try:
        importlib.import_module(pkg)
        return True
    except ImportError:
        return False

_bootstrap()

# ---------------------------------------------------------------------------
# Step 2: all imports — now safe
# ---------------------------------------------------------------------------
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from parser  import scan
from scanner import extract_keys_with_stats, from_directory
from utils   import (
    append_key, mask, normalize_status, remove_key,
    result_path, setup_dirs, sort_result_files,
    load_known_statuses, timestamp, write_csv_report, write_json_report,
    REPORT_DIR, VALID_DIR, RATE_DIR, NO_CREDITS_DIR,
)

_POLL_MS = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _model_summary(models: list, limit: int = 6) -> str:
    if not models:
        return "(no model list returned)"
    shown = models[:limit]
    extra = len(models) - len(shown)
    s = ", ".join(shown)
    return s + f"  (+{extra} more)" if extra else s


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class _App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("OpenAI Key Manager")
        root.minsize(820, 700)

        self._q:       queue.Queue     = queue.Queue()
        self._stop:    threading.Event = threading.Event()
        self._running: bool            = False
        self._results: list            = []
        self._known:   dict            = {}   # {key: normalized_status} from previous runs
        self._deep_var: tk.BooleanVar  = tk.BooleanVar(value=False)

        self._valid = self._invalid = self._rate = self._errors = 0
        self._no_credits = 0
        self._changed = self._dupes = 0       # status-change and dedup counters

        setup_dirs()
        self._build_ui()
        self._poll()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = self.root

        # ── Input ──────────────────────────────────────────────────────
        in_frame = ttk.LabelFrame(root, text="Paste keys / load a file", padding=6)
        in_frame.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        self._input = tk.Text(in_frame, height=9, wrap="none", font=("Consolas", 9))
        sb_y = ttk.Scrollbar(in_frame, command=self._input.yview)
        sb_x = ttk.Scrollbar(in_frame, orient="horizontal", command=self._input.xview)
        self._input.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
        sb_y.pack(side="right",  fill="y")
        sb_x.pack(side="bottom", fill="x")
        self._input.pack(fill="both", expand=True)

        # ── Buttons row 1 ──────────────────────────────────────────────
        btn_row = tk.Frame(root)
        btn_row.pack(fill="x", padx=10, pady=(4, 0))

        ttk.Button(btn_row, text="Load File",      command=self._load_file).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Load Directory", command=self._load_dir ).pack(side="left", padx=3)
        ttk.Button(btn_row, text="Clear",          command=self._clear    ).pack(side="left", padx=3)

        tk.Frame(btn_row, width=16).pack(side="left")

        self._btn_start = ttk.Button(btn_row, text="▶  Start Scan", command=self._start)
        self._btn_start.pack(side="left", padx=3)

        self._btn_stop = ttk.Button(btn_row, text="■  Stop", command=self._stop_scan,
                                    state="disabled")
        self._btn_stop.pack(side="left", padx=3)

        ttk.Checkbutton(
            btn_row, text="🔬 Deep Probe",
            variable=self._deep_var,
        ).pack(side="left", padx=(12, 3))

        # ── Buttons row 2 (re-check / save) ───────────────────────────
        btn_row2 = tk.Frame(root)
        btn_row2.pack(fill="x", padx=10, pady=(2, 4))

        self._btn_recheck = ttk.Button(
            btn_row2, text="↺  Re-check Valids",
            command=self._recheck_valids,
        )
        self._btn_recheck.pack(side="left", padx=3)

        self._btn_recheck_nc = ttk.Button(
            btn_row2, text="↺  Re-check No-Credits",
            command=self._recheck_no_credits,
        )
        self._btn_recheck_nc.pack(side="left", padx=3)

        self._btn_recheck_rate = ttk.Button(
            btn_row2, text="↺  Re-check Rate-Limited",
            command=self._recheck_rate_limited,
        )
        self._btn_recheck_rate.pack(side="left", padx=3)

        ttk.Button(btn_row2, text="💾  Save Report",
                   command=self._save_report).pack(side="right", padx=3)
        ttk.Button(btn_row2, text="📄  Save JSON",
                   command=self._save_json).pack(side="right", padx=3)

        # ── Stats row ──────────────────────────────────────────────────
        stats = tk.Frame(root)
        stats.pack(fill="x", padx=12, pady=2)

        self._lbl_found      = ttk.Label(stats, text="Keys: 0")
        self._lbl_valid      = ttk.Label(stats, text="Valid: 0",         foreground="#1a8f1a")
        self._lbl_no_credits = ttk.Label(stats, text="No-Credits: 0",    foreground="#e07b00")
        self._lbl_invalid    = ttk.Label(stats, text="Invalid: 0",       foreground="#cc2222")
        self._lbl_rate       = ttk.Label(stats, text="Rate-lim: 0",      foreground="#cc7700")
        self._lbl_errors     = ttk.Label(stats, text="Errors: 0",        foreground="#888888")
        self._lbl_changed    = ttk.Label(stats, text="Changed: 0",       foreground="#8800cc")
        self._lbl_dupes      = ttk.Label(stats, text="Dupes skipped: 0", foreground="#555555")

        for lbl in (self._lbl_found, self._lbl_valid, self._lbl_no_credits,
                    self._lbl_invalid, self._lbl_rate, self._lbl_errors,
                    self._lbl_changed, self._lbl_dupes):
            lbl.pack(side="left", padx=6)

        # ── Progress ───────────────────────────────────────────────────
        self._progress = ttk.Progressbar(root, mode="determinate")
        self._progress.pack(fill="x", padx=10, pady=(4, 0))

        self._lbl_status = ttk.Label(root, text="Ready.", anchor="w")
        self._lbl_status.pack(fill="x", padx=12)

        # ── Log ────────────────────────────────────────────────────────
        log_frame = ttk.LabelFrame(root, text="Results", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        self._log = tk.Text(log_frame, height=14, state="disabled",
                            wrap="none", font=("Consolas", 9))
        log_sb_y = ttk.Scrollbar(log_frame, command=self._log.yview)
        log_sb_x = ttk.Scrollbar(log_frame, orient="horizontal", command=self._log.xview)
        self._log.configure(yscrollcommand=log_sb_y.set, xscrollcommand=log_sb_x.set)
        log_sb_y.pack(side="right",  fill="y")
        log_sb_x.pack(side="bottom", fill="x")
        self._log.pack(fill="both", expand=True)

        self._log.tag_configure("valid",        foreground="#1a8f1a")
        self._log.tag_configure("valid_models", foreground="#2da82d", font=("Consolas", 8))
        self._log.tag_configure("roo_hint",     foreground="#0099aa", font=("Consolas", 8, "bold"))
        self._log.tag_configure("no_credits",   foreground="#e07b00", font=("Consolas", 9, "bold"))
        self._log.tag_configure("no_cred_info", foreground="#c07000", font=("Consolas", 8))
        self._log.tag_configure("invalid",      foreground="#cc2222")
        self._log.tag_configure("rate",         foreground="#cc7700")
        self._log.tag_configure("error",        foreground="#888888")
        self._log.tag_configure("info",         foreground="#1a55cc")
        self._log.tag_configure("warn_up",      foreground="#009900", font=("Consolas", 9, "bold"))
        self._log.tag_configure("warn_down",    foreground="#cc0000", font=("Consolas", 9, "bold"))
        self._log.tag_configure("warn_other",   foreground="#8800cc")
        self._log.tag_configure("dupe",         foreground="#999999", font=("Consolas", 8))
        self._log.tag_configure("probe_ok",    foreground="#007700", font=("Consolas", 8))
        self._log.tag_configure("probe_no",    foreground="#aaaaaa", font=("Consolas", 8))
        self._log.tag_configure("probe_limit", foreground="#cc7700", font=("Consolas", 8))
        self._log.tag_configure("probe_feat",  foreground="#005599", font=("Consolas", 8))

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _log_line(self, msg: str, tag: str = "") -> None:
        self._log.configure(state="normal")
        self._log.insert("end", msg + "\n", tag)
        self._log.see("end")
        self._log.configure(state="disabled")

    def _refresh_stats(self) -> None:
        self._lbl_valid.configure(     text=f"Valid: {self._valid}")
        self._lbl_no_credits.configure(text=f"No-Credits: {self._no_credits}")
        self._lbl_invalid.configure(   text=f"Invalid: {self._invalid}")
        self._lbl_rate.configure(      text=f"Rate-lim: {self._rate}")
        self._lbl_errors.configure(    text=f"Errors: {self._errors}")
        self._lbl_changed.configure(   text=f"Changed: {self._changed}")
        self._lbl_dupes.configure(     text=f"Dupes skipped: {self._dupes}")

    # ------------------------------------------------------------------
    # File / directory loading
    # ------------------------------------------------------------------

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Select key file",
            filetypes=[("Text files", "*.txt"), ("Env files", "*.env"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "r", errors="ignore") as fh:
                data = fh.read()
            self._input.delete("1.0", "end")
            self._input.insert("1.0", data)
        except OSError as exc:
            messagebox.showerror("Load error", str(exc))

    def _load_dir(self) -> None:
        path = filedialog.askdirectory(title="Select directory to scan for keys")
        if not path:
            return
        keys = from_directory(path)
        if not keys:
            messagebox.showinfo("Nothing found", "No API keys detected in that directory.")
            return
        self._input.delete("1.0", "end")
        self._input.insert("1.0", "\n".join(keys))
        self._log_line(f"Loaded {len(keys)} unique keys from directory: {path}", "info")

    def _clear(self) -> None:
        if self._running:
            return
        self._input.delete("1.0", "end")
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._progress["value"] = 0
        self._lbl_status.configure(text="Ready.")
        self._valid = self._invalid = self._rate = self._errors = 0
        self._no_credits = 0
        self._changed = self._dupes = 0
        self._results.clear()
        self._refresh_stats()
        self._lbl_found.configure(text="Keys: 0")

    # ------------------------------------------------------------------
    # Re-check shortcuts
    # ------------------------------------------------------------------

    def _recheck_valids(self) -> None:
        """Load results/valid/keys.txt into the input and start a fresh scan."""
        self._load_result_file_and_scan(VALID_DIR / "keys.txt", "valid")

    def _recheck_no_credits(self) -> None:
        """Load results/no_credits/keys.txt — re-scan keys that may now have billing."""
        self._load_result_file_and_scan(NO_CREDITS_DIR / "keys.txt", "no-credits")

    def _recheck_rate_limited(self) -> None:
        """Load results/rate_limited/keys.txt into the input and start a fresh scan."""
        self._load_result_file_and_scan(RATE_DIR / "keys.txt", "rate-limited")

    def _load_result_file_and_scan(self, path, label: str) -> None:
        if self._running:
            messagebox.showwarning("Busy", "A scan is already running.")
            return
        if not path.exists() or path.stat().st_size == 0:
            messagebox.showinfo("Empty", f"No {label} keys saved yet.")
            return
        keys_raw = path.read_text(encoding="utf-8", errors="ignore")
        self._input.delete("1.0", "end")
        self._input.insert("1.0", keys_raw)
        self._log_line(f"── Re-checking {label} keys from {path} ──", "info")
        self._start()

    # ------------------------------------------------------------------
    # Scan control
    # ------------------------------------------------------------------

    def _start(self) -> None:
        if self._running:
            return

        raw  = self._input.get("1.0", "end")
        keys, dup_count = extract_keys_with_stats(raw)

        if not keys:
            messagebox.showerror("No keys", "No valid OpenAI API keys found in the input.")
            return

        setup_dirs()

        # Load history from disk so we can detect status changes
        self._known = load_known_statuses()

        self._valid = self._invalid = self._rate = self._errors = 0
        self._no_credits = 0
        self._changed = self._dupes = 0
        self._results.clear()
        self._refresh_stats()
        self._lbl_found.configure(text=f"Keys: {len(keys)}")
        self._progress.configure(maximum=len(keys), value=0)
        self._lbl_status.configure(text=f"Scanning {len(keys)} keys…")

        self._stop.clear()
        self._running = True
        self._btn_start.configure(       state="disabled")
        self._btn_stop.configure(        state="normal")
        self._btn_recheck.configure(     state="disabled")
        self._btn_recheck_nc.configure(  state="disabled")
        self._btn_recheck_rate.configure(state="disabled")

        deep = self._deep_var.get()
        dup_note  = f"  ({dup_count} duplicates removed from input)" if dup_count else ""
        deep_note = "  [Deep Probe ON]" if deep else ""
        self._log_line(
            f"── Starting scan of {len(keys)} unique keys{dup_note}{deep_note} ──", "info"
        )
        threading.Thread(target=self._worker, args=(keys, deep), daemon=True).start()

    def _stop_scan(self) -> None:
        self._stop.set()
        self._btn_stop.configure(state="disabled")
        self._log_line("Stop requested — finishing in-flight requests…", "info")

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _worker(self, keys: list, deep: bool = False) -> None:
        def callback(done: int, total: int, result: dict) -> None:
            self._results.append(result)
            self._q.put({"type": "result", "data": result, "done": done, "total": total})

        scan(keys, callback=callback, stop=self._stop, deep=deep)

        ts       = timestamp()
        csv_path = REPORT_DIR / f"report_{ts}.csv"
        write_csv_report(self._results, csv_path)

        json_path = None
        if deep:
            json_path = REPORT_DIR / f"report_{ts}.json"
            write_json_report(self._results, json_path)

        self._q.put({
            "type":      "done",
            "report":    str(csv_path),
            "json_path": str(json_path) if json_path else None,
        })

    # ------------------------------------------------------------------
    # Queue polling (main thread only)
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            while True:
                msg = self._q.get_nowait()
                self._handle_msg(msg)
        except queue.Empty:
            pass
        self.root.after(_POLL_MS, self._poll)

    def _handle_msg(self, msg: dict) -> None:
        if msg["type"] == "result":
            self._on_result(msg["data"], msg["done"], msg["total"])
        elif msg["type"] == "done":
            self._on_done(msg["report"], msg.get("json_path"))

    # ------------------------------------------------------------------
    # Per-result logic: dedup + status-change detection
    # ------------------------------------------------------------------

    def _on_result(self, r: dict, done: int, total: int) -> None:
        status  = r["status"]
        key     = r["key"]
        masked  = mask(key)
        latency = r.get("latency_ms", "?")
        norm    = normalize_status(status)        # VALID / INVALID / RATE_LIMITED / ERROR

        self._progress["value"] = done
        self._lbl_status.configure(text=f"Checked {done} / {total}")

        # ── Smart persistence ─────────────────────────────────────────
        prev = self._known.get(key)               # None = never seen before

        if prev is None:
            # Brand new key — just save it
            append_key(result_path(status), key)
            self._known[key] = norm

        elif prev == norm:
            # Same status as last run → skip write entirely (prevents duplicates)
            self._dupes += 1

        else:
            # Status changed between runs → migrate key between files
            old_path = result_path(prev)
            new_path = result_path(status)
            remove_key(old_path, key)
            append_key(new_path, key)
            self._known[key] = norm
            self._changed += 1
            self._log_status_change(masked, prev, norm)

        # ── Counters and log ──────────────────────────────────────────
        if status == "VALID":
            self._valid += 1
            tier      = r.get("tier", "?")
            rpm       = r.get("rpm",  "-")
            tpm       = r.get("tpm",  "-")
            models    = r.get("models") or []
            chat_ms   = r.get("chat_latency_ms", "?")
            rec_model = r.get("recommended_model", "-")
            dupe_note = "  (still valid, no duplicate written)" if prev == norm else ""
            self._log_line(
                f"[VALID/{tier}]  {masked}  RPM:{rpm}  TPM:{tpm}"
                f"  chat:{chat_ms}ms{dupe_note}",
                "valid",
            )
            self._log_line(
                f"               ▶ Use in Roo Code / completions: {rec_model}",
                "roo_hint",
            )
            self._log_line(
                f"               Models: {_model_summary(models)}",
                "valid_models",
            )
            # ── Deep Probe block (only when probe ran) ────────────────
            probe = r.get("probe")
            if probe:
                self._log_probe(probe)

        elif status == "NO_CREDITS":
            self._no_credits += 1
            tier      = r.get("tier", "?")
            models    = r.get("models") or []
            rec_model = r.get("recommended_model", "-")
            dupe_note = "  (no change)" if prev == norm else ""
            self._log_line(
                f"[NO_CREDITS/{tier}]  {masked}  key real but quota exhausted — add billing{dupe_note}",
                "no_credits",
            )
            self._log_line(
                f"               Would use: {rec_model}  |  Models: {_model_summary(models)}",
                "no_cred_info",
            )

        elif status == "INVALID":
            self._invalid += 1
            suffix = "  (still invalid)" if prev == norm else ""
            self._log_line(f"[INVALID]      {masked}{suffix}", "invalid")

        elif status == "RATE_LIMITED":
            self._rate += 1
            suffix = "  (still rate-limited)" if prev == norm else ""
            self._log_line(f"[RATE_LIMITED] {masked}{suffix}", "rate")

        else:
            self._errors += 1
            self._log_line(f"[{status:<14}] {masked}", "error")

        self._refresh_stats()

    def _log_status_change(self, masked: str, old: str, new: str) -> None:
        """Log a prominent status-change notice."""
        arrow = f"{old} → {new}"
        if old == "INVALID" and new == "VALID":
            self._log_line(
                f"  ✅ RECOVERED  {masked}  {arrow}  (moved valid ← invalid)",
                "warn_up",
            )
        elif old == "VALID" and new == "INVALID":
            self._log_line(
                f"  ❌ REVOKED    {masked}  {arrow}  (moved invalid ← valid)",
                "warn_down",
            )
        elif old == "RATE_LIMITED" and new == "VALID":
            self._log_line(
                f"  ✅ UNBLOCKED  {masked}  {arrow}  (moved valid ← rate_limited)",
                "warn_up",
            )
        elif old == "VALID" and new == "RATE_LIMITED":
            self._log_line(
                f"  ⚠️  THROTTLED  {masked}  {arrow}  (moved rate_limited ← valid)",
                "warn_other",
            )
        else:
            self._log_line(
                f"  ℹ️  STATUS CHANGE  {masked}  {arrow}",
                "warn_other",
            )

    # ------------------------------------------------------------------
    # Deep Probe display helper
    # ------------------------------------------------------------------

    def _log_probe(self, probe: dict) -> None:
        """Render the deep-probe sub-dict into the results log."""
        pad = "               "

        self._log_line(f"{pad}── Deep Probe ─────────────────────────────────", "probe_feat")

        # Model access grid
        ma = probe.get("model_access") or {}
        if ma:
            _sym   = {"ACCESSIBLE": "✓", "PERMISSION_DENIED": "✗",
                      "NOT_AVAILABLE": "–", "RATE_LIMITED": "⏱", "NO_CREDITS": "∅"}
            parts  = []
            for model, st in ma.items():
                short = (model.replace("gpt-3.5-turbo", "3.5")
                              .replace("gpt-4o-mini",   "4o-mini")
                              .replace("gpt-4o",        "4o")
                              .replace("gpt-4-turbo",   "4-turbo")
                              .replace("gpt-4",         "4")
                              .replace("o1-mini",       "o1-mini")
                              .replace("o3-mini",       "o3-mini"))
                sym = _sym.get(st, "?")
                parts.append(f"{short} {sym}")
                # Pick tag based on status
            accessible  = [m for m, s in ma.items() if s == "ACCESSIBLE"]
            denied      = [m for m, s in ma.items() if s in ("PERMISSION_DENIED", "NOT_AVAILABLE")]
            rate_lim    = [m for m, s in ma.items() if s == "RATE_LIMITED"]

            self._log_line(f"{pad}Model Access: {'  '.join(parts)}", "probe_ok")

        # Rate limits
        rl = probe.get("rate_limits") or {}
        if rl and rl.get("rpm_limit", "-") != "-":
            rpm_lim  = rl.get("rpm_limit",      "-")
            rpm_rem  = rl.get("rpm_remaining",   "-")
            rpm_rst  = rl.get("rpm_reset",       "-")
            tpm_lim  = rl.get("tpm_limit",       "-")
            tpm_rem  = rl.get("tpm_remaining",   "-")
            tpm_rst  = rl.get("tpm_reset",       "-")
            self._log_line(
                f"{pad}Rate Limits:  RPM {rpm_rem}/{rpm_lim} (reset {rpm_rst})"
                f"  TPM {tpm_rem}/{tpm_lim} (reset {tpm_rst})",
                "probe_feat",
            )

        # Permissions
        perm = probe.get("permissions") or {}
        if perm:
            def _yesno(v) -> str:
                if v is True:  return "✓"
                if v is False: return "✗"
                return "–"
            feat_line = (
                f"embeddings {_yesno(perm.get('embeddings'))}  "
                f"moderation {_yesno(perm.get('moderation'))}  "
                f"fine_tuning {_yesno(perm.get('fine_tuning'))}  "
                f"dall-e {_yesno(perm.get('dall_e'))}  "
                f"whisper {_yesno(perm.get('whisper'))}  "
                f"tts {_yesno(perm.get('tts'))}"
            )
            self._log_line(f"{pad}Permissions:  {feat_line}", "probe_feat")

            org  = perm.get("org_id",     "-")
            proj = perm.get("project_id", "-")
            if org != "-" or proj != "-":
                self._log_line(f"{pad}Org: {org}  Project: {proj}", "probe_feat")

        ms = probe.get("probe_ms")
        if ms is not None:
            self._log_line(f"{pad}Probe time: {ms} ms", "probe_feat")

    # ------------------------------------------------------------------
    # Scan finished
    # ------------------------------------------------------------------

    def _on_done(self, report_path: str, json_path: str | None = None) -> None:
        # Sort + dedup all result files on disk
        sort_result_files()

        self._running = False
        self._btn_start.configure(       state="normal")
        self._btn_stop.configure(        state="disabled")
        self._btn_recheck.configure(     state="normal")
        self._btn_recheck_nc.configure(  state="normal")
        self._btn_recheck_rate.configure(state="normal")

        stopped = " (stopped early)" if self._stop.is_set() else ""
        self._log_line(f"── Scan complete{stopped} ──", "info")
        self._log_line(
            f"Valid:{self._valid}  No-Credits:{self._no_credits}  "
            f"Invalid:{self._invalid}  Rate-lim:{self._rate}  "
            f"Errors:{self._errors}  Changed:{self._changed}  "
            f"Dupes skipped:{self._dupes}",
            "info",
        )
        self._log_line("Result files sorted & deduplicated on disk.", "info")
        self._log_line(f"CSV report → {report_path}", "info")
        if json_path:
            self._log_line(f"JSON report → {json_path}", "info")
        self._lbl_status.configure(text=f"Done{stopped}.")

    # ------------------------------------------------------------------
    # Manual save
    # ------------------------------------------------------------------

    def _save_report(self) -> None:
        if not self._results:
            messagebox.showinfo("Nothing to save", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save CSV report",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=f"report_{timestamp()}.csv",
        )
        if not path:
            return
        from pathlib import Path as _P
        write_csv_report(self._results, _P(path))
        messagebox.showinfo("Saved", f"Report saved to:\n{path}")

    def _save_json(self) -> None:
        if not self._results:
            messagebox.showinfo("Nothing to save", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save JSON report",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile=f"report_{timestamp()}.json",
        )
        if not path:
            return
        from pathlib import Path as _P
        write_json_report(self._results, _P(path))
        messagebox.showinfo("Saved", f"JSON report saved to:\n{path}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def launch() -> None:
    root = tk.Tk()
    _App(root)
    root.mainloop()


if __name__ == "__main__":
    launch()
