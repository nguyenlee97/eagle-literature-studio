#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy_literature_pack.py — Gatekeeper CLI & Atomic Integrator for Eagle Literature Packs
=========================================================================================
Integrates certified `eagle_lit_v1` Visual Novel (`cinema`) & Light Novel (`library`) packages
from `D:\\Eagle Literature Studio\\packages\\` into `Project Eagle\\gta sa gameplay-lab`.

Supported subcommands:
  - `list`     : List all Studio packages, certification status, and runtime deployment status.
  - `verify`   : Run Gatekeeper pre-flight checks against `.cert.json` (`READY_FOR_INTEGRATION`)
                 and validate schema, chapters, 16:9 CGs, 2:3 posters, and BGM files.
  - `stage`    : Build the 30-venue `schedule_and_catalog.json`, Waifu discussion hooks, and
                 staging manifest in `D:\\GTA Archive\\Staging\\project-eagle\\lit-pack-integrator\\`.
  - `apply`    : Under `EagleCoordLock("game-write")`, create zero-footprint NTFS junctions in
                 `data/project-eagle/literature/packs/<work_id>`, deploy `schedule_and_catalog.json`,
                 record before/after SHA-256 hashes, and generate a guarded rollback script.
  - `remove`   : Remove a specific `<work_id>` junction and catalog entry under `game-write.lock`.
  - `rollback` : Restore pre-integration files and remove deployed junctions under `game-write.lock`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

TOOLS_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from eagle_coord_lock import EagleCoordLock

STUDIO_PACKAGES_DIR = Path(r"D:\Eagle Literature Studio\packages")
CERT_DIR = WORKSPACE_ROOT / "data" / "project-eagle" / "literature" / "certifications"
GAME_ROOT = WORKSPACE_ROOT / "Project Eagle" / "gta sa gameplay-lab"
GAME_LIT_DIR = GAME_ROOT / "data" / "project-eagle" / "literature"
GAME_PACKS_DIR = GAME_LIT_DIR / "packs"
GAME_CATALOG_PATH = GAME_LIT_DIR / "schedule_and_catalog.json"
GAME_ENGINE_PATH = GAME_ROOT / "eagle_literature_engine.py"
GAME_HUD_LUA_PATH = GAME_ROOT / "cleo" / "EagleLiteratureHud.lua"

STAGING_DIR = Path(r"D:\GTA Archive\Staging\project-eagle\lit-pack-integrator")
BACKUP_DIR = STAGING_DIR / "backup_before_apply"
STAGED_CATALOG_PATH = STAGING_DIR / "schedule_and_catalog.json"
DEPLOYMENT_MANIFEST_PATH = STAGING_DIR / "deployment_manifest.json"
ROLLBACK_SCRIPT_PATH = STAGING_DIR / "rollback_lit_packs_20261108.py"

DEFAULT_CHAT_ID = "a68843e2-d192-4fdd-8d1a-076fde2236c6"
SHOWTIMES_5 = ["09:00", "13:00", "17:00", "20:30", "23:30"]


# ============================================================================
# HASH & JUNCTION HELPERS
# ============================================================================
def sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_reparse_or_junction(path: Path) -> bool:
    try:
        st = os.lstat(path)
        return bool(getattr(st, "st_file_attributes", 0) & 0x400) or path.is_symlink()
    except OSError:
        return False


def create_directory_junction(link_path: Path, target_path: Path) -> None:
    if link_path.exists() or is_reparse_or_junction(link_path):
        if is_reparse_or_junction(link_path):
            subprocess.run(["cmd", "/c", "rmdir", str(link_path)], check=True, capture_output=True)
        elif link_path.is_dir():
            shutil.rmtree(link_path)
        else:
            link_path.unlink()
    link_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        check=True,
        capture_output=True,
        text=True,
    )


def remove_directory_junction(link_path: Path) -> bool:
    if not link_path.exists() and not is_reparse_or_junction(link_path):
        return False
    if is_reparse_or_junction(link_path):
        subprocess.run(["cmd", "/c", "rmdir", str(link_path)], check=True, capture_output=True)
        return True
    return False


# ============================================================================
# GATEKEEPER CERTIFICATION & PACKAGE VERIFICATION
# ============================================================================
def load_certifications() -> dict[str, dict[str, Any]]:
    certs: dict[str, dict[str, Any]] = {}
    if not CERT_DIR.is_dir():
        return certs
    for cf in sorted(CERT_DIR.glob("*.cert.json")):
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
            wid = str(data.get("work_id") or cf.stem.replace(".cert", "")).strip()
            certs[wid] = data
        except Exception:
            pass
    return certs


