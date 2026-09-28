#!/usr/bin/env python3
"""Idempotent live hotfix: preserve async/strict/defer_loading in BOTH tool rebuild sites.

Site 1: older/alternate rebuild (grok-like / prior live variant).
Site 2: CodexExecutor.normalizeCodexTools from source (e.slice / d?.parameters).

Source-of-truth: branch fix/codex-async-tool-calling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys

LOG_DIR = Path.home() / ".9router" / "logs"
LOG_FILE = LOG_DIR / "async-tool-calling-patch.log"

SITE1_MARKER = "ASYNC_TOOL_CALLING_PRESERVE_V1"
SITE2_MARKER = "ASYNC_TOOL_CALLING_PRESERVE_CODEX_V1"

SITE1_OLD = (
    'for(let b of Object.keys(a))delete a[b];'
    'return a.type="function",a.name=f.slice(0,128),g&&(a.description=g),a.parameters=h,b.add(a.name),!0'
)
SITE1_NEW = (
    'let asyncFlag="boolean"==typeof a.async?a.async:"boolean"==typeof e?.async?e.async:void 0,'
    'strictFlag="boolean"==typeof a.strict?a.strict:"boolean"==typeof e?.strict?e.strict:void 0,'
    'deferLoading="boolean"==typeof a.defer_loading?a.defer_loading:"boolean"==typeof e?.defer_loading?e.defer_loading:void 0;'
    'for(let b of Object.keys(a))delete a[b];'
    'return a.type="function",a.name=f.slice(0,128),g&&(a.description=g),a.parameters=h,'
    'void 0!==asyncFlag&&(a.async=asyncFlag),void 0!==strictFlag&&(a.strict=strictFlag),'
    'void 0!==deferLoading&&(a.defer_loading=deferLoading),b.add(a.name),!0'
    f'/*{SITE1_MARKER}*/'
)

SITE2_OLD = (
    'for(let b of Object.keys(a))delete a[b];'
    'return a.type="function",a.name=e.slice(0,128),f&&(a.description=f),a.parameters=g,b.add(e),!0'
)
SITE2_NEW = (
    'let asyncFlag="boolean"==typeof a.async?a.async:"boolean"==typeof d?.async?d.async:void 0,'
    'strictFlag="boolean"==typeof a.strict?a.strict:"boolean"==typeof d?.strict?d.strict:void 0,'
    'deferLoading="boolean"==typeof a.defer_loading?a.defer_loading:"boolean"==typeof d?.defer_loading?d.defer_loading:void 0;'
    'for(let b of Object.keys(a))delete a[b];'
    'return a.type="function",a.name=e.slice(0,128),f&&(a.description=f),a.parameters=g,'
    'void 0!==asyncFlag&&(a.async=asyncFlag),void 0!==strictFlag&&(a.strict=strictFlag),'
    'void 0!==deferLoading&&(a.defer_loading=deferLoading),b.add(e),!0'
    f'/*{SITE2_MARKER}*/'
)


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def resolve_chunk() -> Path | None:
    candidates = []
    try:
        root = subprocess.check_output(["npm", "root", "-g"], text=True).strip()
        if root:
            candidates.append(Path(root) / "9router" / "app" / ".next-cli-build" / "server" / "chunks" / "318.js")
    except Exception:
        pass
    candidates.append(Path("/opt/homebrew/lib/node_modules/9router/app/.next-cli-build/server/chunks/318.js"))
    for path in candidates:
        if path.is_file():
            return path
    return None


def apply_one(text: str, old: str, new: str, marker: str, label: str) -> tuple[str, bool]:
    if marker in text:
        log(f"OK {label} already patched")
        return text, False
    if old not in text:
        # Already transformed variant of site1 (preserve without marker leftover) is fine to skip
        if label == "site1" and "a.async=asyncFlag" in text and "f.slice(0,128)" in text:
            log(f"OK {label} preserve present (no marker needed)")
            return text, False
        # Current webpack build keeps async with minified locals (a.async=h), not asyncFlag.
        if label == "site2" and "e.slice(0,128)" in text and "a.async=h" in text and "a.defer_loading=j" in text:
            log(f"OK {label} already compiled")
            return text, False
        log(f"FAIL {label} snippet missing")
        return text, False
    return text.replace(old, new, 1), True


def main() -> int:
    chunk = resolve_chunk()
    if chunk is None:
        log("SKIP: 318.js chunk not found")
        return 0
    text = chunk.read_text(encoding="utf-8")
    text, c1 = apply_one(text, SITE1_OLD, SITE1_NEW, SITE1_MARKER, "site1")
    text2, c2 = apply_one(text, SITE2_OLD, SITE2_NEW, SITE2_MARKER, "site2")
    text = text2
    if not c1 and not c2:
        if (
            SITE2_MARKER in text
            or ("e.slice(0,128)" in text and "a.async=asyncFlag" in text)
            or ("e.slice(0,128)" in text and "a.async=h" in text and "a.defer_loading=j" in text)
        ):
            log(f"OK no changes needed: {chunk}")
            return 0
        log(f"FAIL could not patch Codex site in {chunk}")
        return 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = chunk.with_suffix(chunk.suffix + f".bak-async-tools-{stamp}")
    shutil.copy2(chunk, bak)
    chunk.write_text(text, encoding="utf-8")
    log(f"OK patched {chunk} site1={c1} site2={c2}")
    log(f"OK backup {bak}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
