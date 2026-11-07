#!/usr/bin/env python3
"""RenderWare CFont Printable ASCII Sanitizer & Verbatim Coverage Auditor.

Enforces Section 4 & Section 7 (Gates 2 & 3) of GTA_EAGLE_LIT_CONTRACT_V1.md:
- Converts UTF-8 literary prose into 100% safe Printable ASCII (32 <= ord(c) <= 126)
  for GTA San Andreas RenderWare CFont / DirectX 9 HUD rendering.
- Eliminates banned control characters (~, |, *, @), smart quotes, em/en-dashes,
  Unicode ellipses, scene-break ornaments, and Latin diacritics (via NFKD).
- Audits JSON manifests/chapters for zero CFont violations.
- Measures lexical word counts, verbatim_coverage_ratio, and auto_hold_ms timing.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata

BANNED_CFONT_CHARS: frozenset[str] = frozenset({"~", "|", "*", "@"})

# Identifiers, file paths, and URLs in manifest/chapter JSON that should not be
# altered by prose punctuation rules (URLs may legitimately contain '~' or '@').
STRUCTURAL_SKIP_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "work_id",
        "venue_type",
        "default_read_mode",
        "supported_read_modes",
        "source_category",
        "language",
        "series_id",
        "source_urls",
        "poster_2x3_rel",
        "banner_16x9_rel",
        "char_key",
        "file",
        "sha256",
        "kind",
        "parent_cg_id",
        "characters_present",
        "fallback_mood_bucket",
        "chapter_id",
        "scene_id",
        "default_bgm_id",
        "transition_in",
        "block_id",
        "cg_id",
        "bgm_id",
        "beat_id",
        "asset_id",
        "target_file",
        "reference_files",
        "status",
        "quota_policy",
        "updated_at",
        "completed_at",
        "harvested_at",
    }
)

_ZERO_WIDTH_RE = re.compile(r"[\u00ad\u200b\u200c\u200d\u2060\ufeff]")
_UNICODE_SPACES_RE = re.compile(
    r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]"
)
_INLINE_STAR_BREAK_RE = re.compile(r"(?:\*\s*){3,}")
_INLINE_KOME_BREAK_RE = re.compile(r"※+")
_MULTI_SPACE_RE = re.compile(r" {2,}")
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")

_SCENE_BREAK_CHARS = frozenset("*※◆◇•✧✦─━—–-=_~△▼▲▽")
_ORNAMENTAL_BREAK_CHARS = frozenset("*※◆◇✧✦─━△▼▲▽")

_DIRECT_CHAR_MAP: dict[str, str] = {
    # Em-dashes & horizontal bars -> "--"
    "\u2014": "--",  # —
    "\u2015": "--",  # ―
    "\u2e3a": "--",  # ⸺
    "\u2e3b": "--",  # ⸻
    "\u2500": "--",  # ─
    "\u2501": "--",  # ━
    # En-dashes & hyphens -> "-"
    "\u2013": "-",   # –
    "\u2212": "-",   # −
    "\u2012": "-",   # ‒
    "\u2010": "-",   # ‐
    "\u2011": "-",   # ‑
    # Ellipses -> "..."
    "\u2026": "...",  # …
    "\u22ef": "...",  # ⋯
    # Double smart quotes / CJK brackets -> '"'
    "\u201c": '"',   # “
    "\u201d": '"',   # ”
    "\u201e": '"',   # „
    "\u201f": '"',   # ‟
    "\u00ab": '"',   # «
    "\u00bb": '"',   # »
    "\u300c": '"',   # 「
    "\u300d": '"',   # 」
    "\u300e": '"',   # 『
    "\u300f": '"',   # 』
    "\uff02": '"',   # ＂
    # Single smart quotes / apostrophes -> "'"
    "\u2018": "'",   # ‘
    "\u2019": "'",   # ’
    "\u201a": "'",   # ‚
    "\u201b": "'",   # ‛
    "\u2032": "'",   # ′
    "\u00b4": "'",   # ´
    "\uff07": "'",   # ＇
    # Bullets & geometric markers -> "-"
    "\u2022": "-",   # •
    "\u25e6": "-",   # ◦
    "\u25aa": "-",   # ▪
    "\u25c6": "-",   # ◆
    "\u25c7": "-",   # ◇
    # Non-NFKD Latin letters & ligatures
    "æ": "ae",
    "Æ": "AE",
    "œ": "oe",
    "Œ": "OE",
    "ß": "ss",
    "ø": "o",
    "Ø": "O",
    "ł": "l",
    "Ł": "L",
    "đ": "d",
    "Đ": "D",
    "þ": "th",
    "Þ": "Th",
    "ð": "d",
    "Ð": "D",
    # Common typographic symbols
    "×": "x",
    "÷": "/",
    "©": "(c)",
    "®": "(r)",
    "™": "(TM)",
}

_TRANSLATION_TABLE = str.maketrans(_DIRECT_CHAR_MAP)


def is_scene_break_line(line: str) -> bool:
    """Return True if a standalone line represents a literary scene break."""
    stripped = line.strip()
    if not stripped:
        return False
    compact = re.sub(r"\s+", "", stripped)
    if not compact:
        return False
    if any(ch not in _SCENE_BREAK_CHARS for ch in compact):
        return False
    if any(ch in _ORNAMENTAL_BREAK_CHARS for ch in compact):
        return True
    return len(compact) >= 3


def _sanitize_single_line(line: str) -> str:
    """Sanitize a single line into RenderWare CFont safe Printable ASCII."""
    if is_scene_break_line(line):
        return "---"

    s = _ZERO_WIDTH_RE.sub("", line)
    s = _UNICODE_SPACES_RE.sub(" ", s)
    s = s.replace("\t", " ")

    # Convert inline scene-break markers before stripping emphasis asterisks.
    s = _INLINE_STAR_BREAK_RE.sub("---", s)
    s = _INLINE_KOME_BREAK_RE.sub("---", s)

    # Apply direct Unicode punctuation & Latin ligature replacements.
    s = s.translate(_TRANSLATION_TABLE)

    # Decompose accented characters (e.g. Romanée -> Romanee, ū -> u) via NFKD.
    decomposed = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    # Enforce RenderWare CFont / Lua HUD bridge banned character conversions.
    s = s.replace("~", "-")
    s = s.replace("|", "/")
    s = s.replace("*", "")
    s = re.sub(r"(?<=[A-Za-z0-9])@(?=[A-Za-z0-9])", "(at)", s)
    s = s.replace("@", "-")

    # Keep strictly Printable ASCII 32..126 and collapse whitespace runs.
    s = "".join(ch for ch in s if 32 <= ord(ch) <= 126)
    s = _MULTI_SPACE_RE.sub(" ", s).strip()
    return s


def sanitize_cfont_text(text: str | None, preserve_newlines: bool = False) -> str:
    """Convert UTF-8 text into 100% Printable ASCII (32..126) safe for CFont.

    Args:
        text: Input string (UTF-8).
        preserve_newlines: If False (default for JSON display fields), joins
            lines with single spaces so every character satisfies 32 <= ord <= 126.
            If True, sanitizes each line while preserving '\\n' line breaks.
    """
    if text is None:
        return ""
    normalized_newlines = str(text).replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = normalized_newlines.split("\n")

    if preserve_newlines:
        return "\n".join(_sanitize_single_line(line) for line in raw_lines)

    cleaned_parts = [
        cleaned for line in raw_lines if (cleaned := _sanitize_single_line(line))
    ]
    if not cleaned_parts:
        return ""
    joined = " ".join(cleaned_parts)
    return _MULTI_SPACE_RE.sub(" ", joined).strip()


def find_cfont_violations(text: str, field_path: str = "<text>") -> list[dict[str, Any]]:
    """Return all RenderWare CFont character violations in `text`."""
    violations: list[dict[str, Any]] = []
    for idx, ch in enumerate(text):
        code = ord(ch)
        reason: str | None = None
        if code < 32 or code > 126:
            reason = "OUTSIDE_PRINTABLE_ASCII_32_126"
        elif ch in BANNED_CFONT_CHARS:
            reason = f"BANNED_CFONT_CHAR_{repr(ch)}"

        if reason is not None:
            start = max(0, idx - 16)
            end = min(len(text), idx + 17)
            violations.append(
                {
                    "field": field_path,
                    "index": idx,
                    "char": ch,
                    "ord": code,
                    "codepoint": f"U+{code:04X}",
                    "unicode_name": unicodedata.name(ch, "CONTROL_OR_UNKNOWN"),
                    "reason": reason,
                    "context": text[start:end],
                }
            )
    return violations


def sanitize_json_cfont(data: Any, parent_key: str = "") -> Any:
    """Recursively sanitize all display string fields in a manifest/chapter JSON structure."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if k in STRUCTURAL_SKIP_KEYS:
                out[k] = v
            else:
                out[k] = sanitize_json_cfont(v, parent_key=k)
        return out
    if isinstance(data, list):
        if parent_key in STRUCTURAL_SKIP_KEYS:
            return data
        return [sanitize_json_cfont(item, parent_key=parent_key) for item in data]
    if isinstance(data, str):
        if parent_key in STRUCTURAL_SKIP_KEYS:
            return data
        return sanitize_cfont_text(data, preserve_newlines=False)
    return data


