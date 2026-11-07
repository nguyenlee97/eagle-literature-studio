#!/usr/bin/env python3
"""Quota-Safe Codex Batch CG Runner for Eagle Literature Studio (`EagleLit Pack v1.0`).

DEFAULT STUDIO CG ENGINE (unless user explicitly requests OpenAI API for a specific batch):
- Calls local `codex.exe exec -m gpt-6.1-terra -c model_reasoning_effort="high"` by default.
- Disables all MCP servers & plugins (`-c mcp_servers={} -c plugins={}`) and runs in an
  isolated read-only empty directory (`-s read-only -C <empty_cwd>`) so Codex invokes
  `image_generation` in a single lean turn.
- Passes up to 10 verified on-disk reference images via `-i`:
  1. For `diff` CGs: un-cropped raw parent Base PNG (`raw_cache/<parent_id>_raw.png`) as Image 1
     so Base <-> Diff crops remain 100% pixel-aligned.
  2. 100% of declared `reference_files` (LN volume scene illustrations, character sheets,
     official color samples, locations), verified to exist on disk before execution.
  3. Auto-supplements any missing canonical character sheet for characters in `characters_present`
     and ensures at least one official color plate is attached.
- Supports `--cluster-id cluster_XX` for parallel Long-Form Novel cluster packages and syncs
  completion status across `cg_generation_manifest.json`, `manifest.json`, and `clusters/cluster_XX_cg_plan.json`.
- Immediately halts the batch if any quota / rate-limit / 429 pattern is detected.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from PIL import Image

sys.stdout.reconfigure(encoding="utf-8")

STUDIO_ROOT = Path(r"D:\Eagle Literature Studio")
CODEX_GEN_ROOT = Path.home() / ".codex" / "generated_images"


@contextmanager
def manifest_file_lock(pack_dir: Path, timeout_sec: float = 120.0):
    """Cross-process Windows file lock so 12 parallel cluster runners never overwrite manifest.json."""
    lock_path = pack_dir / ".manifest_write.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")
    t0 = time.time()
    locked = False
    try:
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.time() - t0 > timeout_sec:
                    raise TimeoutError(f"Timed out waiting for manifest lock at {lock_path}")
                time.sleep(0.15 + random.uniform(0.02, 0.12))
        yield
    finally:
        if locked:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        f.close()

QUOTA_ERROR_PATTERNS = (
    "usage_limit",
    "rate_limit",
    "resource_exhausted",
    "hit your usage limit",
    "you've reached your",
    "quota exceeded",
    "too many requests",
    "status 429",
    "error 429",
    "insufficient_quota",
    "billing_hard_limit_reached",
)

# Canonical character-to-sheet mapping for Re:Zero Arc 7 & studio franchises
ARC7_CHARACTER_SHEET_MAP: dict[str, str] = {
    "rem": "references/rezero_arc7/characters/rem_arc7_ln_sheet.jpg",
    "louis": "references/rezero_arc7/characters/louis_arneb_ln_sheet.jpg",
    "spica": "references/rezero_arc7/characters/louis_arneb_ln_sheet.jpg",
    "abel": "references/rezero_arc7/characters/vincent_vollachia_ln_sheet.jpg",
    "vincent": "references/rezero_arc7/characters/vincent_vollachia_ln_sheet.jpg",
    "mizelda": "references/rezero_arc7/characters/mizelda_ln_sheet.jpg",
    "taritta": "references/rezero_arc7/characters/taritta_arc7_hunter_sheet.jpg",
    "utakata": "references/rezero_arc7/characters/utakata_ln_sheet.jpg",
    "holly": "references/rezero_arc7/characters/holly_kuna_shudraq_sheet.jpg",
    "kuna": "references/rezero_arc7/characters/holly_kuna_shudraq_sheet.jpg",
    "todd": "references/rezero_arc7/characters/todd_fang_ln_sheet.jpg",
    "jamal": "references/rezero_arc7/characters/jamal_aurelie_ln_sheet.jpg",
    "flop": "references/rezero_arc7/characters/flop_oconnell_ln_sheet.jpg",
    "medium": "references/rezero_arc7/characters/medium_oconnell_ln_sheet.jpg",
    "zikr": "references/rezero_arc7/characters/zikr_osman_ln_sheet.jpg",
    "arakiya": "references/rezero_arc7/characters/arakiya_ln_sheet.jpg",
    "priscilla": "references/rezero_arc7/characters/priscilla_barielle_ln_sheet.jpg",
    "al": "references/rezero_arc7/characters/al_aldebaran_ln_sheet.jpg",
    "aldebaran": "references/rezero_arc7/characters/al_aldebaran_ln_sheet.jpg",
    "schult": "references/rezero_arc7/characters/schult_ln_sheet.jpg",
    "heinkel": "references/rezero_arc7/characters/heinkel_astrea_ln_sheet.jpg",
    "yorna": "references/rezero_arc7/characters/yorna_mishigure_ln_sheet.jpg",
    "tanza": "references/rezero_arc7/characters/tanza_ln_sheet.jpg",
    "olbart": "references/rezero_arc7/characters/olbart_dunkelkenn_ln_sheet.jpg",
    "kafma": "references/rezero_arc7/characters/kafma_irulux_ln_sheet.jpg",
    "madeline": "references/rezero_arc7/characters/madeline_eschart_ln_sheet.jpg",
    "cecilus": "references/rezero_arc7/characters/cecilus_child_ln_sheet.jpg",
    "chisha": "references/rezero_arc7/characters/chisha_gold_ln_sheet.jpg",
    "goz": "references/rezero_arc7/characters/goz_ralfon_ln_sheet.jpg",
    "groovy": "references/rezero_arc7/characters/groovy_gumlet_ln_sheet.jpg",
    "moguro": "references/rezero_arc7/characters/moguro_hagane_ln_sheet.jpg",
    "berstetz": "references/rezero_arc7/characters/berstetz_fondalfon_ln_sheet.jpg",
    "katya": "references/rezero_arc7/characters/katya_aurelie_ln_sheet.jpg",
    "gustav": "references/rezero_arc7/characters/gustav_morello_ln_sheet.jpg",
    "weitz": "references/rezero_arc7/characters/weitz_gladiator_ln_sheet.jpg",
    "idra": "references/rezero_arc7/characters/idra_gladiator_ln_sheet.jpg",
    "hiain": "references/rezero_arc7/characters/hiain_gladiator_ln_sheet.jpg",
    "sparka": "references/rezero_arc7/characters/sparka_war_wolf_sheet.jpg",
    "serena": "references/rezero_arc7/characters/serena_dracroy_ln_sheet.jpg",
    "emilia": "references/rezero_arc7/characters/emilia_ln_sheet.jpg",
    "beatrice": "references/rezero_arc7/characters/beatrice_ln_sheet.jpg",
    "ram": "references/rezero_arc7/characters/ram_ln_sheet.jpg",
    "garfiel": "references/rezero_arc7/characters/garfiel_tinsel_ln_sheet.jpg",
    "otto": "references/rezero_arc7/characters/otto_suwen_ln_sheet.jpg",
    "roswaal": "references/rezero_arc7/characters/roswaal_mathers_ln_sheet.jpg",
    "petra": "references/rezero_arc7/characters/petra_leyte_ln_sheet.jpg",
    "frederica": "references/rezero_arc7/characters/frederica_baumann_ln_sheet.jpg",
    "satella": "references/rezero_arc7/characters/satella_shadow_sheet.jpg",
    "echidna": "references/rezero_arc7/characters/echidna_omega_sheet.jpg",
    "sphinx": "references/rezero_arc7/characters/sphinx_witch_sheet.jpg",
    "natsumi": "references/rezero_arc7/characters/subaru_natsumi_schwartz_sheet.jpg",
}


def find_codex_exe() -> Path:
    default = Path(r"C:\Users\LENOVO\AppData\Local\OpenAI\Codex\bin\a159911ff70742e9\codex.exe")
    if default.is_file():
        return default
    bin_root = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    if bin_root.is_dir():
        candidates = sorted(bin_root.rglob("codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    raise FileNotFoundError("Could not locate codex.exe under AppData/Local/OpenAI/Codex/bin")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def normalize_to_baseline_jpeg(
    src: Path,
    dst: Path,
    target_size: tuple[int, int],
    top_bias: float = 0.38,
) -> str:
    """Convert raw PNG (`1536x1024` or `1024x1536`) to exact RGB baseline JPEG (`1376x768` or `832x1248`).

    Uses deterministic `top_bias=0.38` when cropping 3:2 (1536x1024) -> 16:9 (1536x857)
    so headroom above hair is preserved and Base <-> Diff crops stay 100% pixel-aligned.
    """
    tw, th = target_size
    target_ratio = tw / th
    with Image.open(src) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        cur_ratio = w / h
        if abs(cur_ratio - target_ratio) > 0.005:
            if cur_ratio > target_ratio:
                new_w = int(round(h * target_ratio))
                left = (w - new_w) // 2
                rgb = rgb.crop((left, 0, left + new_w, h))
            else:
                new_h = int(round(w / target_ratio))
                top = int(round((h - new_h) * top_bias))
                top = max(0, min(h - new_h, top))
                rgb = rgb.crop((0, top, w, top + new_h))
        resized = rgb.resize((tw, th), Image.Resampling.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        resized.save(
            dst,
            format="JPEG",
            quality=92,
            optimize=True,
            progressive=False,
            subsampling=0,
        )
    return sha256_file(dst)


def save_checkpoints(
    cg_manifest: dict,
    pack_manifest: dict,
    cg_manifest_path: Path,
    pack_manifest_path: Path,
    quota_policy: str,
    cluster_plan_path: Path | None = None,
    updated_item: dict | None = None,
) -> None:
    pack_dir = cg_manifest_path.parent
    with manifest_file_lock(pack_dir):
        if cg_manifest_path.is_file():
            try:
                disk_cg = json.loads(cg_manifest_path.read_text(encoding="utf-8"))
                disk_by_id = {it["asset_id"]: it for it in disk_cg.get("items", []) if "asset_id" in it}
                for mem_it in cg_manifest.get("items", []):
                    aid = mem_it["asset_id"]
                    if updated_item is not None and aid == updated_item.get("asset_id"):
                        disk_by_id[aid] = mem_it
                    elif aid in disk_by_id:
                        d_it = disk_by_id[aid]
                        for k in ("reference_files", "characters_present", "scene_summary", "prompt", "generation_prompt", "prompt_brief"):
                            if k in d_it:
                                mem_it[k] = d_it[k]
                        if d_it.get("status") == "done":
                            mem_it["status"] = "done"
                            mem_it["sha256"] = d_it.get("sha256")
                            mem_it["completed_at"] = d_it.get("completed_at")
                            if d_it.get("engine"):
                                mem_it["engine"] = d_it["engine"]
            except Exception:
                pass

        if pack_manifest_path.is_file():
            try:
                disk_pack = json.loads(pack_manifest_path.read_text(encoding="utf-8"))
                disk_cat = disk_pack.get("cg_catalog", {})
                mem_cat = pack_manifest.setdefault("cg_catalog", {})
                for cid_key, cval in disk_cat.items():
                    if updated_item is not None and cid_key == updated_item.get("asset_id"):
                        continue
                    if cval.get("sha256") and cval.get("sha256") != ("0" * 64):
                        if cid_key in mem_cat:
                            mem_cat[cid_key]["sha256"] = cval["sha256"]
                            mem_cat[cid_key]["width"] = cval.get("width", mem_cat[cid_key].get("width", 1376))
                            mem_cat[cid_key]["height"] = cval.get("height", mem_cat[cid_key].get("height", 768))
            except Exception:
                pass

        items = cg_manifest["items"]
        done_count = sum(1 for it in items if it.get("status") == "done")
        cg_manifest["updated_at"] = now_iso()
        cg_manifest["quota_policy"] = quota_policy
        cg_manifest["summary"]["total"] = len(items)
        cg_manifest["summary"]["done"] = done_count
        cg_manifest["summary"]["pending"] = len(items) - done_count

        cg_manifest_path.write_text(json.dumps(cg_manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        pack_manifest_path.write_text(json.dumps(pack_manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

        if cluster_plan_path and cluster_plan_path.is_file() and updated_item is not None:
            try:
                cplan = json.loads(cluster_plan_path.read_text(encoding="utf-8"))
                for cit in cplan.get("items", []):
                    if cit.get("asset_id") == updated_item.get("asset_id"):
                        cit["status"] = updated_item["status"]
                        cit["sha256"] = updated_item["sha256"]
                        cit["completed_at"] = updated_item["completed_at"]
                        cit["engine"] = updated_item.get("engine")
                        break
                cluster_plan_path.write_text(json.dumps(cplan, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
            except Exception as exc:
                print(f"[WARN] Could not sync cluster plan {cluster_plan_path.name}: {exc}", flush=True)


def record_done_item(
    item: dict,
    sha_hex: str,
    cg_manifest: dict,
    pack_manifest: dict,
    cg_manifest_path: Path,
    pack_manifest_path: Path,
    usage: dict | None = None,
    source_note: str = "codex_gpt_6_1_terra_high",
) -> None:
    item["status"] = "done"
    item["sha256"] = sha_hex
    item["completed_at"] = now_iso()
    item["engine"] = source_note
    if usage:
        item["token_usage"] = usage

    aid = item["asset_id"]
    if aid in pack_manifest.get("cg_catalog", {}):
        cat = pack_manifest["cg_catalog"][aid]
        cat["sha256"] = sha_hex
        cat["width"] = item["width"]
        cat["height"] = item["height"]

    cluster_plan_path: Path | None = None
    cid = item.get("cluster_id")
    if cid:
        cand_plan = cg_manifest_path.parent / "clusters" / f"{cid}_cg_plan.json"
        if cand_plan.is_file():
            cluster_plan_path = cand_plan

    save_checkpoints(
        cg_manifest,
        pack_manifest,
        cg_manifest_path,
        pack_manifest_path,
        quota_policy=f"{source_note}_batch",
        cluster_plan_path=cluster_plan_path,
        updated_item=item,
    )


def strip_dialogue_quotes(text: str) -> str:
    """Remove parenthetical quoted dialogue snippets that might cause text rendering on canvas."""
    cleaned = re.sub(r"\s*\(['\"][^'\"]{2,120}['\"]\)", "", text)
    return cleaned


def load_cluster_wardrobe_fallback(pack_dir: Path, item: dict) -> str:
    """Lookup cluster-specific Subaru visual phase & wardrobe notes if item lacks wardrobe_lock."""
    cid = item.get("cluster_id")
    if not cid:
        return ""
    brief_path = pack_dir / "clusters" / f"{cid}_brief.json"
    if not brief_path.is_file():
        return ""
    try:
        bdata = json.loads(brief_path.read_text(encoding="utf-8"))
        phase_det = bdata.get("subaru_visual_phase_details") or {}
        desc = phase_det.get("wardrobe_and_anatomy") or bdata.get("subaru_visual_phase") or ""
        if desc:
            return f"- Wardrobe & Character Phase Lock ({cid}): {desc}"
    except Exception:
        pass
    return ""


def infer_subaru_sheet_for_item(item: dict, pack_dir: Path | None = None) -> list[str]:
    """Select the exact phase-accurate Subaru reference sheet(s) for an Arc 7 or IF route scene."""
    pack_name = pack_dir.name.lower() if pack_dir is not None else ""
    if pack_name and "arc7" not in pack_name:
        if "greed" in pack_name or "kasaneru" in pack_name:
            return [
                "references/rezero/characters/subaru_greed_if_otsuka.jpg",
                "references/rezero/characters/subaru_face_expressions.jpg",
            ]
        if "wrath" in pack_name or "oboreru" in pack_name:
            return [
                "references/rezero/characters/subaru_early_dark_suit.jpg",
                "references/rezero/characters/subaru_face_expressions.jpg",
            ]
        return [
            "references/rezero/characters/subaru_face_expressions.jpg",
            "references/rezero/characters/subaru_early_dark_suit.jpg",
        ]

    cid = str(item.get("cluster_id") or "")
    ch_id = str(item.get("chapter_id") or "")
    chars = {str(c).lower() for c in item.get("characters_present", [])}
    prompt_low = (
        str(item.get("generation_prompt") or item.get("prompt") or item.get("scene_summary") or "").lower()
    )

    if "natsumi" in chars or "natsumi" in prompt_low:
        if cid in ("cluster_03", "cluster_04") or "military" in prompt_low or "strategist" in prompt_low:
            return [
                "references/rezero_arc7/characters/subaru_military_advisor_sheet.jpg",
                "references/rezero_arc7/characters/subaru_natsumi_schwartz_sheet.jpg",
            ]
        return ["references/rezero_arc7/characters/subaru_natsumi_schwartz_sheet.jpg"]

    ch_num = 0
    m = re.search(r"ch(\d+)", ch_id)
    if m:
        ch_num = int(m.group(1))

    if cid in ("cluster_05", "cluster_06", "cluster_08", "cluster_09", "cluster_10", "cluster_11", "cluster_12") or (
        ch_num >= 40 and cid != "cluster_07"
    ) or "shotabaru" in prompt_low or "10-year-old" in prompt_low or "child subaru" in prompt_low:
        return [
            "references/rezero_arc7/characters/subaru_child_shotabaru_color_sheet.jpg",
            "references/rezero_arc7/characters/subaru_child_shotabaru_sheet.jpg",
        ]

    return [
        "references/rezero_arc7/characters/subaru_arc7_adult_tunic.jpg",
        "references/rezero_arc7/characters/subaru_adult_tracksuit_sheet.jpg",
    ]


def resolve_item_refs(
    item: dict,
    pack_dir: Path,
    raw_cache_dir: Path,
    max_refs: int = 10,
) -> list[Path]:
    """Resolve and strictly verify all reference images for a CG item.

    Guarantees:
    1. For `diff` CGs: Parent Base CG (`raw_cache/<parent_id>_raw.png` or `cgs/<parent_id>.jpg`)
       is ALWAYS attached as Image 1. Raises FileNotFoundError if missing.
    2. Every non-package file declared in `item['reference_files']` MUST exist on disk
       (raises FileNotFoundError if any declared reference is missing).
    3. Auto-supplements canonical character sheets for `item['characters_present']` if not
       already included in `reference_files`, plus at least 1 official color sample.
    """
    refs: list[Path] = []
    aid = item["asset_id"]
    is_arc7 = "arc7" in pack_dir.name.lower()

    if aid in ("poster", "banner_16x9"):
        prev = raw_cache_dir / f"{aid}_prev_raw.png"
        if prev.is_file():
            refs.append(prev)

    if item.get("kind") == "diff" and item.get("parent_cg_id"):
        parent_id = item["parent_cg_id"]
        raw_parent = raw_cache_dir / f"{parent_id}_raw.png"
        jpg_parent = pack_dir / "cgs" / f"{parent_id}.jpg"
        if raw_parent.is_file():
            refs.append(raw_parent)
        elif jpg_parent.is_file():
            refs.append(jpg_parent)
        else:
            raise FileNotFoundError(
                f"Parent Base CG missing on disk for diff {aid}: {parent_id} "
                f"(checked {raw_parent} and {jpg_parent})"
            )

    # Attach 100% of declared reference_files and verify each exists on disk
    for rel in item.get("reference_files", []):
        norm_rel = rel.replace("\\", "/")
        if item.get("kind") == "diff" and norm_rel.endswith(".jpg") and "packages/" in norm_rel:
            continue
        cand = (STUDIO_ROOT / rel).resolve()
        if not cand.is_file():
            raise FileNotFoundError(f"Declared reference_file missing on disk for {aid}: {rel} -> {cand}")
        if cand not in refs:
            refs.append(cand)

    # Auto-supplement character sheets for characters_present if not yet represented
    ref_names_joined = " ".join(p.name.lower() for p in refs)
    for char_id in item.get("characters_present", []):
        cid_low = str(char_id).lower().strip()
        if not cid_low:
            continue
        if cid_low == "subaru":
            if "subaru" not in ref_names_joined and "shotabaru" not in ref_names_joined and "natsumi" not in ref_names_joined:
                for sub_rel in infer_subaru_sheet_for_item(item, pack_dir=pack_dir):
                    sub_path = (STUDIO_ROOT / sub_rel).resolve()
                    if sub_path.is_file() and sub_path not in refs and len(refs) < max_refs:
                        refs.append(sub_path)
                        ref_names_joined += " " + sub_path.name.lower()
        elif cid_low in ARC7_CHARACTER_SHEET_MAP:
            token = cid_low.split("_")[0]
            if token not in ref_names_joined:
                cpath = (STUDIO_ROOT / ARC7_CHARACTER_SHEET_MAP[cid_low]).resolve()
                if cpath.is_file() and cpath not in refs and len(refs) < max_refs:
                    refs.append(cpath)
                    ref_names_joined += " " + cpath.name.lower()

    # Ensure at least one official Otsuka color style sample is present if room remains
    has_color_sample = any("official_art_samples" in str(p).replace("\\", "/") for p in refs)
    if not has_color_sample and len(refs) < max_refs:
        sample_rel = (
            "references/rezero_arc7/official_art_samples/otsuka_arc7_vol26_01.jpg"
            if is_arc7
            else "references/rezero/official_art_samples/otsuka_ln_vol01_cover.jpg"
        )
        default_sample = (STUDIO_ROOT / sample_rel).resolve()
        if default_sample.is_file() and default_sample not in refs:
            refs.append(default_sample)

    if not refs:
        raise ValueError(f"Zero reference images resolved for {aid}; studio policy forbids reference-free generation.")

    return refs[:max_refs]


def describe_ref_role(rp: Path, idx: int, kind: str) -> str:
    norm = str(rp).replace("\\", "/")
    if (kind == "diff" or rp.name.endswith("_prev_raw.png")) and idx == 1:
        return (
            "EDIT TARGET / PARENT BASE CG (preserve camera angle, background architecture, "
            "lighting geometry, and character outfits 100%; modify ONLY specified pose/expression/action)"
        )
    if "/ln_volumes/" in norm:
        return (
            f"Official Re:Zero Light Novel scene illustration by Shinichirou Otsuka (`{rp.name}`) — "
            "use as canonical scene staging, pose, expression, and outfit reference; render output in 100% Full-Color RGB"
        )
    if "/official_art_samples/" in norm:
        return "Official Re:Zero Light Novel color plate & Shinichirou Otsuka rendering style anchor"
    if "/locations/" in norm:
        return "Canonical environment / architecture / lighting reference"
    return f"Canonical character design & outfit reference sheet (`{rp.stem}`)"


def build_lean_prompt(
    item: dict,
    resolved_refs: list[Path],
    art_style_authority: str,
    pack_dir: Path | None = None,
    for_openai_api: bool = False,
) -> str:
    aid = item["asset_id"]
    kind = item.get("kind", "base")
    summary = strip_dialogue_quotes(item.get("scene_summary", ""))
    raw_prompt_src = (
        item.get("generation_prompt")
        or item.get("prompt")
        or item.get("prompt_brief")
        or item.get("scene_summary")
        or ""
    )
    raw_prompt = strip_dialogue_quotes(raw_prompt_src)
    wardrobe_lock = (item.get("wardrobe_lock") or "").strip()
    if not wardrobe_lock and pack_dir is not None:
        wardrobe_lock = load_cluster_wardrobe_fallback(pack_dir, item).strip()
    if wardrobe_lock and not wardrobe_lock.endswith("\n"):
        wardrobe_lock += "\n"

    ref_roles = [
        f"  - Image {idx} (`{rp.name}`): {describe_ref_role(rp, idx, kind)}"
        for idx, rp in enumerate(resolved_refs, start=1)
    ]
    refs_block = "\n".join(ref_roles)

    if kind == "poster":
        aspect_line = "Generate ONE vertical portrait 2:3 (`1024x1536`) full-color illustration."
        framing_line = (
            "Compose dramatic vertical movie/book key art with clear breathing room around heads and zero text/logos."
        )
    elif kind == "diff":
        aspect_line = (
            "Edit/generate ONE landscape (`1536x1024`) full-color Diff CG derived directly from Image 1 (the Parent Base CG)."
        )
        framing_line = (
            "CRITICAL CONTINUITY: Keep the exact camera angle, focal length, background architecture, lighting palette, "
            "and character outfits 100% locked to Image 1 (`Parent Base CG`), modifying ONLY the specified pose, action, "
            "or emotional expression. Keep all heads and faces inside vertical band y = 0.14..0.72."
        )
    else:
        aspect_line = "Generate ONE landscape (`1536x1024`) full-color anime/light-novel Base CG illustration."
        framing_line = (
            "CRITICAL 16:9 SAFE-ZONE FRAMING: This 1536x1024 canvas will be cropped to 16:9 widescreen with a bottom 22% "
            "Visual Novel dialogue HUD. Use a MEDIUM-WIDE cinematic camera leaving at least 14% ceiling/sky headroom above "
            "characters' hair and atmospheric floor/ground in the bottom 25%, keeping all heads, faces, and key hand actions "
            "cleanly inside y = 0.15..0.70."
        )

    if for_openai_api:
        header = (
            f"HIGH-PRECISION FULL-COLOR LIGHT NOVEL CG ILLUSTRATION (`{aid}`):\n"
            f"- Task: {aspect_line}\n"
            "- Strictly follow every attached Reference Image below for character faces, hair/eye colors, "
            "arc-specific outfits, weapons, scene composition, and lighting.\n\n"
        )
    else:
        header = (
            f"SINGLE-STEP IMAGE GENERATION TASK (`{aid}`):\n"
            f"- Immediately call your built-in `image_generation` tool ONCE ({aspect_line}).\n"
            "- Do NOT run any PowerShell/shell commands, do NOT read external skill/markdown files, and do NOT copy files.\n"
            "- Leave the generated PNG in its default `$CODEX_HOME/generated_images/...` path and reply with ONLY that file path.\n\n"
        )

    return (
        f"{header}"
        f"Input Reference Images ({len(resolved_refs)} attached):\n{refs_block}\n\n"
        "Art Direction & Scene Spec:\n"
        f"- Style Authority: {art_style_authority} (100% Full-Color Light Novel illustration matching Shinichirou Otsuka's "
        "official Re:Zero color plates combined with White Fox cinematic lighting: delicate crisp lineart, multi-layered "
        "expressive jewel eyes, rich painted shading; convert any B&W LN composition reference into vibrant full-color RGB).\n"
        f"{wardrobe_lock}"
        f"- Scene (`{aid}`): {summary}\n"
        f"- Detailed Visual Prompt: {raw_prompt}\n"
        f"- Framing: {framing_line}\n"
        "- STRICT CLEAN CANVAS: ZERO speech bubbles, ZERO English/Japanese text, ZERO subtitles, ZERO watermarks, ZERO UI frames."
    )


def select_batch_candidates(
    cg_manifest: dict,
    allowed_kinds: set[str],
    only_ids: set[str],
    cluster_ids: set[str],
    max_new: int,
) -> list[dict]:
    by_id = {it["asset_id"]: it for it in cg_manifest["items"]}
    candidates: list[dict] = []
    for it in cg_manifest["items"]:
        if cluster_ids and it.get("cluster_id") not in cluster_ids:
            continue
        if only_ids:
            if it["asset_id"] not in only_ids:
                continue
        else:
            if it.get("status") == "done":
                continue
            if it.get("kind", "base") not in allowed_kinds:
                continue
        if it.get("kind") == "diff" and it.get("parent_cg_id"):
            parent = by_id.get(it["parent_cg_id"])
            if not parent or parent.get("status") != "done":
                continue
        candidates.append(it)
        if len(candidates) >= max_new:
            break
    return candidates


def run_single_codex_cg(
    item: dict,
    codex_exe: Path,
    pack_dir: Path,
    raw_cache_dir: Path,
    log_dir: Path,
    empty_cwd: Path,
    art_style_authority: str,
    model_name: str,
    reasoning_effort: str,
    max_refs: int,
    stop_event: threading.Event,
) -> dict:
    aid = item["asset_id"]
    if stop_event.is_set():
        return {"asset_id": aid, "status": "skipped_after_stop"}

    refs = resolve_item_refs(item, pack_dir, raw_cache_dir, max_refs=max_refs)
    prompt = build_lean_prompt(item, refs, art_style_authority, pack_dir=pack_dir, for_openai_api=False)

    ref_list_str = ", ".join(f"[{i}] {r.name}" for i, r in enumerate(refs, 1))
    print(f"[REFS]  {aid}: {len(refs)} reference(s) -> {ref_list_str}", flush=True)

    cmd = [
        str(codex_exe),
        "exec",
        "-m",
        model_name,
        "-c",
        f'model_reasoning_effort="{reasoning_effort}"',
        "-c",
        "mcp_servers={}",
        "-c",
        "plugins={}",
        "--enable",
        "image_generation",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "-C",
        str(empty_cwd),
        "--json",
    ]
    for r in refs:
        cmd += ["-i", str(r)]
    cmd.append("-")

    t0 = time.time()
    print(f"[START] {aid} ({item.get('kind', 'base')}, model={model_name}/{reasoning_effort}, refs={len(refs)})", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin is not None
    proc.stdin.write(prompt)
    proc.stdin.close()

    lines: list[str] = []
    thread_id: str | None = None
    usage: dict = {}
    quota_hit = False
    quota_msg = ""

    assert proc.stdout is not None
    for raw_line in proc.stdout:
        s = raw_line.strip()
        if not s:
            continue
        lines.append(s)
        low = s.lower()
        if any(pat in low for pat in QUOTA_ERROR_PATTERNS):
            quota_hit = True
            quota_msg = s[:300]
            stop_event.set()
        if s.startswith("{"):
            try:
                ev = json.loads(s)
                etype = ev.get("type", "")
                if etype == "thread.started":
                    thread_id = ev.get("thread_id")
                elif etype == "turn.completed":
                    usage = ev.get("usage", {})
                elif etype == "error":
                    msg = json.dumps(ev)
                    if any(pat in msg.lower() for pat in QUOTA_ERROR_PATTERNS):
                        quota_hit = True
                        quota_msg = msg[:300]
                        stop_event.set()
            except Exception:
                pass

    rc = proc.wait()
    elapsed = round(time.time() - t0, 1)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{aid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    found_png: Path | None = None
    if thread_id:
        tdir = CODEX_GEN_ROOT / thread_id
        if tdir.is_dir():
            pngs = sorted(tdir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
            if pngs:
                found_png = pngs[0]

    if found_png is None:
        for ln in reversed(lines):
            clean = ln.replace("\\\\", "\\")
            for m in re.finditer(r'[A-Za-z]:\\[^"\'\r\n<>|]+?\.(?:png|jpg|jpeg|webp)', clean):
                cand = Path(m.group(0))
                if cand.is_file() and "generated_images" in str(cand):
                    found_png = cand
                    break
            if found_png:
                break

    if found_png is None:
        stop_event.set()
        return {
            "asset_id": aid,
            "status": "quota_or_error",
            "rc": rc,
            "elapsed_sec": elapsed,
            "quota_hit": quota_hit,
            "error": quota_msg or f"No generated image found (rc={rc}); check {log_dir / f'{aid}.jsonl'}",
            "usage": usage,
        }

    raw_cache_dir.mkdir(parents=True, exist_ok=True)
    raw_cache_path = raw_cache_dir / f"{aid}_raw.png"
    shutil.copy2(found_png, raw_cache_path)

    dst_jpg = pack_dir / item["target_file"]
    bias = float(item.get("crop_top_bias", 0.50 if item.get("kind") == "poster" else 0.38))
    sha_hex = normalize_to_baseline_jpeg(raw_cache_path, dst_jpg, (item["width"], item["height"]), top_bias=bias)

    usage_with_time = dict(usage)
    usage_with_time["elapsed_sec"] = elapsed
    print(
        f"[DONE]  {aid} in {elapsed}s | in={usage.get('input_tokens', 0)} "
        f"(cached={usage.get('cached_input_tokens', 0)}) out={usage.get('output_tokens', 0)} "
        f"reas={usage.get('reasoning_output_tokens', 0)} | sha={sha_hex[:12]}",
        flush=True,
    )
    return {
        "asset_id": aid,
        "status": "done",
        "sha256": sha_hex,
        "usage": usage_with_time,
        "dst_jpg": str(dst_jpg),
    }


def sync_clusters_if_present(pack_dir: Path) -> None:
    clusters_dir = pack_dir / "clusters"
    if clusters_dir.is_dir() and any(clusters_dir.glob("cluster_*_cg_plan.json")):
        with manifest_file_lock(pack_dir):
            cg_manifest_path = pack_dir / "cg_generation_manifest.json"
            need_merge = not cg_manifest_path.is_file()
            if not need_merge:
                try:
                    cg_data = json.loads(cg_manifest_path.read_text(encoding="utf-8"))
                    by_id = {it["asset_id"]: it for it in cg_data.get("items", []) if "asset_id" in it}
                    changed = False
                    plan_items = 0
                    for pf in sorted(clusters_dir.glob("cluster_*_cg_plan.json")):
                        pdata = json.loads(pf.read_text(encoding="utf-8"))
                        for pit in pdata.get("items", []):
                            plan_items += 1
                            aid = pit.get("asset_id")
                            if aid in by_id and by_id[aid].get("status") != "done":
                                mit = by_id[aid]
                                for k in ("reference_files", "characters_present", "scene_summary", "prompt"):
                                    if k in pit and mit.get(k) != pit[k]:
                                        mit[k] = pit[k]
                                        changed = True
                                if "prompt" in pit:
                                    for pk in ("generation_prompt", "prompt_brief"):
                                        if mit.get(pk) != pit["prompt"]:
                                            mit[pk] = pit["prompt"]
                                            changed = True
                    if len(by_id) < plan_items:
                        need_merge = True
                    elif changed:
                        cg_manifest_path.write_text(json.dumps(cg_data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
                except Exception:
                    need_merge = True
            if need_merge:
                from merge_arc_clusters import merge_package_clusters

                sources_dir = STUDIO_ROOT / "sources" / pack_dir.name
                merge_package_clusters(pack_dir, sources_dir if sources_dir.is_dir() else None)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Default Quota-Safe Codex CLI Batch CG Generator (gpt-6.1-terra high) for EagleLit Pack v1.0"
    )
    ap.add_argument("pack_dir", type=str, help="Path to package directory (e.g. packages/rezero_arc7_wn)")
    ap.add_argument("--max-new", type=int, default=10, help="Maximum new images to generate in this batch (default: 10)")
    ap.add_argument("--kinds", type=str, default="poster,banner,base,diff", help="Comma-separated allowed kinds")
    ap.add_argument("--workers", type=int, default=2, help="Max concurrent Codex processes (recommended: 2)")
    ap.add_argument("--only-ids", type=str, default="", help="Optional comma-separated asset_ids to regenerate/run")
    ap.add_argument(
        "--cluster-id",
        type=str,
        default="",
        help="Optional comma-separated cluster IDs to filter (e.g. cluster_01 or cluster_01,cluster_02)",
    )
    ap.add_argument(
        "--model",
        type=str,
        default="gpt-6.1-terra",
        help="Codex orchestrator model (default: gpt-6.1-terra)",
    )
    ap.add_argument(
        "--effort",
        type=str,
        default="high",
        help="Codex reasoning effort (default: high)",
    )
    ap.add_argument(
        "--max-refs",
        type=int,
        default=10,
        help="Maximum reference images to attach per CG item (default: 10)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify candidate items, reference image existence, and prompts without running Codex",
    )
    args = ap.parse_args()

    pack_dir = Path(args.pack_dir)
    if not pack_dir.is_absolute():
        pack_dir = STUDIO_ROOT / pack_dir

    sync_clusters_if_present(pack_dir)

    cg_manifest_path = pack_dir / "cg_generation_manifest.json"
    pack_manifest_path = pack_dir / "manifest.json"
    if not cg_manifest_path.is_file() or not pack_manifest_path.is_file():
        raise FileNotFoundError(f"Missing manifest.json or cg_generation_manifest.json in {pack_dir}")

    cg_manifest = json.loads(cg_manifest_path.read_text(encoding="utf-8"))
    pack_manifest = json.loads(pack_manifest_path.read_text(encoding="utf-8"))
    work_id = pack_manifest.get("work_id", pack_dir.name)
    art_style = pack_manifest.get(
        "art_style_authority",
        "Shinichirou Otsuka Light Novel Color Style & White Fox Anime Palette",
    )

    raw_cache_dir = STUDIO_ROOT / "ab_benchmark" / work_id / "codex_raw_cache"
    log_dir = STUDIO_ROOT / "ab_benchmark" / work_id / "batch_logs"

    allowed_kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    only_ids = {x.strip() for x in args.only_ids.split(",") if x.strip()}
    cluster_ids = {c.strip() for c in args.cluster_id.split(",") if c.strip()}

    candidates = select_batch_candidates(cg_manifest, allowed_kinds, only_ids, cluster_ids, args.max_new)
    if not candidates:
        print("No matching pending items to run.", flush=True)
        return

    print(
        f"=== Running Codex CLI Batch ({work_id}): {len(candidates)} item(s) | "
        f"model={args.model} ({args.effort}) | cluster={args.cluster_id or 'ALL'} | workers={args.workers} ===",
        flush=True,
    )
    for it in candidates:
        refs = resolve_item_refs(it, pack_dir, raw_cache_dir, max_refs=args.max_refs)
        print(
            f"  - {it['asset_id']} ({it.get('kind', 'base')}, cluster={it.get('cluster_id', '-')}, refs={len(refs)}) "
            f"-> {it['target_file']}",
            flush=True,
        )
        if args.dry_run:
            for r_idx, rp in enumerate(refs, 1):
                print(f"      Ref {r_idx}: {rp.relative_to(STUDIO_ROOT) if STUDIO_ROOT in rp.parents else rp}", flush=True)

    if args.dry_run:
        print("=== Dry Run Complete: 100% of candidate items and reference images verified on disk. ===", flush=True)
        return

    codex_exe = find_codex_exe()
    empty_cwd = Path(tempfile.gettempdir()) / f"codex_lean_cwd_{work_id}"
    empty_cwd.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()
    stop_event = threading.Event()
    results: list[dict] = []
    source_note = f"codex_{args.model.replace('-', '_').replace('.', '_')}_{args.effort}"

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        fut_map = {
            pool.submit(
                run_single_codex_cg,
                it,
                codex_exe,
                pack_dir,
                raw_cache_dir,
                log_dir,
                empty_cwd,
                art_style,
                args.model,
                args.effort,
                args.max_refs,
                stop_event,
            ): it
            for it in candidates
        }
        for fut in as_completed(fut_map):
            it = fut_map[fut]
            res = fut.result()
            results.append(res)
            if res.get("status") == "done":
                with lock:
                    record_done_item(
                        it,
                        res["sha256"],
                        cg_manifest,
                        pack_manifest,
                        cg_manifest_path,
                        pack_manifest_path,
                        usage=res.get("usage"),
                        source_note=source_note,
                    )
            elif res.get("status") == "quota_or_error":
                print(f"[HALT] {it['asset_id']} stopped: {res.get('error')}", flush=True)

    done_in_run = [r for r in results if r.get("status") == "done"]
    errs = [r for r in results if r.get("status") == "quota_or_error"]
    tot_in = sum(r.get("usage", {}).get("input_tokens", 0) for r in done_in_run)
    tot_cached = sum(r.get("usage", {}).get("cached_input_tokens", 0) for r in done_in_run)
    tot_out = sum(r.get("usage", {}).get("output_tokens", 0) for r in done_in_run)
    tot_reas = sum(r.get("usage", {}).get("reasoning_output_tokens", 0) for r in done_in_run)

    print(
        f"=== Run Summary ({work_id}): completed={len(done_in_run)}/{len(candidates)} | "
        f"pack_done={cg_manifest['summary']['done']}/{cg_manifest['summary']['total']} | "
        f"tokens_in={tot_in} (cached={tot_cached}) tokens_out={tot_out} reasoning={tot_reas} ===",
        flush=True,
    )
    if errs:
        sys.exit(2)


if __name__ == "__main__":
    main()
