#!/usr/bin/env python3
"""Cluster Merger & Master Manifest Synchronizer for Long-Form Multi-Cluster Packages.

Allows multiple parallel Cluster Worker conversations to build chapter JSONs
(`chapters/chXXX.json`) and cluster CG storyboards (`clusters/cluster_XX_cg_plan.json`)
independently without file write collisions, then deterministically merges all
available chapters and CG entries into `manifest.json` and `cg_generation_manifest.json`.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from cfont_sanitizer import count_words, sanitize_json_cfont


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_chapter_stats(ch_data: dict[str, Any]) -> dict[str, int]:
    scenes = ch_data.get("scenes", [])
    block_count = 0
    beat_count = 0
    book_words = 0
    vn_words = 0
    for sc in scenes:
        blocks = sc.get("blocks", [])
        block_count += len(blocks)
        for blk in blocks:
            if blk.get("kind") == "scene_break":
                continue
            bt = blk.get("book_text", "")
            book_words += count_words(bt)
            beats = blk.get("vn_beats", [])
            beat_count += len(beats)
            for b in beats:
                combined = f"{b.get('text', '')} {b.get('inner_thought', '')}".strip()
                vn_words += count_words(combined)
    return {
        "scene_count": len(scenes),
        "block_count": block_count,
        "beat_count": beat_count,
        "word_count": book_words,
        "vn_word_count": vn_words,
    }


def merge_package_clusters(pack_dir: Path, raw_sources_dir: Path | None = None) -> dict[str, Any]:
    manifest_path = pack_dir / "manifest.json"
    cg_manifest_path = pack_dir / "cg_generation_manifest.json"
    chapters_dir = pack_dir / "chapters"
    clusters_dir = pack_dir / "clusters"

    if not manifest_path.exists():
        raise FileNotFoundError(f"Base manifest.json not found at {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cg_manifest = (
        json.loads(cg_manifest_path.read_text(encoding="utf-8"))
        if cg_manifest_path.exists()
        else {
            "work_id": manifest.get("work_id", pack_dir.name),
            "art_style_authority": manifest.get("art_style_authority", ""),
            "quota_policy": "antigravity_internal_image_generator_only_100_per_5h",
            "auto_start_generation": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"total": 0, "done": 0, "pending": 0},
            "items": [],
        }
    )

    # 1. Merge CG plans from clusters/cluster_*_cg_plan.json
    existing_items_by_id: dict[str, dict[str, Any]] = {
        item["asset_id"]: item for item in cg_manifest.get("items", []) if "asset_id" in item
    }
    cg_catalog: dict[str, dict[str, Any]] = dict(manifest.get("cg_catalog", {}))

    if clusters_dir.exists():
        for plan_file in sorted(clusters_dir.glob("cluster_*_cg_plan.json")):
            plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
            plan_cluster_id = plan_data.get("cluster_id") or plan_file.stem.replace("_cg_plan", "")
            for item in plan_data.get("items", []):
                aid = item["asset_id"]
                prev = existing_items_by_id.get(aid, {})
                item["cluster_id"] = item.get("cluster_id") or prev.get("cluster_id") or plan_cluster_id
                # Ensure prompt fields are uniformly populated across all cluster schemas
                unified_prompt = (
                    item.get("generation_prompt")
                    or item.get("prompt")
                    or item.get("prompt_brief")
                    or prev.get("generation_prompt")
                    or prev.get("prompt")
                    or ""
                )
                item["generation_prompt"] = unified_prompt
                item["prompt"] = item.get("prompt") or unified_prompt
                item["prompt_brief"] = item.get("prompt_brief") or unified_prompt

                # Preserve 'done' status and sha256 if already rendered on disk
                target_rel = item.get("target_file", f"cgs/{aid}.jpg")
                target_abs = pack_dir / target_rel
                if target_abs.exists() and target_abs.stat().st_size > 1024:
                    digest = sha256_file(target_abs)
                    item["status"] = "done"
                    item["sha256"] = digest
                    item["completed_at"] = prev.get("completed_at") or datetime.now(timezone.utc).isoformat()
                else:
                    item["status"] = prev.get("status", item.get("status", "pending"))
                    item["sha256"] = prev.get("sha256")
                    item["completed_at"] = prev.get("completed_at")

                existing_items_by_id[aid] = item

                if aid not in ("poster", "banner_16x9"):
                    cg_catalog[aid] = {
                        "file": target_rel,
                        "width": item.get("width", 1376),
                        "height": item.get("height", 768),
                        "sha256": item.get("sha256") or ("0" * 64),
                        "kind": item.get("kind", "base"),
                        "parent_cg_id": item.get("parent_cg_id"),
                        "focal_point": item.get("focal_point", [0.5, 0.42]),
                        "characters_present": item.get("characters_present", []),
                        "scene_summary": item.get("scene_summary", item.get("prompt_brief", "")[:180]),
                    }

    # Check poster & banner_16x9 status on disk
    for cover_id, rel_name in [("poster", "poster.jpg"), ("banner_16x9", "banner_16x9.jpg")]:
        if cover_id in existing_items_by_id:
            p_abs = pack_dir / rel_name
            if p_abs.exists() and p_abs.stat().st_size > 1024:
                existing_items_by_id[cover_id]["status"] = "done"
                existing_items_by_id[cover_id]["sha256"] = sha256_file(p_abs)

    # Sort items: poster, banner_16x9, then alphabetical by asset_id
    def _sort_key(it: dict[str, Any]) -> tuple[int, str]:
        aid = it.get("asset_id", "")
        if aid == "poster":
            return (0, aid)
        if aid == "banner_16x9":
            return (1, aid)
        return (2, aid)

    sorted_items = sorted(existing_items_by_id.values(), key=_sort_key)
    done_cnt = sum(1 for it in sorted_items if it.get("status") == "done")
    pend_cnt = len(sorted_items) - done_cnt

    cg_manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    cg_manifest["summary"] = {
        "total": len(sorted_items),
        "done": done_cnt,
        "pending": pend_cnt,
    }
    cg_manifest["items"] = sorted_items
    manifest["cg_catalog"] = cg_catalog

    # 2. Scan chapters/ch*.json and rebuild manifest["chapters"]
    chapter_entries = []
    total_book_words = 0
    total_vn_words = 0
    raw_Matched_words = 0

    if chapters_dir.exists():
        ch_files = sorted(chapters_dir.glob("ch*.json"))
        for idx, ch_file in enumerate(ch_files, 1):
            ch_data = json.loads(ch_file.read_text(encoding="utf-8"))
            # Ensure sequential index 1..N across currently built chapters
            ch_data["index"] = idx
            ch_data = sanitize_json_cfont(ch_data)
            ch_file.write_text(json.dumps(ch_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

            stats = compute_chapter_stats(ch_data)
            total_book_words += stats["word_count"]
            total_vn_words += stats["vn_word_count"]

            # Look up corresponding raw chapter word count if available
            ch_id = ch_data.get("chapter_id", ch_file.stem)
            if raw_sources_dir is not None:
                raw_txt_path = raw_sources_dir / "raw_chapters" / f"{ch_id}_raw.txt"
                if raw_txt_path.exists():
                    raw_Matched_words += count_words(raw_txt_path.read_text(encoding="utf-8"))

            chapter_entries.append({
                "chapter_id": ch_id,
                "index": idx,
                "title": ch_data.get("title", ch_id),
                "file": f"chapters/{ch_file.name}",
                "scene_count": stats["scene_count"],
                "block_count": stats["block_count"],
                "beat_count": stats["beat_count"],
                "word_count": stats["word_count"],
            })

    manifest["chapters"] = chapter_entries
    if total_book_words > 0:
        raw_total = raw_Matched_words if raw_Matched_words > 0 else total_book_words
        manifest["source_provenance"]["raw_word_count"] = raw_total
        manifest["source_provenance"]["pack_book_word_count"] = total_book_words
        manifest["source_provenance"]["pack_vn_word_count"] = total_vn_words
        manifest["source_provenance"]["verbatim_coverage_ratio"] = round(
            min(1.0, total_book_words / max(1, raw_total)), 4
        )

    manifest = sanitize_json_cfont(manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    cg_manifest_path.write_text(json.dumps(cg_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {
        "chapters_merged": len(chapter_entries),
        "total_book_words": total_book_words,
        "total_vn_words": total_vn_words,
        "cg_total": len(sorted_items),
        "cg_done": done_cnt,
        "cg_pending": pend_cnt,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge cluster outputs into master package manifest.")
    parser.add_argument("pack_dir", nargs="?", type=Path, default=None, help="Path to packages/<work_id>")
    parser.add_argument("--package-dir", "--pack-dir", dest="package_dir_opt", type=Path, default=None, help="Path to packages/<work_id>")
    parser.add_argument("--sources-dir", "--raw-sources-dir", dest="sources_dir", type=Path, default=None, help="Path to sources/<work_id>")
    args = parser.parse_args()

    target_pack = args.package_dir_opt or args.pack_dir
    if target_pack is None:
        parser.error("Must provide pack_dir either positionally or via --package-dir.")
    pack_dir = target_pack.resolve()
    sources_dir = args.sources_dir.resolve() if args.sources_dir else (pack_dir.parent.parent / "sources" / pack_dir.name)
    res = merge_package_clusters(pack_dir, sources_dir if sources_dir.exists() else None)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
