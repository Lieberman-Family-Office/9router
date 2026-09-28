#!/usr/bin/env python3
"""Hot-patch live 9router 7843.js for text.verbosity (OpenAI deployment checklist)."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

CHUNK = Path("/opt/homebrew/lib/node_modules/9router/app/.next-cli-build/server/chunks/7843.js")


def backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = path.with_suffix(path.suffix + f".bak-text-verbosity-{stamp}")
    shutil.copy2(path, bak)
    return bak


def build_already_has_verbosity(chunk_dir: Path) -> Path | None:
    """lfenergy.10 compiles text.verbosity into a chunk, not 7843.js."""
    for path in sorted(chunk_dir.glob("*.js")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "a.verbosity.toLowerCase()" in text and "b.verbosity=a.verbosity.toLowerCase()" in text:
            return path
    return None


def main() -> None:
    baked = build_already_has_verbosity(CHUNK.parent)
    if baked is not None:
        print(f"OK already in this build: {baked.name}")
        return
    if not CHUNK.is_file():
        raise SystemExit(f"missing {CHUNK}")
    t = CHUNK.read_text()
    notes = []

    if "tc.verbosity=tv[1]" in t and "preservedVerb" in t:
        print("7843 already patched for text.verbosity")
        return

    bak = backup(CHUNK)
    notes.append(f"backup {bak.name}")

    # --- parseSuffix locals ---
    old_locals = (
        "parts=b[2].split(/[,+]/).map(p=>p.trim().toLowerCase()).filter(Boolean),"
        "rc={},orch={},thinkingRaw=null;"
    )
    new_locals = (
        "parts=b[2].split(/[,+]/).map(p=>p.trim().toLowerCase()).filter(Boolean),"
        "rc={},orch={},tc={},thinkingRaw=null;"
    )
    if old_locals not in t:
        raise SystemExit("parseSuffix locals not found")
    t = t.replace(old_locals, new_locals, 1)
    notes.append("locals tc")

    # --- verbosity tokens after summary_detailed ---
    old_sum = 'if("summary_detailed"===part){rc.summary="detailed";continue}'
    new_sum = (
        old_sum
        + 'let tv=part.match(/^verbosity_(low|medium|high)$/)||part.match(/^verb_(low|medium|high)$/);'
        + "if(tv){tc.verbosity=tv[1];continue}"
    )
    if old_sum not in t:
        raise SystemExit("summary_detailed marker not found")
    t = t.replace(old_sum, new_sum, 1)
    notes.append("verbosity tokens")

    # --- has flags + unknown sole ---
    old_has = "let has=Object.keys(rc).length>0,hasO=Object.keys(orch).length>0;if(null!==thinkingRaw&&null===override&&!has&&!hasO)"
    new_has = (
        "let has=Object.keys(rc).length>0,hasO=Object.keys(orch).length>0,hasT=Object.keys(tc).length>0;"
        "if(null!==thinkingRaw&&null===override&&!has&&!hasO&&!hasT)"
    )
    if old_has not in t:
        raise SystemExit("has/unknown marker not found")
    t = t.replace(old_has, new_has, 1)
    notes.append("hasT unknown-check")

    # --- returns ---
    t = t.replace(
        "reasoningConfig:null,reasoningMode:null,orchestration:null}",
        "reasoningConfig:null,reasoningMode:null,orchestration:null,textConfig:null}",
    )
    old_ret = "reasoningMode:rc.mode||null,orchestration:hasO?orch:null}"
    new_ret = "reasoningMode:rc.mode||null,orchestration:hasO?orch:null,textConfig:hasT?tc:null}"
    if old_ret not in t:
        raise SystemExit("parseSuffix return not found")
    t = t.replace(old_ret, new_ret, 1)
    notes.append("return textConfig")

    # --- applyThinking destructure + preserve ---
    old_head = (
        "let{cleanModel:p,override:x,reasoningConfig:rc,orchestration:orch}=j(b),"
        "preserved={},priorMA=c.multi_agent,priorCM=c.context_management;"
    )
    new_head = (
        "let{cleanModel:p,override:x,reasoningConfig:rc,orchestration:orch,textConfig:tcCfg}=j(b),"
        "preserved={},preservedVerb=null,priorMA=c.multi_agent,priorCM=c.context_management;"
        'if(c.text&&"object"==typeof c.text&&!Array.isArray(c.text)'
        '&&("low"===c.text.verbosity||"medium"===c.text.verbosity||"high"===c.text.verbosity))'
        "preservedVerb=c.text.verbosity;"
        'else if("low"===c.verbosity||"medium"===c.verbosity||"high"===c.verbosity)preservedVerb=c.verbosity;'
    )
    if old_head not in t:
        raise SystemExit("applyThinking head not found")
    t = t.replace(old_head, new_head, 1)
    notes.append("head preserve verbosity")

    # Non-reasoning early return should still apply verbosity
    old_early = "if(!z.reasoning)return s(c),c;"
    new_early = (
        "if(!z.reasoning){s(c);"
        "(function(){let v=(tcCfg&&tcCfg.verbosity)||preservedVerb;"
        'if("low"===v||"medium"===v||"high"===v){'
        'c.text={...(c.text&&"object"==typeof c.text&&!Array.isArray(c.text)?c.text:{}),verbosity:v},c.verbosity=v'
        "}})();return c}"
    )
    if old_early not in t:
        raise SystemExit("early return not found")
    t = t.replace(old_early, new_early, 1)
    notes.append("early-return verbosity")

    # Apply before multi-agent IIFE continuation (after reasoning merge IIFE starts)
    apply_snip = (
        "(function(){let v=(tcCfg&&tcCfg.verbosity)||preservedVerb;"
        'if("low"===v||"medium"===v||"high"===v){'
        'c.text={...(c.text&&"object"==typeof c.text&&!Array.isArray(c.text)?c.text:{}),verbosity:v},'
        "c.verbosity=v"
        "}})(),"
    )
    marker = "(function(){let m={...preserved,...(rc||{})};"
    if marker not in t:
        raise SystemExit("reasoning apply IIFE not found")
    if "tcCfg&&tcCfg.verbosity)||preservedVerb" not in t.split(marker, 1)[0][-200:]:
        # insert immediately before reasoning IIFE (runs first is fine either order)
        t = t.replace(marker, apply_snip + marker, 1)
        notes.append("apply text.verbosity")
    else:
        notes.append("apply already adjacent")

    CHUNK.write_text(t)
    for n in notes:
        print(n)
    print("DONE")


if __name__ == "__main__":
    main()
