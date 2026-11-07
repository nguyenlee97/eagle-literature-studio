#!/usr/bin/env python3
"""7-Gate Zero-Error Quality Validator for EagleLit Pack v1.0.

Enforces Section 7 of bootstrap/GTA_EAGLE_LIT_CONTRACT_V1.md across:
  Gate 1: Schema & Structural Integrity
  Gate 2: Dual-Mode Sync & Verbatim Coverage Audit
  Gate 3: Strict RenderWare CFont Printable ASCII Check (32..126, zero ~ | * @)
  Gate 4: VN Beat Length (<= 280 chars) & Auto-Hold Timing ([2000, 10000] ms)
  Gate 5: CG Format, Dimensions, Baseline RGB JPEG & SHA-256 Uniqueness
  Gate 6: BGM Win32 MCI Integrity (nb_streams==1, stripped ID3v2, >=55s, vol 185..240)
  Gate 7: Standalone Dual-Mode preview.html Readiness (CORS-free file:///)

Writes full structured audit results to <pack_dir>/validation_report.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from PIL import Image

TOOLS_DIR = Path(__file__).resolve().parent
STUDIO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from audio_mci_normalizer import (  # noqa: E402
    ASCII_MP3_NAME_RE,
    MAX_LEAD_SILENCE_SEC,
    MIN_DURATION_SEC,
    VALID_MOOD_BUCKETS,
    AudioNormalizationError,
    detect_lead_silence_sec,
    probe_audio_file,
)
from cfont_sanitizer import (  # noqa: E402
    check_json_cfont,
    compute_verbatim_coverage,
    count_words,
)

VALID_VENUE_TYPES = frozenset({"cinema", "library", "both"})
VALID_READ_MODES = frozenset({"adv_cinema", "nvl_book"})
VALID_TRANSITIONS = frozenset(
    {"cut", "fade_black", "fade_white", "flash_crimson", "dissolve"}
)
VALID_BLOCK_KINDS = frozenset(
    {"narration", "dialogue", "internal", "scene_break", "epigraph"}
)
VALID_CG_KINDS = frozenset({"base", "diff"})
SLUG_RE = re.compile(r"^[a-z0-9_]+$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

POSTER_SIZE = (832, 1248)
CG_16X9_SIZE = (1376, 768)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_rgb_triplet(val: Any) -> bool:
    return (
        isinstance(val, list)
        and len(val) == 3
        and all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 255 for x in val)
    )


def _inspect_jpeg(path: Path, expected_size: tuple[int, int], label: str) -> list[str]:
    """Validate physical image is an exact-dimension RGB baseline JPEG."""
    errors: list[str] = []
    try:
        with Image.open(path) as img:
            if img.format != "JPEG":
                errors.append(
                    f"{label} ({path.name}): format is '{img.format}', expected 'JPEG'."
                )
            if img.mode != "RGB":
                errors.append(
                    f"{label} ({path.name}): color mode is '{img.mode}', expected 'RGB' (no alpha/CMYK/grayscale)."
                )
            if img.size != expected_size:
                errors.append(
                    f"{label} ({path.name}): dimensions {img.size[0]}x{img.size[1]} != required {expected_size[0]}x{expected_size[1]}."
                )
            if img.info.get("progression") or img.info.get("progressive"):
                errors.append(
                    f"{label} ({path.name}): progressive JPEG detected; must be baseline JPEG (progressive=False) for RenderWare/D3D9."
                )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{label} ({path.name}): unreadable image file ({exc}).")
    return errors


def _load_cg_gen_status_map(cg_manifest_path: Path) -> tuple[dict[str, str], list[str]]:
    """Return ({asset_id_or_rel_file: status}, errors)."""
    errors: list[str] = []
    status_map: dict[str, str] = {}
    if not cg_manifest_path.is_file():
        errors.append("Missing cg_generation_manifest.json in package root.")
        return status_map, errors

    try:
        payload = json.loads(cg_manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Invalid JSON in cg_generation_manifest.json: {exc}")
        return status_map, errors

    items = payload.get("items")
    if isinstance(items, list):
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"cg_generation_manifest.items[{idx}] must be an object.")
                continue
            aid = item.get("asset_id") or item.get("cg_id") or f"item_{idx}"
            tfile = item.get("target_file") or item.get("file") or ""
            st = str(item.get("status", "")).lower()
            if st not in {"done", "pending"}:
                errors.append(
                    f"cg_generation_manifest item '{aid}' has invalid status '{st}' (expected 'done' or 'pending')."
                )
            status_map[str(aid)] = st
            if tfile:
                status_map[str(tfile)] = st
    elif isinstance(payload.get("cgs"), dict):
        for aid, item in payload["cgs"].items():
            if isinstance(item, dict):
                st = str(item.get("status", "")).lower()
                status_map[str(aid)] = st
                if item.get("file"):
                    status_map[str(item["file"])] = st
    else:
        errors.append(
            "cg_generation_manifest.json must contain an 'items' array (or 'cgs' object)."
        )

    return status_map, errors


def validate_pack(
    pack_dir: Path,
    *,
    sources_dir: Path | None = None,
    allow_pending_cgs: bool = False,
) -> dict[str, Any]:
    """Run all 7 quality gates on `pack_dir` and write `validation_report.json`."""
    pack_dir = pack_dir.resolve()

    gates: dict[str, dict[str, Any]] = {
        "gate_1_schema_structure": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
        "gate_2_dual_mode_verbatim": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
        "gate_3_cfont_ascii": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
        "gate_4_beat_constraints": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
        "gate_5_cg_integrity": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
        "gate_6_bgm_integrity": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
        "gate_7_preview_html": {"status": "PASS", "errors": [], "warnings": [], "metrics": {}},
    }

    g1_err = gates["gate_1_schema_structure"]["errors"]
    g1_warn = gates["gate_1_schema_structure"]["warnings"]
    g2_err = gates["gate_2_dual_mode_verbatim"]["errors"]
    g2_warn = gates["gate_2_dual_mode_verbatim"]["warnings"]
    g3_err = gates["gate_3_cfont_ascii"]["errors"]
    g4_err = gates["gate_4_beat_constraints"]["errors"]
    g4_warn = gates["gate_4_beat_constraints"]["warnings"]
    g5_err = gates["gate_5_cg_integrity"]["errors"]
    g5_warn = gates["gate_5_cg_integrity"]["warnings"]
    g6_err = gates["gate_6_bgm_integrity"]["errors"]
    g7_err = gates["gate_7_preview_html"]["errors"]

    manifest_path = pack_dir / "manifest.json"
    manifest: dict[str, Any] = {}
    if not manifest_path.is_file():
        g1_err.append(f"Missing manifest.json at {manifest_path}")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                g1_err.append("manifest.json root must be a JSON object.")
                manifest = {}
        except Exception as exc:  # noqa: BLE001
            g1_err.append(f"Failed to parse manifest.json: {exc}")

    # Load CG generation manifest early so Gate 1 & Gate 5 know pending cover/CG status.
    cg_gen_path = pack_dir / "cg_generation_manifest.json"
    cg_status_map, cg_manifest_errs = _load_cg_gen_status_map(cg_gen_path)
    g5_err.extend(cg_manifest_errs)

    loaded_chapters: list[tuple[dict[str, Any], dict[str, Any], Path]] = []

    if manifest:
        if manifest.get("schema_version") != "eagle_lit_v1":
            g1_err.append(
                f"manifest.schema_version must be 'eagle_lit_v1', got {manifest.get('schema_version')!r}."
            )

        work_id = manifest.get("work_id", "")
        if not isinstance(work_id, str) or not SLUG_RE.match(work_id):
            g1_err.append(f"manifest.work_id must match ^[a-z0-9_]+$, got {work_id!r}.")
        elif pack_dir.name != work_id:
            g1_warn.append(
                f"Package directory name '{pack_dir.name}' differs from manifest.work_id '{work_id}'."
            )

        venue_type = manifest.get("venue_type")
        if venue_type not in VALID_VENUE_TYPES:
            g1_err.append(
                f"manifest.venue_type must be one of {sorted(VALID_VENUE_TYPES)}, got {venue_type!r}."
            )

        default_mode = manifest.get("default_read_mode")
        if default_mode not in VALID_READ_MODES:
            g1_err.append(
                f"manifest.default_read_mode must be one of {sorted(VALID_READ_MODES)}, got {default_mode!r}."
            )

        sup_modes = manifest.get("supported_read_modes")
        if not isinstance(sup_modes, list) or set(sup_modes) != VALID_READ_MODES:
            g1_err.append(
                "manifest.supported_read_modes must include both 'adv_cinema' and 'nvl_book'."
            )

        for str_key in (
            "title",
            "short_title",
            "subtitle",
            "franchise",
            "source_category",
            "author",
            "art_style_authority",
            "language",
            "age_rating",
            "synopsis",
        ):
            val = manifest.get(str_key)
            if not isinstance(val, str) or not val.strip():
                g1_err.append(f"manifest.{str_key} must be a non-empty string.")

        if not _is_rgb_triplet(manifest.get("theme_color_rgb")):
            g1_err.append("manifest.theme_color_rgb must be [R, G, B] integers in 0..255.")

        tags = manifest.get("tags")
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
            g1_err.append("manifest.tags must be a non-empty array of non-empty strings.")

        vol_info = manifest.get("volume_info")
        if not isinstance(vol_info, dict):
            g1_err.append("manifest.volume_info must be an object.")
        else:
            if not isinstance(vol_info.get("series_id"), str) or not vol_info["series_id"].strip():
                g1_err.append("manifest.volume_info.series_id must be a non-empty string.")
            if not isinstance(vol_info.get("volume_number"), int) or vol_info.get("volume_number", 0) < 1:
                g1_err.append("manifest.volume_info.volume_number must be an integer >= 1.")
            if not isinstance(vol_info.get("is_complete_work"), bool):
                g1_err.append("manifest.volume_info.is_complete_work must be a boolean.")

        econ = manifest.get("economy")
        if not isinstance(econ, dict):
            g1_err.append("manifest.economy must be an object.")
        else:
            for ek in (
                "ticket_price_usd",
                "rent_price_usd",
                "rent_days",
                "buy_price_usd",
                "estimated_runtime_min",
            ):
                ev = econ.get(ek)
                if not isinstance(ev, (int, float)) or isinstance(ev, bool) or ev < 0:
                    g1_err.append(f"manifest.economy.{ek} must be a non-negative number.")

        prov = manifest.get("source_provenance")
        if not isinstance(prov, dict):
            g1_err.append("manifest.source_provenance must be an object.")
        else:
            if not isinstance(prov.get("translation_credit"), str) or not prov["translation_credit"].strip():
                g1_err.append("manifest.source_provenance.translation_credit must be a non-empty string.")
            urls = prov.get("source_urls")
            if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.strip() for u in urls):
                g1_err.append("manifest.source_provenance.source_urls must be a non-empty array of URLs.")
            for nk in ("raw_word_count", "pack_book_word_count", "pack_vn_word_count"):
                if not isinstance(prov.get(nk), int) or prov.get(nk, 0) <= 0:
                    g1_err.append(f"manifest.source_provenance.{nk} must be a positive integer.")

        cover = manifest.get("cover_assets")
        if not isinstance(cover, dict):
            g1_err.append("manifest.cover_assets must be an object.")
        else:
            poster_rel = cover.get("poster_2x3_rel", "poster.jpg")
            banner_rel = cover.get("banner_16x9_rel", "banner_16x9.jpg")
            for asset_key, rel_path, req_size in (
                ("poster", poster_rel, POSTER_SIZE),
                ("banner_16x9", banner_rel, CG_16X9_SIZE),
            ):
                if not isinstance(rel_path, str) or not rel_path:
                    g1_err.append(f"manifest.cover_assets missing path for {asset_key}.")
                    continue
                abs_p = pack_dir / rel_path
                if not abs_p.is_file():
                    is_pending = (
                        cg_status_map.get(asset_key) == "pending"
                        or cg_status_map.get(rel_path) == "pending"
                    )
                    if allow_pending_cgs and is_pending:
                        g1_warn.append(f"Cover asset '{rel_path}' is pending quota recovery.")
                    else:
                        g1_err.append(f"Missing required cover asset '{rel_path}'.")
                else:
                    g5_err.extend(_inspect_jpeg(abs_p, req_size, f"Cover asset {asset_key}"))

        cast = manifest.get("cast")
        if not isinstance(cast, list) or not cast:
            g1_err.append("manifest.cast must be a non-empty array of character definitions.")
        else:
            seen_char_keys: set[str] = set()
            for idx, member in enumerate(cast):
                if not isinstance(member, dict):
                    g1_err.append(f"manifest.cast[{idx}] must be an object.")
                    continue
                ck = member.get("char_key", "")
                if not isinstance(ck, str) or not SLUG_RE.match(ck):
                    g1_err.append(f"manifest.cast[{idx}].char_key invalid: {ck!r}.")
                elif ck in seen_char_keys:
                    g1_err.append(f"Duplicate char_key '{ck}' in manifest.cast.")
                seen_char_keys.add(ck)
                for req_s in ("display_name", "full_name", "visual_anchor_notes"):
                    if not isinstance(member.get(req_s), str) or not member[req_s].strip():
                        g1_err.append(f"manifest.cast[{idx}].{req_s} must be a non-empty string.")
                if not _is_rgb_triplet(member.get("nameplate_color_rgb")):
                    g1_err.append(f"manifest.cast[{idx}].nameplate_color_rgb must be [R,G,B] in 0..255.")

        chapters_meta = manifest.get("chapters")
        if not isinstance(chapters_meta, list) or not chapters_meta:
            g1_err.append("manifest.chapters must be a non-empty array.")
        else:
            for expected_idx, ch_entry in enumerate(chapters_meta, start=1):
                if not isinstance(ch_entry, dict):
                    g1_err.append(f"manifest.chapters[{expected_idx - 1}] must be an object.")
                    continue
                if ch_entry.get("index") != expected_idx:
                    g1_err.append(
                        f"manifest.chapters[{expected_idx - 1}].index must be {expected_idx}, got {ch_entry.get('index')!r}."
                    )
                ch_rel = ch_entry.get("file", "")
                if not isinstance(ch_rel, str) or not ch_rel:
                    g1_err.append(f"manifest.chapters[{expected_idx - 1}].file missing.")
                    continue
                ch_path = pack_dir / ch_rel
                if not ch_path.is_file():
                    g1_err.append(f"Missing chapter file '{ch_rel}'.")
                    continue
                try:
                    ch_data = json.loads(ch_path.read_text(encoding="utf-8"))
                    if not isinstance(ch_data, dict):
                        g1_err.append(f"Chapter file '{ch_rel}' root must be an object.")
                        continue
                    loaded_chapters.append((ch_entry, ch_data, ch_path))
                except Exception as exc:  # noqa: BLE001
                    g1_err.append(f"Invalid JSON in chapter file '{ch_rel}': {exc}")

    # Gate 3: CFont Printable ASCII Check across manifest.json and chapters/*.json
    cfont_violations: list[dict[str, Any]] = []
    if manifest:
        cfont_violations.extend(check_json_cfont(manifest, field_path="manifest"))
    for _, ch_data, ch_path in loaded_chapters:
        cfont_violations.extend(check_json_cfont(ch_data, field_path=ch_path.stem))

    if cfont_violations:
        for v in cfont_violations[:40]:
            g3_err.append(
                f"{v['field']}[{v['index']}]: {v['reason']} char={v['char']!r} ({v['codepoint']}) in {v['context']!r}"
            )
        if len(cfont_violations) > 40:
            g3_err.append(f"... and {len(cfont_violations) - 40} additional CFont violations.")
    gates["gate_3_cfont_ascii"]["metrics"]["violation_count"] = len(cfont_violations)

    # Walk chapters, scenes, blocks, and vn_beats for Gates 1, 2, 4, 5, 6
    seen_scene_ids: set[str] = set()
    seen_block_ids: set[str] = set()
    seen_beat_ids: set[str] = set()
    referenced_cgs: set[str] = set()
    referenced_bgms: set[str] = set()

    all_book_paragraphs: list[str] = []
    all_vn_texts: list[str] = []

    total_scenes = 0
    total_blocks = 0
    total_non_break_blocks = 0
    total_beats = 0
    soft_over_240_beats = 0
    max_beat_len = 0

    for ch_entry, ch_data, ch_path in loaded_chapters:
        ch_id = ch_entry.get("chapter_id")
        if ch_data.get("chapter_id") != ch_id:
            g1_err.append(
                f"{ch_path.name}: chapter_id {ch_data.get('chapter_id')!r} != manifest {ch_id!r}."
            )
        if ch_data.get("index") != ch_entry.get("index"):
            g1_err.append(
                f"{ch_path.name}: index {ch_data.get('index')!r} != manifest {ch_entry.get('index')!r}."
            )
        if ch_data.get("title") != ch_entry.get("title"):
            g1_err.append(
                f"{ch_path.name}: title {ch_data.get('title')!r} != manifest {ch_entry.get('title')!r}."
            )

        scenes = ch_data.get("scenes")
        if not isinstance(scenes, list) or not scenes:
            g1_err.append(f"{ch_path.name}: 'scenes' must be a non-empty array.")
            continue

        ch_block_count = 0
        ch_beat_count = 0
        ch_book_texts: list[str] = []

        for s_idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                g1_err.append(f"{ch_path.name}.scenes[{s_idx}] must be an object.")
                continue
            total_scenes += 1
            sid = scene.get("scene_id", "")
            if not isinstance(sid, str) or not sid.strip():
                g1_err.append(f"{ch_path.name}.scenes[{s_idx}] missing scene_id.")
            elif sid in seen_scene_ids:
                g1_err.append(f"Duplicate scene_id '{sid}'.")
            seen_scene_ids.add(sid)

            for req_k in ("title", "location", "time_of_day", "ambience_note"):
                if not isinstance(scene.get(req_k), str) or not scene[req_k].strip():
                    g1_err.append(f"Scene '{sid}' missing non-empty '{req_k}'.")

            trans = scene.get("transition_in")
            if trans not in VALID_TRANSITIONS:
                g1_err.append(
                    f"Scene '{sid}' invalid transition_in={trans!r} (expected {sorted(VALID_TRANSITIONS)})."
                )

            def_bgm = scene.get("default_bgm_id", "")
            if not isinstance(def_bgm, str) or not def_bgm.strip():
                g1_err.append(f"Scene '{sid}' missing default_bgm_id.")
            else:
                referenced_bgms.add(def_bgm)

            blocks = scene.get("blocks")
            if not isinstance(blocks, list) or not blocks:
                g1_err.append(f"Scene '{sid}' must contain a non-empty 'blocks' array.")
                continue

            for b_idx, block in enumerate(blocks):
                if not isinstance(block, dict):
                    g1_err.append(f"Scene '{sid}'.blocks[{b_idx}] must be an object.")
                    continue
                total_blocks += 1
                ch_block_count += 1

                bid = block.get("block_id", "")
                if not isinstance(bid, str) or not bid.strip():
                    g1_err.append(f"Scene '{sid}'.blocks[{b_idx}] missing block_id.")
                elif bid in seen_block_ids:
                    g1_err.append(f"Duplicate block_id '{bid}'.")
                seen_block_ids.add(bid)

                bkind = block.get("kind")
                if bkind not in VALID_BLOCK_KINDS:
                    g1_err.append(
                        f"Block '{bid}' invalid kind={bkind!r} (expected {sorted(VALID_BLOCK_KINDS)})."
                    )

                bcg = block.get("cg_id", "")
                bbgm = block.get("bgm_id", "")
                if not isinstance(bcg, str) or not bcg.strip():
                    g2_err.append(f"Block '{bid}' missing cg_id.")
                else:
                    referenced_cgs.add(bcg)
                if not isinstance(bbgm, str) or not bbgm.strip():
                    g2_err.append(f"Block '{bid}' missing bgm_id.")
                else:
                    referenced_bgms.add(bbgm)

                book_text = block.get("book_text", "")
                vn_beats = block.get("vn_beats")
                if not isinstance(vn_beats, list):
                    g2_err.append(f"Block '{bid}'.vn_beats must be an array.")
                    vn_beats = []

                if bkind == "scene_break":
                    if book_text != "---":
                        g2_err.append(
                            f"scene_break block '{bid}' must have book_text == '---', got {book_text!r}."
                        )
                    if len(vn_beats) != 0:
                        g2_err.append(
                            f"scene_break block '{bid}' must have empty vn_beats [], got {len(vn_beats)} beats."
                        )
                    continue

                total_non_break_blocks += 1
                if not isinstance(book_text, str) or not book_text.strip():
                    g2_err.append(f"Block '{bid}' ({bkind}) has empty book_text.")
                else:
                    all_book_paragraphs.append(book_text)
                    ch_book_texts.append(book_text)

                if len(vn_beats) < 1:
                    g2_err.append(
                        f"Non-break block '{bid}' ({bkind}) must contain >= 1 vn_beats."
                    )
                    continue

                first_beat_cg = vn_beats[0].get("cg_id") if isinstance(vn_beats[0], dict) else None
                if first_beat_cg and bcg and first_beat_cg != bcg:
                    g2_err.append(
                        f"Dual-Mode anchor mismatch in block '{bid}': block.cg_id='{bcg}' != vn_beats[0].cg_id='{first_beat_cg}'."
                    )

                block_vn_chunks: list[str] = []
                for v_idx, beat in enumerate(vn_beats):
                    if not isinstance(beat, dict):
                        g2_err.append(f"Block '{bid}'.vn_beats[{v_idx}] must be an object.")
                        continue
                    total_beats += 1
                    ch_beat_count += 1

                    vid = beat.get("beat_id", "")
                    if not isinstance(vid, str) or not vid.strip():
                        g1_err.append(f"Block '{bid}'.vn_beats[{v_idx}] missing beat_id.")
                    elif vid in seen_beat_ids:
                        g1_err.append(f"Duplicate beat_id '{vid}'.")
                    seen_beat_ids.add(vid)

                    speaker = beat.get("speaker", "")
                    if not isinstance(speaker, str) or not speaker.strip():
                        g4_err.append(f"Beat '{vid}' missing non-empty speaker.")

                    btext = beat.get("text", "")
                    if not isinstance(btext, str) or not btext.strip():
                        g4_err.append(f"Beat '{vid}' has empty text.")
                    else:
                        tlen = len(btext)
                        max_beat_len = max(max_beat_len, tlen)
                        if tlen > 280:
                            g4_err.append(
                                f"Beat '{vid}' text length is {tlen} chars (> hard cap 280)."
                            )
                        elif tlen > 240:
                            soft_over_240_beats += 1
                        all_vn_texts.append(btext)
                        block_vn_chunks.append(btext)

                    ithought = beat.get("inner_thought", "")
                    if not isinstance(ithought, str):
                        g4_err.append(f"Beat '{vid}'.inner_thought must be a string.")
                    else:
                        if len(ithought) > 180:
                            g4_err.append(
                                f"Beat '{vid}' inner_thought length is {len(ithought)} chars (> 180)."
                            )
                        if ithought.strip():
                            all_vn_texts.append(ithought)
                            block_vn_chunks.append(ithought)

                    hold_ms = beat.get("auto_hold_ms")
                    if (
                        not isinstance(hold_ms, int)
                        or isinstance(hold_ms, bool)
                        or not (2000 <= hold_ms <= 10000)
                    ):
                        g4_err.append(
                            f"Beat '{vid}' auto_hold_ms={hold_ms!r} outside required [2000, 10000] ms."
                        )

                    vcg = beat.get("cg_id", "")
                    vbgm = beat.get("bgm_id", "")
                    if not isinstance(vcg, str) or not vcg.strip():
                        g2_err.append(f"Beat '{vid}' missing cg_id.")
                    else:
                        referenced_cgs.add(vcg)
                    if not isinstance(vbgm, str) or not vbgm.strip():
                        g2_err.append(f"Beat '{vid}' missing bgm_id.")
                    else:
                        referenced_bgms.add(vbgm)

                # Block-level sanity check that vn_beats truly came from book_text
                b_words = count_words(book_text)
                v_words = count_words(" ".join(block_vn_chunks))
                if b_words >= 12 and v_words < int(b_words * 0.70):
                    g2_err.append(
                        f"Block '{bid}' vn_beats severely truncates book_text ({v_words}/{b_words} words)."
                    )

        # Verify manifest chapter summary counts match actual chapter contents
        ch_words = count_words("\n".join(ch_book_texts))
        if ch_entry.get("scene_count") != len(scenes):
            g1_err.append(
                f"manifest.chapters['{ch_id}'].scene_count={ch_entry.get('scene_count')} != actual {len(scenes)}."
            )
        if ch_entry.get("block_count") != ch_block_count:
            g1_err.append(
                f"manifest.chapters['{ch_id}'].block_count={ch_entry.get('block_count')} != actual {ch_block_count}."
            )
        if ch_entry.get("beat_count") != ch_beat_count:
            g1_err.append(
                f"manifest.chapters['{ch_id}'].beat_count={ch_entry.get('beat_count')} != actual {ch_beat_count}."
            )
        declared_ch_words = ch_entry.get("word_count")
        if isinstance(declared_ch_words, int) and ch_words > 0:
            if abs(declared_ch_words - ch_words) > max(5, int(ch_words * 0.01)):
                g1_err.append(
                    f"manifest.chapters['{ch_id}'].word_count={declared_ch_words} differs from measured {ch_words}."
                )

    if soft_over_240_beats > 0:
        g4_warn.append(
            f"{soft_over_240_beats} beat(s) exceed recommended 240 chars (all <= 280 hard cap)."
        )

    gates["gate_1_schema_structure"]["metrics"] = {
        "chapter_count": len(loaded_chapters),
        "scene_count": total_scenes,
        "block_count": total_blocks,
        "beat_count": total_beats,
    }
    gates["gate_4_beat_constraints"]["metrics"] = {
        "total_beats": total_beats,
        "max_beat_chars": max_beat_len,
        "beats_241_to_280_chars": soft_over_240_beats,
    }

    # Gate 2: Global Word Count & Raw Source Verbatim Coverage Audit
    joined_book = "\n".join(all_book_paragraphs)
    joined_vn = "\n".join(all_vn_texts)
    measured_book_words = count_words(joined_book)
    measured_vn_words = count_words(joined_vn)

    work_id = manifest.get("work_id", pack_dir.name) if manifest else pack_dir.name
    resolved_raw_dir: Path | None = None
    if sources_dir is not None:
        cand = sources_dir / "raw_chapters" if (sources_dir / "raw_chapters").is_dir() else sources_dir
        if cand.is_dir():
            resolved_raw_dir = cand
    else:
        default_cand = STUDIO_ROOT / "sources" / str(work_id) / "raw_chapters"
        if default_cand.is_dir():
            resolved_raw_dir = default_cand

    raw_files: list[Path] = []
    if resolved_raw_dir is not None:
        raw_files = sorted(
            p for p in resolved_raw_dir.iterdir() if p.is_file() and not p.name.startswith(".")
        )

    prov = manifest.get("source_provenance", {}) if isinstance(manifest.get("source_provenance"), dict) else {}
    declared_raw_words = int(prov.get("raw_word_count") or 0)

    if raw_files:
        raw_corpus = "\n".join(p.read_text(encoding="utf-8") for p in raw_files)
        book_cov = compute_verbatim_coverage(raw_corpus, joined_book)
        vn_cov = compute_verbatim_coverage(raw_corpus, joined_vn)
        baseline_raw_words = book_cov["raw_word_count"]

        if declared_raw_words > 0 and abs(declared_raw_words - baseline_raw_words) > max(
            10, int(baseline_raw_words * 0.01)
        ):
            g2_err.append(
                f"manifest.source_provenance.raw_word_count ({declared_raw_words}) != measured raw_chapters word count ({baseline_raw_words})."
            )

        if not (0.99 <= book_cov["word_count_ratio"] <= 1.01) or book_cov["verbatim_coverage_ratio"] < 0.99:
            g2_err.append(
                f"Mode 2 (nvl_book) failed 99%-100% verbatim audit against raw_chapters: "
                f"word_count_ratio={book_cov['word_count_ratio']:.4f}, "
                f"verbatim_coverage_ratio={book_cov['verbatim_coverage_ratio']:.4f}, "
                f"missing_samples={book_cov['missing_word_samples'][:8]}"
            )

        if vn_cov["word_count_ratio"] < 0.95 or vn_cov["verbatim_coverage_ratio"] < 0.95:
            g2_err.append(
                f"Mode 1 (adv_cinema) failed >=95% unabridged audit against raw_chapters: "
                f"word_count_ratio={vn_cov['word_count_ratio']:.4f}, "
                f"verbatim_coverage_ratio={vn_cov['verbatim_coverage_ratio']:.4f}"
            )

        gates["gate_2_dual_mode_verbatim"]["metrics"] = {
            "raw_source_verified_from_disk": True,
            "raw_chapters_dir": str(resolved_raw_dir),
            "raw_word_count": baseline_raw_words,
            "pack_book_word_count": measured_book_words,
            "pack_vn_word_count": measured_vn_words,
            "book_word_ratio": book_cov["word_count_ratio"],
            "book_verbatim_coverage_ratio": book_cov["verbatim_coverage_ratio"],
            "vn_word_ratio": vn_cov["word_count_ratio"],
            "vn_verbatim_coverage_ratio": vn_cov["verbatim_coverage_ratio"],
        }
    else:
        g2_warn.append(
            "No raw_chapters/ folder found on disk; auditing word ratios against manifest.source_provenance.raw_word_count."
        )
        baseline_raw_words = declared_raw_words
        if baseline_raw_words <= 0:
            g2_err.append("Cannot verify word coverage: raw_word_count <= 0 and no raw_chapters/ found.")
        else:
            book_ratio = round(measured_book_words / baseline_raw_words, 6)
            vn_ratio = round(measured_vn_words / baseline_raw_words, 6)
            if not (0.99 <= book_ratio <= 1.01):
                g2_err.append(
                    f"Mode 2 pack_book_word_count ratio {book_ratio:.4f} ({measured_book_words}/{baseline_raw_words}) outside required [0.99, 1.01]."
                )
            if vn_ratio < 0.95:
                g2_err.append(
                    f"Mode 1 pack_vn_word_count ratio {vn_ratio:.4f} ({measured_vn_words}/{baseline_raw_words}) < required 0.95."
                )
            gates["gate_2_dual_mode_verbatim"]["metrics"] = {
                "raw_source_verified_from_disk": False,
                "raw_word_count": baseline_raw_words,
                "pack_book_word_count": measured_book_words,
                "pack_vn_word_count": measured_vn_words,
                "book_word_ratio": book_ratio,
                "vn_word_ratio": vn_ratio,
            }

    # Gate 5: CG Catalog, Physical Files, RGB Baseline JPEG & SHA-256 Uniqueness
    cg_catalog = manifest.get("cg_catalog") if manifest else None
    pending_cg_ids: list[str] = []
    verified_cg_count = 0
    seen_cg_hashes: dict[str, str] = {}

    if not isinstance(cg_catalog, dict) or not cg_catalog:
        g5_err.append("manifest.cg_catalog must be a non-empty object.")
        cg_catalog = {}

    for ref_cg in sorted(referenced_cgs):
        if ref_cg not in cg_catalog:
            g5_err.append(
                f"Chapter data references cg_id '{ref_cg}' which is missing from manifest.cg_catalog."
            )

    # Check cg_generation_manifest pending items
    manifest_pending_entries = [
        k for k, st in cg_status_map.items() if st == "pending" and "/" not in k and "." not in k
    ]
    if manifest_pending_entries and not allow_pending_cgs:
        g5_err.append(
            f"cg_generation_manifest.json still has {len(manifest_pending_entries)} 'pending' item(s): {manifest_pending_entries[:10]}."
        )

    for cg_id, cg_meta in cg_catalog.items():
        if not isinstance(cg_meta, dict):
            g5_err.append(f"cg_catalog['{cg_id}'] must be an object.")
            continue

        rel_file = cg_meta.get("file", "")
        if not isinstance(rel_file, str) or not rel_file.startswith("cgs/") or not rel_file.endswith(".jpg"):
            g5_err.append(
                f"cg_catalog['{cg_id}'].file must be 'cgs/<name>.jpg', got {rel_file!r}."
            )

        if cg_meta.get("width") != CG_16X9_SIZE[0] or cg_meta.get("height") != CG_16X9_SIZE[1]:
            g5_err.append(
                f"cg_catalog['{cg_id}'] metadata width/height must be 1376x768."
            )

        kind = cg_meta.get("kind")
        if kind not in VALID_CG_KINDS:
            g5_err.append(
                f"cg_catalog['{cg_id}'].kind must be 'base' or 'diff', got {kind!r}."
            )
        parent_id = cg_meta.get("parent_cg_id")
        if kind == "diff":
            if not isinstance(parent_id, str) or parent_id not in cg_catalog:
                g5_err.append(
                    f"Diff CG '{cg_id}' must reference a valid parent_cg_id in cg_catalog, got {parent_id!r}."
                )

        fp = cg_meta.get("focal_point")
        if (
            not isinstance(fp, list)
            or len(fp) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0 for v in fp)
        ):
            g5_err.append(
                f"cg_catalog['{cg_id}'].focal_point must be [fx, fy] in 0.0..1.0."
            )

        if not isinstance(cg_meta.get("characters_present"), list):
            g5_err.append(f"cg_catalog['{cg_id}'].characters_present must be an array.")
        if not isinstance(cg_meta.get("scene_summary"), str) or not cg_meta["scene_summary"].strip():
            g5_err.append(f"cg_catalog['{cg_id}'].scene_summary must be a non-empty string.")

        abs_cg = pack_dir / rel_file if isinstance(rel_file, str) and rel_file else None
        is_marked_pending = (
            cg_status_map.get(cg_id) == "pending"
            or (isinstance(rel_file, str) and cg_status_map.get(rel_file) == "pending")
        )

        if abs_cg is None or not abs_cg.is_file():
            if allow_pending_cgs and is_marked_pending:
                pending_cg_ids.append(cg_id)
                continue
            g5_err.append(f"Missing physical CG file on disk: '{rel_file}' (cg_id='{cg_id}').")
            continue

        img_errs = _inspect_jpeg(abs_cg, CG_16X9_SIZE, f"CG '{cg_id}'")
        g5_err.extend(img_errs)

        actual_hash = _sha256_file(abs_cg)
        declared_hash = cg_meta.get("sha256", "")
        if not isinstance(declared_hash, str) or not HEX64_RE.match(declared_hash):
            g5_err.append(
                f"cg_catalog['{cg_id}'].sha256 must be a 64-char lowercase hex SHA-256."
            )
        elif declared_hash != actual_hash:
            g5_err.append(
                f"cg_catalog['{cg_id}'].sha256 mismatch: manifest={declared_hash[:12]}... != disk={actual_hash[:12]}..."
            )

        if actual_hash in seen_cg_hashes:
            g5_err.append(
                f"Duplicate CG image SHA-256 detected between '{cg_id}' and '{seen_cg_hashes[actual_hash]}' ({actual_hash[:16]}...)."
            )
        else:
            seen_cg_hashes[actual_hash] = cg_id

        verified_cg_count += 1

    if pending_cg_ids:
        g5_warn.append(
            f"{len(pending_cg_ids)} CG(s) pending quota recovery (--allow-pending-cgs active)."
        )

    gates["gate_5_cg_integrity"]["metrics"] = {
        "catalog_cg_count": len(cg_catalog),
        "verified_on_disk": verified_cg_count,
        "pending_quota_count": len(pending_cg_ids),
        "unique_sha256_count": len(seen_cg_hashes),
    }

    # Gate 6: BGM Catalog & Physical Win32 MCI Compliance
    bgm_catalog = manifest.get("bgm_catalog") if manifest else None
    verified_bgm_count = 0

    if not isinstance(bgm_catalog, dict) or not bgm_catalog:
        g6_err.append("manifest.bgm_catalog must be a non-empty object.")
        bgm_catalog = {}

    for ref_bgm in sorted(referenced_bgms):
        if ref_bgm not in bgm_catalog:
            g6_err.append(
                f"Chapter/scene data references bgm_id '{ref_bgm}' which is missing from manifest.bgm_catalog."
            )

    for bgm_id, bgm_meta in bgm_catalog.items():
        if not isinstance(bgm_meta, dict):
            g6_err.append(f"bgm_catalog['{bgm_id}'] must be an object.")
            continue

        rel_mp3 = bgm_meta.get("file", "")
        if not isinstance(rel_mp3, str) or not rel_mp3.startswith("bgm/"):
            g6_err.append(
                f"bgm_catalog['{bgm_id}'].file must be 'bgm/<slug>.mp3', got {rel_mp3!r}."
            )
            continue

        mp3_path = pack_dir / rel_mp3
        if not ASCII_MP3_NAME_RE.match(mp3_path.name):
            g6_err.append(
                f"BGM filename '{mp3_path.name}' must be lowercase ASCII matching ^[a-z0-9_]+\\.mp3$."
            )

        bucket = bgm_meta.get("fallback_mood_bucket")
        if bucket not in VALID_MOOD_BUCKETS:
            g6_err.append(
                f"bgm_catalog['{bgm_id}'].fallback_mood_bucket={bucket!r} invalid (expected {sorted(VALID_MOOD_BUCKETS)})."
            )

        mci_vol = bgm_meta.get("base_mci_volume")
        if (
            not isinstance(mci_vol, int)
            or isinstance(mci_vol, bool)
            or not (185 <= mci_vol <= 240)
        ):
            g6_err.append(
                f"bgm_catalog['{bgm_id}'].base_mci_volume={mci_vol!r} outside required [185, 240]."
            )

        if not mp3_path.is_file():
            g6_err.append(f"Missing physical BGM file: '{rel_mp3}'.")
            continue

        raw_head = mp3_path.read_bytes()[:3]
        if raw_head == b"ID3":
            g6_err.append(
                f"BGM '{rel_mp3}' contains an ID3v2 header (risks Win32 MCI Error 277); normalize via tools/audio_mci_normalizer.py."
            )

        try:
            probe = probe_audio_file(mp3_path)
            streams = probe.get("streams", [])
            fmt = probe.get("format", {})
            nb_streams = int(fmt.get("nb_streams", len(streams)))
            if nb_streams != 1 or len(streams) != 1:
                g6_err.append(
                    f"BGM '{rel_mp3}' has nb_streams={nb_streams} (must be 1 audio stream with zero embedded cover art)."
                )
            elif streams[0].get("codec_name") != "mp3":
                g6_err.append(
                    f"BGM '{rel_mp3}' codec is {streams[0].get('codec_name')!r}, expected 'mp3'."
                )
            else:
                sr = int(streams[0].get("sample_rate", 0))
                ch = int(streams[0].get("channels", 0))
                if sr != 44100 or ch != 2:
                    g6_err.append(
                        f"BGM '{rel_mp3}' audio format is {sr}Hz/{ch}ch, expected 44100Hz/2ch stereo."
                    )

            dur = float(fmt.get("duration") or (streams[0].get("duration") if streams else 0.0) or 0.0)
            if dur < MIN_DURATION_SEC:
                g6_err.append(
                    f"BGM '{rel_mp3}' duration is {dur:.2f}s (< required {MIN_DURATION_SEC:.1f}s)."
                )

            lead_sil = detect_lead_silence_sec(mp3_path, probe_window_sec=min(max(dur, 5.0), 10.0))
            if lead_sil > MAX_LEAD_SILENCE_SEC:
                g6_err.append(
                    f"BGM '{rel_mp3}' lead_silence_sec is {lead_sil:.2f}s (> allowed {MAX_LEAD_SILENCE_SEC:.1f}s)."
                )
            verified_bgm_count += 1
        except AudioNormalizationError as exc:
            g6_err.append(f"BGM '{rel_mp3}' probe failed: {exc}")

    gates["gate_6_bgm_integrity"]["metrics"] = {
        "catalog_bgm_count": len(bgm_catalog),
        "verified_on_disk": verified_bgm_count,
    }

    # Gate 7: Standalone CORS-Free Dual-Mode preview.html Check
    preview_path = pack_dir / "preview.html"
    if not preview_path.is_file():
        g7_err.append(
            "Missing preview.html in package root. Run `python tools/build_preview_html.py <pack_dir>`."
        )
    else:
        html_text = preview_path.read_text(encoding="utf-8", errors="replace")
        required_markers = [
            "eagle-lit-pack-data",
            "adv_cinema",
            "nvl_book",
            "<audio",
            "auto_hold_ms",
            "cg-gallery",
            "backlog",
        ]
        for marker in required_markers:
            if marker not in html_text:
                g7_err.append(
                    f"preview.html is missing required Dual-Mode component marker '{marker}'."
                )
        if re.search(r"\bfetch\s*\(", html_text):
            g7_err.append(
                "preview.html contains `fetch(...)` which breaks standalone file:/// playback due to browser CORS."
            )
        gates["gate_7_preview_html"]["metrics"] = {
            "size_bytes": preview_path.stat().st_size,
        }

    # Compute per-gate statuses and overall status
    total_errors = 0
    total_warnings = 0
    for g_name, g_obj in gates.items():
        errs = g_obj["errors"]
        warns = g_obj["warnings"]
        total_errors += len(errs)
        total_warnings += len(warns)
        if errs:
            g_obj["status"] = "FAIL"
        elif g_name == "gate_5_cg_integrity" and (pending_cg_ids or manifest_pending_entries) and allow_pending_cgs:
            g_obj["status"] = "PARTIAL_QUOTA_WAIT"
        else:
            g_obj["status"] = "PASS"

    has_pending_quota = bool(pending_cg_ids or manifest_pending_entries) or any(
        "pending quota recovery" in w for w in g1_warn
    )

    if total_errors > 0:
        overall_status = "FAIL"
    elif allow_pending_cgs and has_pending_quota:
        overall_status = "PARTIAL_QUOTA_WAIT"
    else:
        overall_status = "PASS"

    report: dict[str, Any] = {
        "schema_version": "eagle_lit_validation_v1",
        "work_id": manifest.get("work_id", pack_dir.name) if manifest else pack_dir.name,
        "pack_dir": str(pack_dir),
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "allow_pending_cgs": allow_pending_cgs,
        "overall_status": overall_status,
        "ready_for_gta_export": overall_status == "PASS",
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "summary": {
            "chapters": len(loaded_chapters),
            "scenes": total_scenes,
            "blocks_total": total_blocks,
            "blocks_prose": total_non_break_blocks,
            "vn_beats": total_beats,
            "book_words": measured_book_words,
            "vn_words": measured_vn_words,
            "cgs_verified": verified_cg_count,
            "cgs_pending_quota": len(pending_cg_ids),
            "bgms_verified": verified_bgm_count,
        },
        "gates": gates,
    }

    report_path = pack_dir / "validation_report.json"
    if pack_dir.is_dir():
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an EagleLit Pack v1.0 across all 7 Zero-Error Export Gates."
    )
    parser.add_argument(
        "pack_dir",
        help="Path to package directory (e.g. packages/rezero_ayamatsu_pride_if).",
    )
    parser.add_argument(
        "--sources-dir",
        help="Optional override path to sources/<work_id>/ directory for raw verbatim comparison.",
    )
    parser.add_argument(
        "--allow-pending-cgs",
        action="store_true",
        help="Allow pending CGs in cg_generation_manifest.json during 429 quota wait checkpoints (marks PARTIAL_QUOTA_WAIT).",
    )

    args = parser.parse_args(argv)
    pack_path = Path(args.pack_dir)
    if not pack_path.is_dir():
        print(f"ERROR: Package directory not found: {pack_path}", file=sys.stderr)
        return 2

    src_path = Path(args.sources_dir) if args.sources_dir else None
    report = validate_pack(
        pack_path,
        sources_dir=src_path,
        allow_pending_cgs=args.allow_pending_cgs,
    )

    print(
        f"[{report['overall_status']}] {report['work_id']} -- "
        f"errors={report['total_errors']}, warnings={report['total_warnings']}, "
        f"chapters={report['summary']['chapters']}, blocks={report['summary']['blocks_total']}, "
        f"beats={report['summary']['vn_beats']}, cgs_done={report['summary']['cgs_verified']}, "
        f"cgs_pending={report['summary']['cgs_pending_quota']}, bgms={report['summary']['bgms_verified']}"
    )
    for g_name, g_obj in report["gates"].items():
        print(
            f"  - {g_name}: {g_obj['status']} "
            f"(errors={len(g_obj['errors'])}, warnings={len(g_obj['warnings'])})"
        )
        for err in g_obj["errors"][:5]:
            print(f"      ERROR: {err}")

    print(f"Report written to: {pack_path.resolve() / 'validation_report.json'}")

    if report["overall_status"] in {"PASS", "PARTIAL_QUOTA_WAIT"} and report["total_errors"] == 0:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
