#!/usr/bin/env python3
"""Hot-patch live 9router chunks for Responses reasoning config (docs-aligned)."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

CHUNK_DIR_CANDIDATES = [
    Path("/opt/homebrew/lib/node_modules/9router/app/.next-cli-build/server/chunks"),
]

NEW_PARSE = (
    "function j(a){"
    'if("string"!=typeof a)return{cleanModel:a,override:null,reasoningConfig:null,reasoningMode:null};'
    "let b=a.match(/^(.*)\\(([^()]+)\\)\\s*$/);"
    "if(!b)return{cleanModel:a,override:null,reasoningConfig:null,reasoningMode:null};"
    "let c=b[1].trim(),parts=b[2].split(/[,+]/).map(p=>p.trim().toLowerCase()).filter(Boolean),rc={},thinkingRaw=null;"
    "for(let part of parts){"
    'if("pro"===part||"standard"===part){rc.mode=part;continue}'
    'if("current_turn"===part||"all_turns"===part){rc.context=part;continue}'
    'if("ctx_auto"===part||"context_auto"===part){rc.context="auto";continue}'
    'if("summary_auto"===part){rc.summary="auto";continue}'
    'if("summary_concise"===part){rc.summary="concise";continue}'
    'if("summary_detailed"===part){rc.summary="detailed";continue}'
    "if(null===thinkingRaw)thinkingRaw=part"
    "}"
    "let override=null;"
    "if(null!==thinkingRaw){"
    'if("none"===thinkingRaw||"off"===thinkingRaw)override={mode:"none"};'
    'else if("auto"===thinkingRaw)override={mode:"auto"};'
    "else if(/^\\d+$/.test(thinkingRaw))override={mode:\"budget\",budget:Number(thinkingRaw)};"
    'else if(void 0!==g.Dl[thinkingRaw])override={mode:"level",level:thinkingRaw}'
    "}"
    "let has=Object.keys(rc).length>0;"
    "if(null!==thinkingRaw&&null===override&&!has)"
    "return{cleanModel:c,override:null,reasoningConfig:null,reasoningMode:null};"
    "return{cleanModel:c,override:override,reasoningConfig:has?rc:null,reasoningMode:rc.mode||null}"
    "}"
)

OLD_PARSE_START = 'function j(a){if("string"!=typeof a)return{cleanModel:a,override:null};'


def find_chunk_dir() -> Path:
    for p in CHUNK_DIR_CANDIDATES:
        if p.is_dir():
            return p
    raise SystemExit("chunk dir not found")


def backup(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = path.with_suffix(path.suffix + f".bak-reasoning-config-{stamp}")
    shutil.copy2(path, bak)
    return bak


def patch_7843(path: Path) -> list[str]:
    t = path.read_text()
    notes = []
    if "reasoningConfig:null,reasoningMode:null" in t and "summary_auto" in t and 'levels:[...k,"ultra"]' not in t:
        notes.append("7843 already patched")
        return notes

    bak = backup(path)
    notes.append(f"backup {bak.name}")

    # Replace parseSuffix function j — find old body ending at function k
    start = t.find(OLD_PARSE_START)
    if start < 0:
        # maybe already new form
        start = t.find("function j(a){if(\"string\"!=typeof a)return{cleanModel:a,override:null,reasoningConfig:null")
        if start >= 0:
            notes.append("parseSuffix already new")
        else:
            raise SystemExit("parseSuffix not found in 7843")
    else:
        end = t.find("function k(a){", start)
        if end < 0:
            raise SystemExit("parseSuffix end (function k) not found")
        t = t[:start] + NEW_PARSE + t[end:]
        notes.append("replaced parseSuffix")

    # Remove ultra from openai normalize
    old_oa = (
        'a&&(b.reasoning_effort="max"!==a&&"ultra"!==a||e?.includes(a)?a:'
        '"ultra"===a&&e?.includes("max")?"max":"xhigh")'
    )
    new_oa = 'a&&(b.reasoning_effort="max"!==a||e?.includes(a)?a:"xhigh")'
    if old_oa in t:
        t = t.replace(old_oa, new_oa, 1)
        notes.append("openai level no ultra")
    elif new_oa in t:
        notes.append("openai level already clean")

    old_claude = '"ultra"===f?"max":"minimal"===f?"low"'
    new_claude = '"minimal"===f?"low"'
    if old_claude in t:
        t = t.replace(old_claude, new_claude, 1)
        notes.append("claude no ultra")

    # levels
    t2 = t.replace('levels:[...k,"ultra"]', "levels:k")
    if t2 != t:
        notes.append("removed ultra from gpt-5.6 levels")
        t = t2

    # applyThinking: capture + restore reasoning config
    old_head = 'function t(a,b,c,i=null,l){if(!c||"object"!=typeof c)return c;let{cleanModel:p,override:x}=j(b),y=x||l||k(c),'
    new_head = (
        'function t(a,b,c,i=null,l){if(!c||"object"!=typeof c)return c;'
        "let{cleanModel:p,override:x,reasoningConfig:rc}=j(b),preserved={};"
        'if(c.reasoning&&"object"==typeof c.reasoning&&!Array.isArray(c.reasoning)){'
        "let m=c.reasoning.mode,cx=c.reasoning.context,sm=c.reasoning.summary;"
        '("pro"===m||"standard"===m)&&(preserved.mode=m);'
        '("auto"===cx||"current_turn"===cx||"all_turns"===cx)&&(preserved.context=cx);'
        '("auto"===sm||"concise"===sm||"detailed"===sm)&&(preserved.summary=sm)'
        "}"
        "let y=x||l||k(c),"
    )
    if old_head in t:
        t = t.replace(old_head, new_head, 1)
        notes.append("applyThinking head")
    elif "reasoningConfig:rc" in t:
        notes.append("applyThinking head already")
    else:
        raise SystemExit("applyThinking head not found")

    old_tail = (
        'return"claude"===i&&c.thinking?.type==="adaptive"&&(c.thinking.display=A||"summarized"),'
        '"claude"===i&&v.has(c.output_config?.effort)&&(c.max_tokens=Math.max(c.max_tokens||0,u)),c}'
    )
    new_tail = (
        'return"claude"===i&&c.thinking?.type==="adaptive"&&(c.thinking.display=A||"summarized"),'
        '"claude"===i&&v.has(c.output_config?.effort)&&(c.max_tokens=Math.max(c.max_tokens||0,u)),'
        "(function(){let m={...preserved,...(rc||{})};"
        "if(Object.keys(m).length){"
        'c.reasoning={...(c.reasoning&&"object"==typeof c.reasoning&&!Array.isArray(c.reasoning)?c.reasoning:{}),...m}'
        "}})(),c}"
    )
    if old_tail in t:
        t = t.replace(old_tail, new_tail, 1)
        notes.append("applyThinking tail")
    elif "preserved,...(rc||{})" in t:
        notes.append("applyThinking tail already")
    else:
        raise SystemExit("applyThinking tail not found")

    path.write_text(t)
    return notes


def patch_318(path: Path) -> list[str]:
    t = path.read_text()
    notes = []
    old = (
        'if(b.model=g,b.reasoning)b.reasoning.effort=w(b.model,b.reasoning.effort),b.reasoning.summary||(b.reasoning.summary="auto");'
        'else{let a=w(b.model,b.reasoning_effort||i||"low");b.reasoning={effort:a,summary:"auto"}}'
    )
    new = (
        "if(b.reasoning){let a=b.reasoning,c=w(b.model,a.effort||b.reasoning_effort||i||\"low\");"
        "b.reasoning={effort:c};"
        # ChatGPT Codex OAuth rejects mode=pro; keep standard only.
        '("standard"===a.mode)&&(b.reasoning.mode=a.mode);'
        '("auto"===a.context||"current_turn"===a.context||"all_turns"===a.context)&&(b.reasoning.context=a.context);'
        '("auto"===a.summary||"concise"===a.summary||"detailed"===a.summary)&&(b.reasoning.summary=a.summary)'
        '}else{let a=w(b.model,b.reasoning_effort||i||"low");b.reasoning={effort:a}}'
    )
    # Upgrade older pro+standard patch → strip-pro
    old_pro_mode = '("pro"===a.mode||"standard"===a.mode)&&(b.reasoning.mode=a.mode)'
    new_std_only = '("standard"===a.mode)&&(b.reasoning.mode=a.mode)'
    if old_pro_mode in t:
        bak = backup(path)
        notes.append(f"backup {bak.name}")
        t = t.replace(old_pro_mode, new_std_only, 1)
        path.write_text(t)
        notes.append("318 strip mode=pro (Codex OAuth)")
        return notes
    if new_std_only in t and "b.reasoning.context=a.context" in t:
        notes.append("318 already strips pro")
        return notes
    if "b.reasoning.context=a.context" in t and 'summary:"auto"' not in t[t.find("prompt_cache_key") : t.find("prompt_cache_key") + 800]:
        # fragile check — just look for our marker
        if '("pro"===a.mode||"standard"===a.mode)&&(b.reasoning.mode=a.mode)' in t:
            notes.append("318 already patched")
            return notes
    if old not in t:
        if new[:40] in t:
            notes.append("318 already patched")
            return notes
        raise SystemExit("318 reasoning block not found")
    bak = backup(path)
    notes.append(f"backup {bak.name}")
    t = t.replace(old, new, 1)
    # include: push rather than overwrite if possible
    old_inc = 'b.include=["reasoning.encrypted_content"]'
    new_inc = (
        'b.include=(Array.isArray(b.include)?b.include.slice():[]),'
        'b.include.includes("reasoning.encrypted_content")||b.include.push("reasoning.encrypted_content")'
    )
    # careful: the original is inside a for-of comma mess. Keep simple replace of assignment.
    if old_inc in t:
        t = t.replace(old_inc, 'b.include=[...(Array.isArray(b.include)?b.include:[]),"reasoning.encrypted_content"].filter((x,i,a)=>a.indexOf(x)===i)', 1)
        notes.append("318 include merge")
    path.write_text(t)
    notes.append("318 reasoning object")
    return notes


def patch_8895(path: Path) -> list[str]:
    t = path.read_text()
    notes = []
    old = (
        'if((0,g.z)(at,aC,a,ao),a.reasoning_effort){let b=ah.reasoning;'
        "ah.reasoning={...b&&\"object\"==typeof b&&!Array.isArray(b)?b:{},effort:a.reasoning_effort},"
        "delete ah.reasoning_effort}"
    )
    new = (
        "if((0,g.z)(at,aC,a,ao),a.reasoning_effort||a.reasoning&&(a.reasoning.mode||a.reasoning.context||a.reasoning.summary)){"
        "let b=ah.reasoning,c={...b&&\"object\"==typeof b&&!Array.isArray(b)?b:{}};"
        "a.reasoning_effort&&(c.effort=a.reasoning_effort);"
        "a.reasoning&&a.reasoning.mode&&(c.mode=a.reasoning.mode);"
        "a.reasoning&&a.reasoning.context&&(c.context=a.reasoning.context);"
        "a.reasoning&&a.reasoning.summary&&(c.summary=a.reasoning.summary);"
        "ah.reasoning=c,delete ah.reasoning_effort}"
    )
    if "a.reasoning.context&&(c.context=a.reasoning.context)" in t:
        notes.append("8895 already patched")
        return notes
    if old not in t:
        raise SystemExit("8895 passthrough block not found")
    bak = backup(path)
    notes.append(f"backup {bak.name}")
    path.write_text(t.replace(old, new, 1))
    notes.append("8895 passthrough reasoning fields")
    return notes


def build_already_has_reasoning(chunk_dir: Path) -> Path | None:
    """lfenergy.10 compiles parseReasoningConfigToken into a chunk, not 7843.js."""
    for path in sorted(chunk_dir.glob("*.js")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if (
            '"summary_auto"===b' in text
            and '"summary_concise"===b' in text
            and '"summary_detailed"===b' in text
        ):
            return path
    return None


def main() -> None:
    d = find_chunk_dir()
    baked = build_already_has_reasoning(d)
    if baked is not None:
        print(f"OK already in this build: {baked.name}")
        return
    all_notes = []
    all_notes += patch_7843(d / "7843.js")
    all_notes += patch_318(d / "318.js")
    all_notes += patch_8895(d / "8895.js")
    for n in all_notes:
        print(n)
    print("DONE")


if __name__ == "__main__":
    main()
