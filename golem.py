#!/usr/bin/env python3
"""copper-golem — an AI file sorter modeled on Minecraft's Copper Golem.

Files dropped directly into a watched root are read by Claude (`claude -p`) and
moved into the sibling category subfolder whose contents they most resemble.
If nothing matches well enough, the file is left in place (golem-faithful).

The classifier never touches the filesystem: this script extracts each file's
text, embeds it in a prompt, and uses `claude -p` purely as a text classifier.
File moves are done here, with a dry-run default, a JSONL move log, and undo.
"""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict = {
    "watch_roots": ["~/Downloads"],
    "model": "claude-haiku-4-5",     # classification is cheap/fast — Haiku is enough
    "dry_run": True,                 # start safe; flip off once you trust it
    "on_no_match": "keep",           # "keep" (leave in place) or a folder name to move into
    "confidence_threshold": 0.6,     # below this, treat as no-match (curbs misfiling)
    "max_chars": 4000,               # cap on extracted text per file
    "stability_seconds": 20,         # skip files modified within the last N seconds (downloads in flight)
    "exclude_globs": ["*.crdownload", "*.part", "*.download", "*.tmp", ".*"],
    "enable_ocr": False,             # OCR images with tesseract (slow); off by default
    "claude_timeout": 120,
    "claude_bin": "",                # path to the `claude` CLI; empty = auto-detect (PATH, mise/npm, homebrew, ...)
    "notify": True,                  # show a desktop notification after each live run
    "notify_sound": True,            # play a sound + ignore Do-Not-Disturb (so it's hard to miss)
    "interval_seconds": 0,           # also sweep every N seconds (0 = only when files change)
}

DEFAULT_CONFIG_PATH = Path("~/.config/copper-golem/config.toml").expanduser()

STATE_DIR = Path("~/.local/state/copper-golem").expanduser()
MOVES_LOG = STATE_DIR / "moves.jsonl"
UNDONE_LOG = STATE_DIR / "undone.txt"
LOCK_FILE = STATE_DIR / "golem.lock"


def load_config(explicit: str | None) -> dict:
    """Merge a TOML config over the defaults.

    An explicit path that doesn't exist is an error (so a typo'd --config never
    silently falls back to sorting ~/Downloads). Without --config, try
    $COPPER_GOLEM_CONFIG then ~/.config/copper-golem/config.toml; if neither
    exists, run on defaults.
    """
    cfg = dict(DEFAULT_CONFIG)
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {explicit}")
        with open(p, "rb") as f:
            cfg.update(tomllib.load(f))
        return cfg

    env = os.environ.get("COPPER_GOLEM_CONFIG", "").strip()
    candidates = ([Path(env).expanduser()] if env else []) + [DEFAULT_CONFIG_PATH]
    for p in candidates:
        if p.is_file():
            with open(p, "rb") as f:
                cfg.update(tomllib.load(f))
            break
    return cfg