def check_json_cfont(
    data: Any,
    field_path: str = "root",
    parent_key: str = "",
) -> list[dict[str, Any]]:
    """Recursively inspect all display string fields in JSON data for CFont violations."""
    violations: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            child_path = f"{field_path}.{k}"
            if k in STRUCTURAL_SKIP_KEYS:
                continue
            violations.extend(check_json_cfont(v, field_path=child_path, parent_key=k))
    elif isinstance(data, list):
        if parent_key in STRUCTURAL_SKIP_KEYS:
            return violations
        for idx, item in enumerate(data):
            child_path = f"{field_path}[{idx}]"
            violations.extend(
                check_json_cfont(item, field_path=child_path, parent_key=parent_key)
            )
    elif isinstance(data, str):
        if parent_key not in STRUCTURAL_SKIP_KEYS:
            violations.extend(find_cfont_violations(data, field_path=field_path))
    return violations


def tokenize_words(text: str) -> list[str]:
    """Tokenize text into normalized ASCII lexical words for consistent word-count auditing."""
    if not text:
        return []
    sanitized = sanitize_cfont_text(text, preserve_newlines=False).lower()
    return _WORD_TOKEN_RE.findall(sanitized)


def count_words(text: str) -> int:
    """Count lexical words after CFont ASCII normalization."""
    return len(tokenize_words(text))