def verify_single_pack(work_id: str, cert: dict[str, Any] | None) -> dict[str, Any]:
    pack_dir = STUDIO_PACKAGES_DIR / work_id
    result: dict[str, Any] = {
        "work_id": work_id,
        "pack_dir": str(pack_dir),
        "certified": False,
        "verdict": cert.get("verdict") if cert else "UNCERTIFIED_WIP",
        "ready": False,
        "errors": [],
        "metrics": {},
    }
    if cert is None:
        result["errors"].append(f"Missing certification file: {CERT_DIR / f'{work_id}.cert.json'}")
        return result

    if cert.get("verdict") != "READY_FOR_INTEGRATION":
        result["errors"].append(f"Certification verdict is {cert.get('verdict')!r} (expected 'READY_FOR_INTEGRATION')")
        return result

    gates = cert.get("gate_results") or {}
    for g_key in ("schema_ok", "cfont_ok", "raster_txd_ok", "mci_audio_ok", "visual_hud_ok"):
        if not gates.get(g_key):
            result["errors"].append(f"Gate {g_key} is not true in certification")

    result["certified"] = len(result["errors"]) == 0
    if not pack_dir.is_dir():
        result["errors"].append(f"Package directory missing: {pack_dir}")
        return result

    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        result["errors"].append("Missing manifest.json")
        return result

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "eagle_lit_v1":
        result["errors"].append(f"Invalid schema_version: {manifest.get('schema_version')!r}")

    # Verify cover assets (poster 832x1248 2:3 & banner 1376x768 16:9)
    poster_p = pack_dir / "poster.jpg"
    banner_p = pack_dir / "banner_16x9.jpg"
    if not poster_p.is_file():
        result["errors"].append("Missing poster.jpg")
    else:
        with Image.open(poster_p) as im:
            if im.size != (832, 1248) or im.mode != "RGB":
                result["errors"].append(f"poster.jpg invalid size/mode: {im.size} {im.mode}")
    if not banner_p.is_file():
        result["errors"].append("Missing banner_16x9.jpg")
    else:
        with Image.open(banner_p) as im:
            if im.size != (1376, 768) or im.mode != "RGB":
                result["errors"].append(f"banner_16x9.jpg invalid size/mode: {im.size} {im.mode}")

    cg_cat = manifest.get("cg_catalog") or {}
    for cg_id, cg_meta in cg_cat.items():
        cp = pack_dir / str(cg_meta.get("file") or "")
        if not cp.is_file():
            result["errors"].append(f"Missing CG {cg_id}: {cp.name}")

    bgm_cat = manifest.get("bgm_catalog") or {}
    for bgm_id, bgm_meta in bgm_cat.items():
        bp = pack_dir / str(bgm_meta.get("file") or "")
        if not bp.is_file():
            result["errors"].append(f"Missing BGM {bgm_id}: {bp.name}")

    chapters = manifest.get("chapters") or []
    total_blocks = 0
    total_prose_blocks = 0
    total_beats = 0
    for ch_meta in chapters:
        ch_p = pack_dir / str(ch_meta.get("file") or "")
        if not ch_p.is_file():
            result["errors"].append(f"Missing chapter file: {ch_meta.get('file')}")
            continue
        ch_data = json.loads(ch_p.read_text(encoding="utf-8"))
        for sc in ch_data.get("scenes") or []:
            for blk in sc.get("blocks") or []:
                total_blocks += 1
                if str(blk.get("kind") or "").lower() == "scene_break":
                    continue
                total_prose_blocks += 1
                total_beats += len(blk.get("vn_beats") or [])

    result["metrics"] = {
        "title": manifest.get("title"),
        "short_title": manifest.get("short_title") or manifest.get("title"),
        "venue_type": manifest.get("venue_type"),
        "default_read_mode": manifest.get("default_read_mode"),
        "chapters": len(chapters),
        "cgs": len(cg_cat),
        "bgms": len(bgm_cat),
        "blocks_total": total_blocks,
        "blocks_prose": total_prose_blocks,
        "vn_beats": total_beats,
        "words": (cert.get("metrics_summary") or {}).get("word_count", 0),
    }
    result["ready"] = len(result["errors"]) == 0
    return result