# --------------------------------------------------------------------------- #
# Content extraction (extension-dispatched, degrades gracefully)
# --------------------------------------------------------------------------- #

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".html", ".htm", ".xml", ".srt",
    ".py", ".js", ".ts", ".sh", ".rb", ".go", ".rs", ".java", ".c", ".h",
    ".cpp", ".css", ".sql",
}
OFFICE_EXTS = {".doc", ".docx", ".rtf", ".odt", ".rtfd", ".wordml"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tiff", ".bmp"}
SPREADSHEET_EXTS = {".xlsx", ".xlsm"}


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _run_capture(argv: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _xlsx_text(path: Path, max_chars: int) -> str:
    """Pull sheet names + cell strings from an .xlsx/.xlsm (zip + XML, stdlib only).

    Reads are bounded so a workbook with a huge sharedStrings table can't blow
    up memory — we only need a representative sample for classification.
    """
    cap = max(max_chars, 0) * 8 + 4096  # bytes; generous vs. the char cap
    out: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "xl/workbook.xml" in names:
                with z.open("xl/workbook.xml") as fp:
                    wb = fp.read(cap).decode("utf-8", "replace")
                out += [f"sheet: {n}" for n in re.findall(r'<sheet[^>]*name="([^"]+)"', wb)]
            if "xl/sharedStrings.xml" in names:
                with z.open("xl/sharedStrings.xml") as fp:
                    ss = fp.read(cap).decode("utf-8", "replace")
                out += re.findall(r"<t[^>]*>(.*?)</t>", ss, re.S)
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""
    return "\n".join(out)[:max_chars]


def extract_text(path: Path, cfg: dict) -> str:
    """Return up to max_chars of representative text for `path`.

    The filename is always included — even when the body is empty it is often
    the strongest classification signal.
    """
    ext = path.suffix.lower()
    body = ""
    try:
        if ext in TEXT_EXTS:
            # Bounded read: never pull a multi-GB .log/.csv fully into memory.
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read(max(cfg["max_chars"], 0))
        elif ext == ".pdf" and _have("pdftotext"):
            body = _run_capture(["pdftotext", "-l", "5", "-nopgbrk", str(path), "-"])
        elif ext in OFFICE_EXTS and _have("textutil"):
            body = _run_capture(["textutil", "-convert", "txt", "-stdout", str(path)])
        elif ext in SPREADSHEET_EXTS:
            body = _xlsx_text(path, cfg["max_chars"])
        elif ext in IMAGE_EXTS:
            meta = _run_capture(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)]) if _have("sips") else ""
            ocr = ""
            if cfg.get("enable_ocr") and _have("tesseract"):
                ocr = _run_capture(["tesseract", str(path), "stdout"], timeout=20)
            body = (meta + "\n" + ocr).strip()
    except OSError:
        body = ""

    header = f"filename: {path.name}\nextension: {ext or '(none)'}\n"
    text = header + ("\ncontent:\n" + body if body.strip() else "\n(no extractable text content)")
    return text[: cfg["max_chars"]]


# --------------------------------------------------------------------------- #
# Candidate folders & profiles
# --------------------------------------------------------------------------- #

def candidate_folders(root: Path, cfg: dict) -> list[Path]:
    on_no_match = cfg.get("on_no_match", "keep")
    skip = {on_no_match} if on_no_match != "keep" else set()
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(".") or child.name in skip:
            continue
        out.append(child)
    return out


def folder_profile(folder: Path) -> str:
    """A short description of what a category folder holds."""
    profile = folder / ".golem.md"
    if profile.is_file():
        try:
            return profile.read_text(encoding="utf-8", errors="replace").strip()[:500]
        except OSError:
            pass
    names = []
    try:
        for child in sorted(folder.iterdir()):
            if child.name.startswith("."):
                continue
            names.append(child.name)
            if len(names) >= 10:
                break
    except OSError:
        pass
    return "example contents: " + (", ".join(names) if names else "(empty)")


# --------------------------------------------------------------------------- #
# Classification via `claude -p`
# --------------------------------------------------------------------------- #

PROMPT_TEMPLATE = """You are a file classifier. Read the file below and choose the single \
candidate folder whose contents it most closely matches. Judge by meaning/content, \
not just the filename.

# File
{file_block}

# Candidate folders
{folders_block}

# Output
Return ONLY this JSON object, nothing else:
{{"folder": "<exact folder name or null>", "confidence": <0.0-1.0>, "reason": "<short>"}}
If the file does not clearly belong in any candidate folder, set "folder" to null.
"""