def compute_verbatim_coverage(raw_text: str, adapted_text: str) -> dict[str, Any]:
    """Compare adapted text against raw UTF-8 source text and return coverage metrics."""
    raw_tokens = tokenize_words(raw_text)
    adapted_tokens = tokenize_words(adapted_text)

    raw_count = len(raw_tokens)
    adapted_count = len(adapted_tokens)

    if raw_count == 0:
        return {
            "raw_word_count": 0,
            "adapted_word_count": adapted_count,
            "word_count_ratio": 1.0 if adapted_count == 0 else 0.0,
            "multiset_matched_words": 0,
            "verbatim_coverage_ratio": 1.0 if adapted_count == 0 else 0.0,
            "missing_word_count": 0,
            "missing_word_samples": [],
        }

    raw_counter = Counter(raw_tokens)
    adapted_counter = Counter(adapted_tokens)
    matched_words = sum((raw_counter & adapted_counter).values())
    missing_counter = raw_counter - adapted_counter
    missing_total = sum(missing_counter.values())
    missing_samples = [
        f"{word} (x{cnt})" if cnt > 1 else word
        for word, cnt in missing_counter.most_common(15)
    ]

    return {
        "raw_word_count": raw_count,
        "adapted_word_count": adapted_count,
        "word_count_ratio": round(adapted_count / raw_count, 6),
        "multiset_matched_words": matched_words,
        "verbatim_coverage_ratio": round(matched_words / raw_count, 6),
        "missing_word_count": missing_total,
        "missing_word_samples": missing_samples,
    }


def compute_auto_hold_ms(
    text: str,
    inner_thought: str = "",
    cg_changed: bool = False,
) -> int:
    """Compute Mode 1 Auto-Play hold duration in milliseconds per Contract Section 3.2:
    clamp(1800 + word_count(text + inner_thought) * 210 + (450 if cg_changed else 0), 2600, 9500)
    """
    combined = f"{text} {inner_thought}".strip()
    words = count_words(combined)
    hold_ms = 1800 + words * 210 + (450 if cg_changed else 0)
    return max(2600, min(9500, int(hold_ms)))


def _extract_adapted_texts_from_chapter_json(data: dict[str, Any]) -> tuple[str, str]:
    """Extract concatenated Mode 2 book_text and Mode 1 vn_beats text from a chapter JSON."""
    book_parts: list[str] = []
    vn_parts: list[str] = []
    for scene in data.get("scenes", []):
        for block in scene.get("blocks", []):
            if block.get("kind") == "scene_break":
                continue
            bt = block.get("book_text", "")
            if bt:
                book_parts.append(bt)
            for beat in block.get("vn_beats", []):
                t = beat.get("text", "")
                it = beat.get("inner_thought", "")
                if t:
                    vn_parts.append(t)
                if it:
                    vn_parts.append(it)
    return "\n".join(book_parts), "\n".join(vn_parts)