# ============================================================================
# 30-VENUE SCHEDULE & LIBRARY SHELF CATALOG GENERATOR
# ============================================================================
WAIFU_DISCUSSION_HOOKS: dict[str, dict[str, Any]] = {
    "rezero_ayamatsu_pride_if": {
        "affection_bonus": 25,
        "theme_tag": "Obsessive Devotion & Crimson Fire",
        "waifu_lines": {
            "hutao": "Burning down the whole kingdom after eighty-eight butterfly loops... Now that is one stubborn soul who refused to enter Wangsheng Parlour until his mission was done!",
            "furina": "What a tragic, overwhelming grand finale! Playing the villain on the world's stage so the silver-haired heroine could take the crown... I nearly cried into my popcorn!",
            "citlali": "Eighty-eight loops against the Sword Saint just from sheer spite?! That boy's soul is crazier than any ancient Natlan scroll I have ever read.",
            "columbina": "A quiet, ash-covered lullaby at the end of a burning world... His devotion sang a very pale, sorrowful melody.",
            "kirara": "Meow... Elsa and Meili were terrifying, and watching the whole capital catch fire gave me goosebumps! Let's hold hands on the walk back!",
            "vesna": "He turned himself into the Sin Archbishop of Pride so she would stand in the light. A ruthless winter vow, carried to the final breath.",
        },
    },
    "rezero_rem_sloth_if": {
        "affection_bonus": 30,
        "theme_tag": "Warm Kararagi Domestic Life & Family",
        "waifu_lines": {
            "hutao": "Running away to Kararagi, raising Rigel and Spica, and befriending a wolf shinobi! See, peaceful everyday life with the one you love is the greatest treasure!",
            "furina": "Oh, Rem's long hair and their little family banter in Banan were so heartwarming! That rooftop vow under the full moon was pure romance!",
            "citlali": "Hmph, a domestic slice-of-life novel where the hero actually chooses peace and family... Don't look at me like that, I liked Rigel and Spica's chapters!",
            "columbina": "Gentle wind chimes over wooden verandas... A serene dream where sorrow was traded for warm tea and lullabies.",
            "kirara": "Kararagi's wooden streets look just like Inazuma! And Halibel is so fluffy—let's get snacks and talk about Rem's cooking!",
            "vesna": "Choosing a quiet home over a throne of blood takes its own kind of courage. Their rooftop promise under the moonlight was unforgettable.",
        },
    },
    "rezero_kasaneru_greed_if": {
        "affection_bonus": 25,
        "theme_tag": "One Hundred Million Loops & The Witch's Tea Party",
        "waifu_lines": {
            "hutao": "Over a hundred million deaths just to predict the weather and save everyone without a scratch?! Even the border of life and death would wear out!",
            "furina": "Practicing a fake smile in the mirror every morning while carrying a hundred million memories... The contrast between his perfect victory and hollow heart was chilling!",
            "citlali": "That Witch of Greed Echidna drank tea while he traded away his humanity! Never make a contract like that, Traveler—promise me right now!",
            "columbina": "A flawless clockwork paradise where nobody weeps, except the boy who forgot how to feel the rain.",
            "kirara": "Seeing Emilia cut her hair and everyone safe in the mansion felt so eerie once we knew how many times Subaru reset the day...",
            "vesna": "Victory without warmth is colder than a Snezhnayan blizzard. Never hide your scars behind a rehearsed smile when you are with me.",
        },
    },
    "rezero_butterfly_lust_if": {
        "affection_bonus": 25,
        "theme_tag": "Royal Lugunica Palace Comedy & Golden Reverie",
        "waifu_lines": {
            "hutao": "Hahaha! Marrying all the Royal Selection candidates at once and running down the palace carpet?! What a chaotic butterfly dream!",
            "furina": "A royal court overflowing with cakes, tea parties, and seven queens?! The costume design in the Lugunica Sunlit Conservatory was dazzling!",
            "citlali": "Wait—that whole royal harem was a cheeky April Fool's butterfly dream?! I knew it was too ridiculous to be canon, though Felt and Beatrice were hilarious!",
            "columbina": "A fleeting golden butterfly fluttering through a sunlit palace... Sweet, foolish, and gone upon waking.",
            "kirara": "Every queen had such gorgeous royal dresses! Which crown gown did you like best, Traveler?",
            "vesna": "Even in a lighthearted palace dream, seeing everyone laugh around the same banquet table had a gentle charm.",
        },
    },
    "rezero_oboreru_wrath_if": {
        "affection_bonus": 25,
        "theme_tag": "The Purge King of Pandemonium & The Flipped Coin",
        "waifu_lines": {
            "hutao": "Tossing a gold coin to decide life or death inside a monochrome fortress... The Purge King was drowning in fear until the only color left was Emilia and Ram!",
            "furina": "That duel between Cecilus the Blue Lightning and Reinhard outside Pandemonium was breathtaking IMAX cinema! And the monochrome-to-color visual motif was genius!",
            "citlali": "Building a crime syndicate with the strongest swordsman of Vollachia and the Admirer of Kararagi because he couldn't trust anyone... What a haunting tragedy.",
            "columbina": "In a world drained of all color by suspicion, only two voices remained painted in true light before the end.",
            "kirara": "Pandemonium castle was so dark and tense! When Halibel and Cecilus guarded the halls, you could feel the pressure through the screen!",
            "vesna": "Fear builds higher walls than stone. Yet in the snow-white sanctuary at the end, he finally laid down his coin.",
        },
    },
    "rezero_tsugihagu_gluttony_if": {
        "affection_bonus": 25,
        "theme_tag": "Taygeta Library of the Dead & Snow-White Hair",
        "waifu_lines": {
            "hutao": "Reading the Books of the Dead in Taygeta Library to piece together the friend he forgot... Memory and the afterlife are never meant to be stitched by force.",
            "furina": "The translucent memory phantoms whispering around white-haired Subaru and Shaula in the desert watchtower... It was eerie, poetic, and heartbreaking!",
            "citlali": "The Pleiades Watchtower and Taygeta Library hold the memories of every departed soul. Watching him chase "'Natsuki Subaru'" through those pages gave me chills.",
            "columbina": "Phantoms of forgotten friends applauding in the sandstorm... He became a patchwork of voices that loved him.",
            "kirara": "Shaula was so loyal following him across the Auguria Dunes! That white fox scarf and his bleached white hair looked striking on the big screen!",
            "vesna": "Even when amnesia erased his name, the echoes of his comrades guided his blade through the watchtower.",
        },
    },
    "rezero_arc7_wn": {
        "affection_bonus": 35,
        "theme_tag": "The Land of Wolves — 116-Chapter Vollachia Empire Saga",
        "waifu_lines": {
            "hutao": "One hundred and sixteen chapters across the Vollachia Empire! From Buddheim Jungle with Rem and Louis to the siege of Lupgana—what an epic adventure!",
            "furina": "From the Bloodless Siege of Guaral to Gladiator Island Ginunhive and Emperor Vincent Vollachia's throne room! Having 474 full-color illustrations makes every chapter come alive!",
            "citlali": "Now THIS is a proper flagship Light Novel! 774,000 words of imperial strategy, Divine Generals, and Natsumi Schwartz's absurd schemes—I'm buying a permanent copy for my EaglePhone!",
            "columbina": "Through burning jungles, gladiator tides, and the Great Disaster of the Imperial Capital, the star-named boy defied the Sword Wolves' creed.",
            "kirara": "The Shudraq People in the jungle and Flop & Medium O'Connell were so lively! Let's bookmark our chapter at the reading table and grab an espresso!",
            "vesna": "The law of the Vollachia Empire—that the strong devour the weak—broken by a boy who refuses to abandon a single comrade on Gladiator Island. A masterpiece.",
        },
    },
}