def _parse_classification(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j != -1:
        t = t[i : j + 1]
    return json.loads(t)


def resolve_claude(cfg: dict) -> str | None:
    """Locate the `claude` binary. launchd/cron don't inherit your shell PATH,
    and `claude` is often a shell function — so check config, then PATH, then
    known install locations (mise/npm, homebrew, ~/.local, cmux)."""
    explicit = cfg.get("claude_bin", "")
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            return str(p)
    found = shutil.which("claude")
    if found:
        return found
    cands: list[Path] = sorted(
        Path("~/.local/share/mise/installs/node").expanduser().glob("*/bin/claude"),
        reverse=True,
    )
    cands += [Path(p).expanduser() for p in (
        "~/.mise/shims/claude", "~/.local/bin/claude",
        "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
        "/Applications/cmux.app/Contents/Resources/bin/claude",
    )]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def classify(file_block: str, folders: list[Path], cfg: dict) -> dict:
    claude = resolve_claude(cfg)
    if claude is None:
        return {"folder": None, "confidence": 0.0, "error": True,
                "reason": "claude binary not found — set claude_bin in config.toml"}
    folders_block = "\n".join(
        f"- {f.name}: {folder_profile(f)}" for f in folders
    )
    prompt = PROMPT_TEMPLATE.format(file_block=file_block, folders_block=folders_block)
    argv = [
        claude, "-p",
        "--model", cfg["model"],
        "--output-format", "json",
        "--allowedTools", "",
    ]
    # launchd/cron run with a minimal PATH; make sure the dir holding `claude`
    # (and its sibling `node`) is reachable so the CLI can actually launch.
    env = os.environ.copy()
    env["PATH"] = str(Path(claude).parent) + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run(
            argv, input=prompt, capture_output=True, text=True,
            timeout=cfg["claude_timeout"], env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"folder": None, "confidence": 0.0, "error": True, "reason": f"claude error: {e}"}

    if r.returncode != 0:
        return {"folder": None, "confidence": 0.0, "error": True, "reason": f"claude exit {r.returncode}: {r.stderr[:200]}"}

    # `--output-format json` wraps the model reply; the text is under "result".
    inner = r.stdout
    try:
        outer = json.loads(r.stdout)
        if isinstance(outer, dict):
            inner = outer.get("result", r.stdout)
    except json.JSONDecodeError:
        pass
    try:
        parsed = _parse_classification(inner)
    except (json.JSONDecodeError, ValueError):
        return {"folder": None, "confidence": 0.0, "error": True, "reason": "unparseable classifier output"}
    if not isinstance(parsed, dict):
        return {"folder": None, "confidence": 0.0, "error": True, "reason": "classifier output was not a JSON object"}
    return parsed


# --------------------------------------------------------------------------- #
# Safety: exclusion, stability, collision-safe move, move log
# --------------------------------------------------------------------------- #

def is_excluded(path: Path, cfg: dict) -> bool:
    return any(fnmatch.fnmatch(path.name, g) for g in cfg["exclude_globs"])


def is_stable(path: Path, cfg: dict) -> bool:
    try:
        age = datetime.now().timestamp() - path.stat().st_mtime
    except OSError:
        return False
    return age >= cfg["stability_seconds"]


def unique_dest(dest: Path) -> Path:
    """Never overwrite: append ' (2)', ' (3)', ... before the suffix."""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 2
    while True:
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
        n += 1


def append_move(batch: str, src: Path, dst: Path, decision: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {
        "batch": batch,
        "ts": datetime.now(timezone.utc).isoformat(),
        "src": str(src),
        "dst": str(dst),
        "file": src.name,
        "confidence": decision.get("confidence"),
        "reason": decision.get("reason"),
    }
    with open(MOVES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# Core flow
# --------------------------------------------------------------------------- #

def _coerce_conf(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def process_root(root: Path, cfg: dict, batch: str) -> tuple[int, int, int, int]:
    """Scan one root and sort its files. Returns (considered, moved, kept, errors).

    Each considered file lands in exactly one of moved / kept / errors. A file
    that matches a category is `moved`; a no-match file is `kept` (left in place,
    or relocated to the `on_no_match` folder — still counted as kept, and still
    recorded so `undo` can restore it); failures are `errors`.
    """
    if not root.is_dir():
        print(f"[skip] not a directory: {root}", file=sys.stderr)
        return (0, 0, 0, 0)

    folders = candidate_folders(root, cfg)
    if not folders:
        print(f"[skip] no candidate folders under {root}")
        return (0, 0, 0, 0)

    considered = moved = kept = errors = 0
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or is_excluded(entry, cfg):
            continue
        if not is_stable(entry, cfg):
            print(f"[wait] {entry.name}  (modified <{cfg['stability_seconds']}s ago — will retry next run)")
            continue

        considered += 1
        file_block = extract_text(entry, cfg)
        decision = classify(file_block, folders, cfg)

        # A real failure (claude missing/timeout/bad output) is NOT a "no match".
        if decision.get("error"):
            errors += 1
            print(f"[error] {entry.name}  — {decision.get('reason', 'unknown error')}")
            continue

        folder_name = decision.get("folder")
        conf = _coerce_conf(decision.get("confidence"))
        match = next((f for f in folders if f.name == folder_name), None) if folder_name else None

        if match is not None and conf >= cfg["confidence_threshold"]:
            dest_dir, is_match, reason = match, True, ""
        else:
            if folder_name and match is None:
                reason = f"unknown folder '{folder_name}'"
            else:
                reason = f"no match · conf={conf:.2f} · {decision.get('reason', '')}"
            target = _no_match_target(root, cfg)
            if target is None:
                kept += 1
                print(f"[keep] {entry.name}  ({reason})")
                continue
            dest_dir, is_match = target, False

        dest = unique_dest(dest_dir / entry.name)

        if cfg["dry_run"]:
            if is_match:
                moved += 1
                print(f"[dry-run] {entry.name}  ->  {dest_dir.name}/  (conf={conf:.2f})")
            else:
                kept += 1
                print(f"[dry-run keep] {entry.name}  ->  {dest_dir.name}/  ({reason})")
            continue

        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(dest))
        except OSError as ex:
            errors += 1
            print(f"[error] {entry.name}  — move failed: {ex}")
            continue

        append_move(batch, entry, dest, decision)
        if is_match:
            moved += 1
            print(f"[move] {entry.name}  ->  {dest_dir.name}/  (conf={conf:.2f})")
        else:
            kept += 1
            print(f"[keep] {entry.name}  ->  {dest_dir.name}/  ({reason})")

    return (considered, moved, kept, errors)


def _no_match_target(root: Path, cfg: dict) -> Path | None:
    on_no_match = cfg.get("on_no_match", "keep")
    if on_no_match == "keep":
        return None
    return root / on_no_match


def _notify(cfg: dict, title: str, text: str) -> None:
    """Best-effort desktop notification, so results are visible without digging
    through the log file. Prefers terminal-notifier (reliable from launchd);
    falls back to osascript. Adds sound + ignore-DnD so it's hard to miss."""
    sound = cfg.get("notify_sound", True)
    tn = shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier",
                     "/usr/local/bin/terminal-notifier") if Path(p).exists()),
        None,
    )
    try:
        if tn:
            argv = [tn, "-title", title, "-message", text, "-ignoreDnD"]
            if sound:
                argv += ["-sound", "default"]
            subprocess.run(argv, capture_output=True, timeout=10)
            return
        osa = "/usr/bin/osascript"
        if Path(osa).exists():
            script = f"display notification {json.dumps(text)} with title {json.dumps(title)}"
            if sound:
                script += ' sound name "default"'
            subprocess.run([osa, "-e", script], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def cmd_once(cfg: dict, roots: list[Path]) -> int:
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")
    mode = "dry-run" if cfg["dry_run"] else "live"
    tc = tm = tk = te = 0
    for root in roots:
        c, m, k, e = process_root(root, cfg, batch)
        tc += c; tm += m; tk += k; te += e

    # Stay quiet on empty sweeps so periodic runs don't flood the log.
    if tc or te:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        verb = "would move" if cfg["dry_run"] else "moved"
        print(f"[{stamp} {mode}] considered {tc} · {verb} {tm} · kept(no match) {tk} · errors {te}")
        if te:
            print(f"  WARNING: {te} file(s) ERRORED (see [error] lines) — failures, not 'no match'.")

    if cfg.get("notify", True) and not cfg["dry_run"] and (tm or tk or te):
        parts = []
        if tm:
            parts.append(f"{tm}件を振り分け")
        if tk:
            parts.append(f"該当なし{tk}件")
        if te:
            parts.append(f"⚠️エラー{te}件")
        _notify(cfg, "copper-golem ⚠️" if te else "copper-golem 🟫", " / ".join(parts))
    return 0


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #

def _read_batches() -> list[str]:
    if not MOVES_LOG.is_file():
        return []
    seen = []
    for line in MOVES_LOG.read_text(encoding="utf-8").splitlines():
        try:
            b = json.loads(line)["batch"]
        except (json.JSONDecodeError, KeyError):
            continue
        if b not in seen:
            seen.append(b)
    return seen


def _undone() -> set[str]:
    if not UNDONE_LOG.is_file():
        return set()
    return set(UNDONE_LOG.read_text(encoding="utf-8").split())


def cmd_undo(cfg: dict) -> int:
    batches = [b for b in _read_batches() if b not in _undone()]
    if not batches:
        print("Nothing to undo.")
        return 0
    target = batches[-1]
    records = []
    if MOVES_LOG.is_file():
        for line in MOVES_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("batch") == target:
                records.append(rec)
    restored = 0
    for rec in reversed(records):
        dst, src = Path(rec["dst"]), Path(rec["src"])
        if not dst.exists():
            print(f"[skip] gone: {dst}")
            continue
        back = unique_dest(src)
        back.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(dst), str(back))
        print(f"[undo] {dst.name}  ->  {back}")
        restored += 1
    with open(UNDONE_LOG, "a", encoding="utf-8") as f:
        f.write(target + "\n")
    print(f"\nRestored {restored} file(s) from batch {target}.")
    return 0