def _load_text_or_dir(path: Path) -> tuple[str, str | None]:
    """Load raw text or chapter JSON(s) from a file or directory.

    Returns (book_or_raw_text, vn_text_if_chapter_json_else_None).
    """
    if path.is_dir():
        files = sorted(
            p for p in path.iterdir() if p.is_file() and not p.name.startswith(".")
        )
        if not files:
            return "", None
        if all(p.suffix.lower() == ".json" for p in files):
            book_chunks: list[str] = []
            vn_chunks: list[str] = []
            for p in files:
                payload = json.loads(p.read_text(encoding="utf-8"))
                b_txt, v_txt = _extract_adapted_texts_from_chapter_json(payload)
                book_chunks.append(b_txt)
                vn_chunks.append(v_txt)
            return "\n".join(book_chunks), "\n".join(vn_chunks)
        return "\n".join(p.read_text(encoding="utf-8") for p in files), None

    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _extract_adapted_texts_from_chapter_json(payload)

    return path.read_text(encoding="utf-8"), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RenderWare CFont ASCII sanitizer, violation checker, and verbatim coverage auditor."
    )
    parser.add_argument(
        "target",
        nargs="?",
        help="Input file (text or .json) to sanitize or check.",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file path when sanitizing.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite target file in place with sanitized output.",
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Treat target as EagleLit manifest/chapter JSON structure.",
    )
    parser.add_argument(
        "--preserve-newlines",
        action="store_true",
        help="Preserve newline characters when sanitizing plain text files.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Audit target file for CFont ASCII violations (non-zero exit if violations found).",
    )
    parser.add_argument(
        "--raw",
        help="Raw UTF-8 source file or raw_chapters/ directory for verbatim coverage comparison.",
    )
    parser.add_argument(
        "--adapted",
        help="Adapted text/chapter JSON file or chapters/ directory for coverage comparison.",
    )

    args = parser.parse_args(argv)

    if args.raw and args.adapted:
        raw_path = Path(args.raw)
        adapted_path = Path(args.adapted)
        raw_text, _ = _load_text_or_dir(raw_path)
        adapted_book, adapted_vn = _load_text_or_dir(adapted_path)
        result: dict[str, Any] = {
            "raw_source": str(raw_path),
            "adapted_target": str(adapted_path),
            "book_mode_coverage": compute_verbatim_coverage(raw_text, adapted_book),
        }
        if adapted_vn is not None:
            result["vn_cinema_mode_coverage"] = compute_verbatim_coverage(
                raw_text, adapted_vn
            )
        print(json.dumps(result, indent=2))
        return 0

    if not args.target:
        parser.error("Provide a `target` file, or both `--raw` and `--adapted`.")

    target_path = Path(args.target)
    if not target_path.is_file():
        print(f"ERROR: File not found: {target_path}", file=sys.stderr)
        return 2

    is_json = args.as_json or target_path.suffix.lower() == ".json"

    if args.check:
        if is_json:
            payload = json.loads(target_path.read_text(encoding="utf-8"))
            violations = check_json_cfont(payload, field_path=target_path.name)
        else:
            raw = target_path.read_text(encoding="utf-8")
            violations = []
            for line_no, line in enumerate(raw.splitlines(), start=1):
                violations.extend(
                    find_cfont_violations(line, field_path=f"{target_path.name}:L{line_no}")
                )

        report = {
            "file": str(target_path),
            "status": "PASS" if not violations else "FAIL",
            "violation_count": len(violations),
            "violations": violations[:50],
        }
        print(json.dumps(report, indent=2))
        return 0 if not violations else 1

    # Sanitize mode
    if is_json:
        payload = json.loads(target_path.read_text(encoding="utf-8"))
        sanitized_payload = sanitize_json_cfont(payload)
        rendered = json.dumps(sanitized_payload, indent=2, ensure_ascii=True) + "\n"
    else:
        raw = target_path.read_text(encoding="utf-8")
        rendered = sanitize_cfont_text(
            raw, preserve_newlines=args.preserve_newlines
        )
        if args.preserve_newlines and not rendered.endswith("\n"):
            rendered += "\n"

    if args.in_place:
        target_path.write_text(rendered, encoding="utf-8")
    elif args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