def build_schedule_and_catalog(certified_work_ids: list[str]) -> dict[str, Any]:
    """
    Constructs the complete 30-venue `schedule_and_catalog.json` covering:
      - 6 Re:Zero IF Visual Novel Movies across all 15 Cinemas (`CIN-01..15`)
        * 6 Grand Cinemas (`CIN-01..04, 11, 12`): $25 ticket, $10 combo, all 6 IF movies (plus special Arc 7 screening at CIN-03)
        * 9 Small Cinemas (`CIN-05..10, 13..15`): $10-$12 ticket, $6 combo, 3 curated IF movies per regional theme
      - Re:Zero Arc 7 (116 Chapters) as Featured Flagship Book across all 15 Libraries (`LIB-01..15`),
        plus Dual-Mode `nvl_book` editions of all 6 IF routes at `LIB-01..04, 06, 11, 12` and regional IF shelves at small bookstores.
    """
    all_6_ifs = [
        "rezero_ayamatsu_pride_if",
        "rezero_rem_sloth_if",
        "rezero_kasaneru_greed_if",
        "rezero_oboreru_wrath_if",
        "rezero_tsugihagu_gluttony_if",
        "rezero_butterfly_lust_if",
    ]

    cinema_schedules: dict[str, dict[str, Any]] = {
        "CIN-01": {
            "venue_id": "CIN-01",
            "name_vi": "Đại Rạp Chiếu Phim Cathay Vinewood",
            "name_en": "Vinewood Cathay Grand Theatre",
            "tier": "grand",
            "max_weekly_featured": 7,
            "ticket_price_usd": 25,
            "combo_price_usd": 10,
            "featured_works": [
                "rezero_ayamatsu_pride_if",
                "rezero_rem_sloth_if",
                "rezero_kasaneru_greed_if",
                "rezero_oboreru_wrath_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-02": {
            "venue_id": "CIN-02",
            "name_vi": "Tổ Hợp Rạp IMAX Đại Lộ Hoa Anh Đào",
            "name_en": "Sakura Multiplex IMAX",
            "tier": "grand",
            "max_weekly_featured": 7,
            "ticket_price_usd": 25,
            "combo_price_usd": 10,
            "featured_works": [
                "rezero_rem_sloth_if",
                "rezero_ayamatsu_pride_if",
                "rezero_kasaneru_greed_if",
                "rezero_butterfly_lust_if",
                "rezero_oboreru_wrath_if",
                "rezero_tsugihagu_gluttony_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-03": {
            "venue_id": "CIN-03",
            "name_vi": "Đại Rạp Anime TOHO Akihabara",
            "name_en": "Akihabara TOHO Anime Cinema",
            "tier": "grand",
            "max_weekly_featured": 8,
            "ticket_price_usd": 25,
            "combo_price_usd": 10,
            "featured_works": [
                "rezero_ayamatsu_pride_if",
                "rezero_rem_sloth_if",
                "rezero_kasaneru_greed_if",
                "rezero_oboreru_wrath_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_butterfly_lust_if",
                "rezero_arc7_wn",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-04": {
            "venue_id": "CIN-04",
            "name_vi": "Nhà Hát Điện Ảnh Cổ Điển The Loop",
            "name_en": "The Loop Historic Grand Theatre",
            "tier": "grand",
            "max_weekly_featured": 7,
            "ticket_price_usd": 25,
            "combo_price_usd": 10,
            "featured_works": [
                "rezero_kasaneru_greed_if",
                "rezero_oboreru_wrath_if",
                "rezero_ayamatsu_pride_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_rem_sloth_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-05": {
            "venue_id": "CIN-05",
            "name_vi": "Rạp Phim Bình Dân Cinemark Đông LS",
            "name_en": "East Los Santos Cinemark",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 12,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_oboreru_wrath_if",
                "rezero_rem_sloth_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-06": {
            "venue_id": "CIN-06",
            "name_vi": "Rạp Phim Nghệ Thuật Queens San Fierro",
            "name_en": "Queens Castro Art-House Cinema",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 12,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_kasaneru_greed_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_ayamatsu_pride_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-07": {
            "venue_id": "CIN-07",
            "name_vi": "Rạp Đèn Lồng Phố Hoa Las Venturas",
            "name_en": "Chinatown Lantern Indie Cinema",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 12,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_rem_sloth_if",
                "rezero_oboreru_wrath_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-08": {
            "venue_id": "CIN-08",
            "name_vi": "Khán Phòng Điện Ảnh & Opera Staunton",
            "name_en": "Staunton Opera & Indie Film Hall",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 12,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_ayamatsu_pride_if",
                "rezero_kasaneru_greed_if",
                "rezero_tsugihagu_gluttony_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-09": {
            "venue_id": "CIN-09",
            "name_vi": "Rạp Chiếu Bóng Cổ Điển Thị Trấn Bullworth",
            "name_en": "Bullworth Classic Town Theatre",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 10,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_ayamatsu_pride_if",
                "rezero_kasaneru_greed_if",
                "rezero_rem_sloth_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-10": {
            "venue_id": "CIN-10",
            "name_vi": "Rạp Chiếu Phim Cổ Điển Thành Phố Rose City",
            "name_en": "Rose City Retro Cinema",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 10,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_rem_sloth_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_ayamatsu_pride_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-11": {
            "venue_id": "CIN-11",
            "name_vi": "Đại Tổ Hợp Điện Ảnh Trung Tâm Đồng Bằng Wichita",
            "name_en": "Wichita Heartland Grand Multiplex",
            "tier": "grand",
            "max_weekly_featured": 7,
            "ticket_price_usd": 25,
            "combo_price_usd": 10,
            "featured_works": [
                "rezero_tsugihagu_gluttony_if",
                "rezero_ayamatsu_pride_if",
                "rezero_rem_sloth_if",
                "rezero_kasaneru_greed_if",
                "rezero_oboreru_wrath_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-12": {
            "venue_id": "CIN-12",
            "name_vi": "Đại Rạp Điện Ảnh Miền Nam Cottonmouth Movie Haus",
            "name_en": "Cottonmouth Bayou Grand Movie Haus",
            "tier": "grand",
            "max_weekly_featured": 7,
            "ticket_price_usd": 25,
            "combo_price_usd": 10,
            "featured_works": [
                "rezero_oboreru_wrath_if",
                "rezero_ayamatsu_pride_if",
                "rezero_rem_sloth_if",
                "rezero_kasaneru_greed_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-13": {
            "venue_id": "CIN-13",
            "name_vi": "Rạp Chiếu Phim Ánh Sao Quốc Lộ Cordillera Colorado",
            "name_en": "Cordillera Route 66 Starlight Cinema",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 10,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_rem_sloth_if",
                "rezero_butterfly_lust_if",
                "rezero_ayamatsu_pride_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-14": {
            "venue_id": "CIN-14",
            "name_vi": "Rạp Phim Nghệ Thuật Đô Thị Peachtree",
            "name_en": "Peachtree Midtown Boutique Cinema",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 12,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_kasaneru_greed_if",
                "rezero_tsugihagu_gluttony_if",
                "rezero_rem_sloth_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
        "CIN-15": {
            "venue_id": "CIN-15",
            "name_vi": "Khán Phòng Chiếu Phim Cổ Điển Independence",
            "name_en": "Independence Liberty Screening Hall",
            "tier": "small",
            "max_weekly_featured": 4,
            "ticket_price_usd": 12,
            "combo_price_usd": 6,
            "featured_works": [
                "rezero_oboreru_wrath_if",
                "rezero_ayamatsu_pride_if",
                "rezero_kasaneru_greed_if",
                "_smoke_test_demo",
            ],
            "showtimes": SHOWTIMES_5,
        },
    }

    full_library_lineup = ["rezero_arc7_wn"] + all_6_ifs + ["_smoke_test_demo"]

    library_shelves: dict[str, dict[str, Any]] = {
        "LIB-01": {
            "venue_id": "LIB-01",
            "name_vi": "Thư Viện Trung Tâm Thành Phố Los Santos",
            "name_en": "Los Santos Central Public Library",
            "tier": "grand",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 8,
            "buy_default_usd": 45,
            "coffee_combo_usd": 5,
            "shelf_works": full_library_lineup,
        },
        "LIB-02": {
            "venue_id": "LIB-02",
            "name_vi": "Đại Siêu Thị Light Novel 8 Tầng Kinokuniya",
            "name_en": "Kinokuniya 8F Light Novel Megastore",
            "tier": "grand",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 8,
            "buy_default_usd": 45,
            "coffee_combo_usd": 5,
            "shelf_works": full_library_lineup,
        },
        "LIB-03": {
            "venue_id": "LIB-03",
            "name_vi": "Đại Thư Viện Quảng Trường Tòa Thị Chính SF",
            "name_en": "San Fierro Civic Center Grand Library",
            "tier": "grand",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 8,
            "buy_default_usd": 45,
            "coffee_combo_usd": 5,
            "shelf_works": full_library_lineup,
        },
        "LIB-04": {
            "venue_id": "LIB-04",
            "name_vi": "Viện Văn Học & Đại Thư Viện Đảo Staunton",
            "name_en": "Staunton Grand Athenaeum & Library",
            "tier": "grand",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 8,
            "buy_default_usd": 45,
            "coffee_combo_usd": 5,
            "shelf_works": full_library_lineup,
        },
        "LIB-05": {
            "venue_id": "LIB-05",
            "name_vi": "Tiệm Sách Light Novel & Cafe Học Viện Sakura",
            "name_en": "Sakura High Light Novel & Manga Cafe",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 5,
            "buy_default_usd": 35,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_rem_sloth_if",
                "rezero_butterfly_lust_if",
                "rezero_ayamatsu_pride_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-06": {
            "venue_id": "LIB-06",
            "name_vi": "Thư Các Yae & Tây Phong Bờ Nam Hồ Cider",
            "name_en": "Cider Lake Yae & Favonius Book Pavilion",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 5,
            "buy_default_usd": 35,
            "coffee_combo_usd": 4,
            "shelf_works": full_library_lineup,
        },
        "LIB-07": {
            "venue_id": "LIB-07",
            "name_vi": "Tiệm Sách & Cafe Art-Deco Đại Lộ Ocean Drive",
            "name_en": "Ocean Drive Art-Deco Book & News Cafe",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 5,
            "buy_default_usd": 35,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_butterfly_lust_if",
                "rezero_rem_sloth_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-08": {
            "venue_id": "LIB-08",
            "name_vi": "Tiệm Truyện & Light Novel Dragon's Wing",
            "name_en": "Dragon's Wing Books & Light Novels",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 5,
            "buy_default_usd": 30,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_ayamatsu_pride_if",
                "rezero_kasaneru_greed_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-09": {
            "venue_id": "LIB-09",
            "name_vi": "Phòng Đọc Văn Hóa Đại Lộ Michigan",
            "name_en": "Michigan Ave Cultural Reading Hall",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 5,
            "buy_default_usd": 35,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_kasaneru_greed_if",
                "rezero_tsugihagu_gluttony_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-10": {
            "venue_id": "LIB-10",
            "name_vi": "Thư Viện Thị Trấn Palomino Creek",
            "name_en": "Palomino Creek Town Library",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 4,
            "buy_default_usd": 30,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_rem_sloth_if",
                "rezero_ayamatsu_pride_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-11": {
            "venue_id": "LIB-11",
            "name_vi": "Đại Thư Viện & Lưu Trữ Trung Tâm Anywhere City",
            "name_en": "Anywhere City Central Archive & Library",
            "tier": "grand",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 8,
            "buy_default_usd": 45,
            "coffee_combo_usd": 5,
            "shelf_works": full_library_lineup,
        },
        "LIB-12": {
            "venue_id": "LIB-12",
            "name_vi": "Đại Thư Viện & Bảo Tàng Phố Cổ Carcer City",
            "name_en": "Carcer Old Town Museum & Grand Library",
            "tier": "grand",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 8,
            "buy_default_usd": 45,
            "coffee_combo_usd": 5,
            "shelf_works": full_library_lineup,
        },
        "LIB-13": {
            "venue_id": "LIB-13",
            "name_vi": "Nhà Sách Gỗ Thông & Cafe Đèo Frostwater",
            "name_en": "Frostwater Pine Lodge Bookstore & Cafe",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 4,
            "buy_default_usd": 30,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_rem_sloth_if",
                "rezero_tsugihagu_gluttony_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-14": {
            "venue_id": "LIB-14",
            "name_vi": "Trạm Sách & Cà Phê Cánh Đồng Lúa Mì Redland",
            "name_en": "Redland Wheatfield Roadside Book & Coffee",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 4,
            "buy_default_usd": 30,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_rem_sloth_if",
                "rezero_butterfly_lust_if",
                "_smoke_test_demo",
            ],
        },
        "LIB-15": {
            "venue_id": "LIB-15",
            "name_vi": "Thư Các Rừng Thông Đèo Blue Ridge Argal",
            "name_en": "Argal Blue Ridge Woodland Book Pavilion",
            "tier": "small",
            "featured_flagship": "rezero_arc7_wn",
            "rent_7d_usd": 4,
            "buy_default_usd": 30,
            "coffee_combo_usd": 4,
            "shelf_works": [
                "rezero_arc7_wn",
                "rezero_tsugihagu_gluttony_if",
                "rezero_kasaneru_greed_if",
                "_smoke_test_demo",
            ],
        },
    }

    return {
        "schema_version": "eagle_lit_catalog_v1",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gatekeeper_enforced": True,
        "certified_works": sorted(certified_work_ids),
        "auto_discover_pack_dirs": [
            "data/project-eagle/literature/packs",
            "ui_assets/literature_packs",
        ],
        "auto_include_new_packs": False,
        "showtime_policy": {
            "daily_showtimes": SHOWTIMES_5,
            "entry_grace_window_minutes": 60,
            "free_exchange_to_next_showtime": True,
        },
        "pricing_policy": {
            "cinema": {
                "GRAND": {"ticket_usd": 25, "popcorn_combo_usd": 10, "weekly_slots": 6},
                "SMALL": {"ticket_usd": 12, "popcorn_combo_usd": 6, "weekly_slots": 3},
            },
            "library": {
                "GRAND": {
                    "rent_7d_usd": 8,
                    "buy_default_usd": 45,
                    "coffee_combo_usd": 5,
                    "rent_duration_game_days": 7,
                },
                "SMALL": {
                    "rent_7d_usd": 5,
                    "buy_default_usd": 30,
                    "coffee_combo_usd": 4,
                    "rent_duration_game_days": 7,
                },
            },
        },
        "waifu_discussion_hooks": WAIFU_DISCUSSION_HOOKS,
        "cinema_schedules": cinema_schedules,
        "library_shelves": library_shelves,
    }


def write_guarded_rollback_script() -> None:
    ROLLBACK_SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    code = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Guarded Rollback Script for Literature Pack Release #1 (6 Re:Zero IF + Re:Zero Arc 7)
"""
import subprocess
import sys
from pathlib import Path

CLI = Path(r"C:\\Users\\LENOVO\\Desktop\\GTA\\tools\\deploy_literature_pack.py")
if __name__ == "__main__":
    rc = subprocess.call([sys.executable, str(CLI), "rollback"])
    sys.exit(rc)
'''
    ROLLBACK_SCRIPT_PATH.write_text(code, encoding="utf-8")


# ============================================================================
# CLI SUBCOMMANDS
# ============================================================================
def cmd_list(_args: argparse.Namespace) -> int:
    certs = load_certifications()
    studio_dirs = sorted([d.name for d in STUDIO_PACKAGES_DIR.iterdir() if d.is_dir()]) if STUDIO_PACKAGES_DIR.is_dir() else []
    deployed_links = sorted([d.name for d in GAME_PACKS_DIR.iterdir() if d.is_dir() or is_reparse_or_junction(d)]) if GAME_PACKS_DIR.is_dir() else []

    rows = []
    for wid in studio_dirs:
        c = certs.get(wid)
        verdict = c.get("verdict") if c else "UNCERTIFIED_WIP (SKIPPED)"
        rows.append(
            {
                "work_id": wid,
                "verdict": verdict,
                "venue_type": c.get("venue_type") if c else "wip",
                "chapters": (c.get("metrics_summary") or {}).get("chapter_count") if c else "?",
                "cgs": (c.get("metrics_summary") or {}).get("cg_count") if c else "?",
                "bgms": (c.get("metrics_summary") or {}).get("bgm_count") if c else "?",
                "deployed_in_game_packs": wid in deployed_links,
            }
        )
    print(json.dumps({"studio_packages": rows, "deployed_packs_dir": deployed_links}, indent=2, ensure_ascii=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    certs = load_certifications()
    studio_dirs = sorted([d.name for d in STUDIO_PACKAGES_DIR.iterdir() if d.is_dir()]) if STUDIO_PACKAGES_DIR.is_dir() else []
    target_ids = [args.work_id] if getattr(args, "work_id", None) else studio_dirs

    ready_packs = []
    skipped_uncertified = []
    failed_packs = []

    for wid in target_ids:
        c = certs.get(wid)
        if c is None and not getattr(args, "work_id", None):
            skipped_uncertified.append({"work_id": wid, "reason": "No .cert.json in certifications/ (WIP package excluded by Gatekeeper)"})
            continue
        res = verify_single_pack(wid, c)
        if res["ready"]:
            ready_packs.append(res)
        else:
            failed_packs.append(res)

    summary = {
        "verified_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ready_count": len(ready_packs),
        "skipped_uncertified_count": len(skipped_uncertified),
        "failed_count": len(failed_packs),
        "ready_work_ids": [r["work_id"] for r in ready_packs],
        "skipped_uncertified": skipped_uncertified,
        "failed_packs": failed_packs,
        "ready_details": ready_packs,
    }
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    (STAGING_DIR / "verify_report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if len(failed_packs) == 0 and len(ready_packs) > 0 else 1


def cmd_stage(args: argparse.Namespace) -> int:
    certs = load_certifications()
    ready_ids: list[str] = []
    ready_details: list[dict[str, Any]] = []
    skipped_ids: list[str] = []

    studio_dirs = sorted([d.name for d in STUDIO_PACKAGES_DIR.iterdir() if d.is_dir()]) if STUDIO_PACKAGES_DIR.is_dir() else []
    for wid in studio_dirs:
        c = certs.get(wid)
        if c is None or c.get("verdict") != "READY_FOR_INTEGRATION":
            skipped_ids.append(wid)
            continue
        res = verify_single_pack(wid, c)
        if not res["ready"]:
            print(f"[ERROR] Certified pack {wid} failed verification: {res['errors']}", file=sys.stderr)
            return 1
        ready_ids.append(wid)
        ready_details.append(res)

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged_packs_dir = STAGING_DIR / "packs"
    staged_packs_dir.mkdir(parents=True, exist_ok=True)

    # Create staging junctions on D: for all certified packs
    for wid in ready_ids:
        create_directory_junction(staged_packs_dir / wid, STUDIO_PACKAGES_DIR / wid)

    catalog = build_schedule_and_catalog(ready_ids)
    STAGED_CATALOG_PATH.write_text(json.dumps(catalog, indent=2, ensure_ascii=False), encoding="utf-8")
    write_guarded_rollback_script()

    stage_summary = {
        "staged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "staging_dir": str(STAGING_DIR),
        "staged_catalog_path": str(STAGED_CATALOG_PATH),
        "certified_ready_ids": ready_ids,
        "excluded_wip_ids": skipped_ids,
        "cinema_venues_scheduled": len(catalog["cinema_schedules"]),
        "library_venues_shelved": len(catalog["library_shelves"]),
    }
    (STAGING_DIR / "stage_summary.json").write_text(json.dumps(stage_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(stage_summary, indent=2, ensure_ascii=False))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    # Ensure stage is up to date outside lock first
    rc = cmd_stage(args)
    if rc != 0:
        return rc

    staged_catalog = json.loads(STAGED_CATALOG_PATH.read_text(encoding="utf-8"))
    certified_ids: list[str] = list(staged_catalog.get("certified_works") or [])

    chat_id = getattr(args, "chat", None) or DEFAULT_CHAT_ID
    with EagleCoordLock(
        "game-write",
        chat=chat_id,
        task="deploy-literature-packs-rezero-if-and-arc7",
        resources=[
            str(GAME_CATALOG_PATH),
            str(GAME_PACKS_DIR),
            str(GAME_ENGINE_PATH),
            str(GAME_HUD_LUA_PATH),
        ],
        wait_sec=600,
    ):
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        tracked_files = [
            ("schedule_and_catalog.json", GAME_CATALOG_PATH),
            ("eagle_literature_engine.py", GAME_ENGINE_PATH),
            ("EagleLiteratureHud.lua", GAME_HUD_LUA_PATH),
        ]

        before_hashes: dict[str, str] = {}
        for name, fpath in tracked_files:
            bkp_target = BACKUP_DIR / name
            if fpath.is_file() and not bkp_target.is_file():
                shutil.copy2(fpath, bkp_target)
            before_hashes[name] = sha256_file(bkp_target if bkp_target.is_file() else fpath)

        # Create lightweight NTFS junctions in `data/project-eagle/literature/packs/<work_id>`
        GAME_PACKS_DIR.mkdir(parents=True, exist_ok=True)
        linked_packs: list[str] = []
        for wid in certified_ids:
            src_pack = STUDIO_PACKAGES_DIR / wid
            dst_link = GAME_PACKS_DIR / wid
            create_directory_junction(dst_link, src_pack)
            linked_packs.append(wid)

        # Ensure any uncertified WIP pack is NOT linked inside `data/project-eagle/literature/packs`
        for child in list(GAME_PACKS_DIR.iterdir()):
            if child.name == "_smoke_test_demo":
                continue
            if child.name not in certified_ids and is_reparse_or_junction(child):
                remove_directory_junction(child)

        # Write updated `schedule_and_catalog.json`
        GAME_CATALOG_PATH.write_text(json.dumps(staged_catalog, indent=2, ensure_ascii=False), encoding="utf-8")

        after_hashes: dict[str, str] = {}
        for name, fpath in tracked_files:
            after_hashes[name] = sha256_file(fpath)

        manifest_doc = {
            "schema_version": "eagle_lit_deployment_manifest_v1",
            "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "chat_id": chat_id,
            "certified_packs_linked": linked_packs,
            "before_sha256": before_hashes,
            "after_sha256": after_hashes,
            "backup_dir": str(BACKUP_DIR),
            "rollback_script": str(ROLLBACK_SCRIPT_PATH),
        }
        DEPLOYMENT_MANIFEST_PATH.write_text(json.dumps(manifest_doc, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(manifest_doc, indent=2, ensure_ascii=False))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    wid = str(getattr(args, "work_id", "") or "").strip()
    if not wid:
        print("[ERROR] --work-id is required for remove", file=sys.stderr)
        return 1
    chat_id = getattr(args, "chat", None) or DEFAULT_CHAT_ID
    with EagleCoordLock(
        "game-write",
        chat=chat_id,
        task=f"remove-literature-pack-{wid}",
        resources=[str(GAME_CATALOG_PATH), str(GAME_PACKS_DIR / wid)],
        wait_sec=600,
    ):
        removed_link = remove_directory_junction(GAME_PACKS_DIR / wid)
        if GAME_CATALOG_PATH.is_file():
            cat = json.loads(GAME_CATALOG_PATH.read_text(encoding="utf-8"))
            if wid in (cat.get("certified_works") or []):
                cat["certified_works"] = [x for x in cat["certified_works"] if x != wid]
            for sc in (cat.get("cinema_schedules") or {}).values():
                if wid in (sc.get("featured_works") or []):
                    sc["featured_works"] = [x for x in sc["featured_works"] if x != wid]
            for sh in (cat.get("library_shelves") or {}).values():
                if wid in (sh.get("shelf_works") or []):
                    sh["shelf_works"] = [x for x in sh["shelf_works"] if x != wid]
            GAME_CATALOG_PATH.write_text(json.dumps(cat, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"removed_work_id": wid, "junction_removed": removed_link}, indent=2))
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    chat_id = getattr(args, "chat", None) or DEFAULT_CHAT_ID
    with EagleCoordLock(
        "game-write",
        chat=chat_id,
        task="rollback-literature-packs",
        resources=[str(GAME_CATALOG_PATH), str(GAME_PACKS_DIR), str(GAME_ENGINE_PATH), str(GAME_HUD_LUA_PATH)],
        wait_sec=600,
    ):
        restored: list[str] = []
        removed_links: list[str] = []
        if GAME_PACKS_DIR.is_dir():
            for child in list(GAME_PACKS_DIR.iterdir()):
                if child.name == "_smoke_test_demo":
                    continue
                if remove_directory_junction(child):
                    removed_links.append(child.name)

        restore_map = [
            ("schedule_and_catalog.json", GAME_CATALOG_PATH),
            ("eagle_literature_engine.py", GAME_ENGINE_PATH),
            ("EagleLiteratureHud.lua", GAME_HUD_LUA_PATH),
        ]
        for name, dst_path in restore_map:
            bkp = BACKUP_DIR / name
            if bkp.is_file():
                shutil.copy2(bkp, dst_path)
                restored.append(name)

        print(json.dumps({"rolled_back": True, "restored_files": restored, "removed_junctions": removed_links}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Eagle Literature Pack Gatekeeper & Deployment CLI")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_list = sub.add_parser("list", help="List Studio packages and certification/deployment status")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="Verify certified packages against Gatekeeper rules")
    p_verify.add_argument("--work-id", default="", help="Optional single work_id to verify")
    p_verify.set_defaults(func=cmd_verify)

    p_stage = sub.add_parser("stage", help="Stage 30-venue catalog and pack links on D:")
    p_stage.set_defaults(func=cmd_stage)

    p_apply = sub.add_parser("apply", help="Atomically apply certified packs and catalog to gameplay-lab")
    p_apply.add_argument("--chat", default=DEFAULT_CHAT_ID, help="Conversation ID for game-write lock")
    p_apply.set_defaults(func=cmd_apply)

    p_remove = sub.add_parser("remove", help="Remove a deployed pack from gameplay-lab")
    p_remove.add_argument("--work-id", required=True, help="work_id to remove")
    p_remove.add_argument("--chat", default=DEFAULT_CHAT_ID, help="Conversation ID for game-write lock")
    p_remove.set_defaults(func=cmd_remove)

    p_rb = sub.add_parser("rollback", help="Rollback literature pack integration to pre-apply backup")
    p_rb.add_argument("--chat", default=DEFAULT_CHAT_ID, help="Conversation ID for game-write lock")
    p_rb.set_defaults(func=cmd_rollback)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