# --------------------------------------------------------------------------- #
# launchd install / uninstall (immediate, WatchPaths-triggered)
# --------------------------------------------------------------------------- #

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
{args}
\t</array>
\t<key>WatchPaths</key>
\t<array>
{watch}
\t</array>
{interval}\t<key>StandardOutPath</key>
\t<string>{log}</string>
\t<key>StandardErrorPath</key>
\t<string>{log}</string>
\t<key>RunAtLoad</key>
\t<false/>
</dict>
</plist>
"""


def _label() -> str:
    return f"com.{getpass.getuser()}.copper-golem"


def _plist_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{_label()}.plist"


def cmd_install(cfg: dict, roots: list[str], config_path: str | None) -> int:
    label = _label()
    log = Path("~/Library/Logs/copper-golem.log").expanduser()
    log.parent.mkdir(parents=True, exist_ok=True)

    prog = [sys.executable, str(Path(__file__).resolve()), "once"]
    if config_path:
        prog += ["--config", str(Path(config_path).expanduser().resolve())]
    # Escape every interpolated string — paths/usernames may contain &, <, >.
    args_xml = "\n".join(f"\t\t<string>{_xml_escape(a)}</string>" for a in prog)
    watch_xml = "\n".join(f"\t\t<string>{_xml_escape(r)}</string>" for r in roots)
    try:
        interval = int(cfg.get("interval_seconds", 0) or 0)
    except (TypeError, ValueError):
        interval = 0
    interval_xml = (f"\t<key>StartInterval</key>\n\t<integer>{interval}</integer>\n"
                    if interval > 0 else "")

    plist = PLIST_TEMPLATE.format(label=_xml_escape(label), args=args_xml,
                                  watch=watch_xml, log=_xml_escape(str(log)),
                                  interval=interval_xml)
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)  # reload if present
    path.write_text(plist, encoding="utf-8")
    r = subprocess.run(["launchctl", "load", "-w", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl load failed: {r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"Installed and loaded: {path}")
    print(f"Watching: {', '.join(roots)}" + (f"  (+ sweep every {interval}s)" if interval > 0 else ""))
    print(f"Logs: {log}")
    if cfg.get("dry_run", True):
        print("\nNote: dry_run is ON — files won't move yet. Set dry_run=false in your config "
              "(no reinstall needed; the watcher re-reads the config on each run).")
    return 0


def cmd_uninstall(cfg: dict) -> int:
    path = _plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
        print(f"Unloaded and removed: {path}")
    else:
        print("Not installed (no plist found).")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

# Held for the whole process so the flock isn't released when _acquire_lock
# returns. A bare local would be GC'd, closing the fd and dropping the lock.
_LOCK_FH = None


def _acquire_lock():
    global _LOCK_FH
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _LOCK_FH = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another copper-golem run is in progress; exiting.", file=sys.stderr)
        sys.exit(0)
    return _LOCK_FH


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="golem", description="copper-golem: AI file sorter")
    p.add_argument("command", nargs="?", default="once",
                   choices=["once", "undo", "install", "uninstall"],
                   help="once: scan & sort (default); undo: revert the last batch; "
                        "install/uninstall: manage the launchd watcher")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--root", action="append", help="override watch root (repeatable)")
    p.add_argument("--dry-run", action="store_true", help="force dry-run (no moves)")
    p.add_argument("--apply", action="store_true", help="force live mode (perform moves)")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        cfg["dry_run"] = True
    if args.apply:
        cfg["dry_run"] = False

    if args.command == "undo":
        return cmd_undo(cfg)

    roots = (
        [Path(r).expanduser() for r in args.root]
        if args.root
        else [Path(r).expanduser() for r in cfg["watch_roots"]]
    )

    if args.command == "install":
        return cmd_install(cfg, [str(r) for r in roots], args.config)
    if args.command == "uninstall":
        return cmd_uninstall(cfg)

    _acquire_lock()
    return cmd_once(cfg, roots)


if __name__ == "__main__":
    raise SystemExit(main())
