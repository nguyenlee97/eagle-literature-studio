# -*- coding: utf-8 -*-
"""
EagleLiteratureEngine (v1.0) — Decoupled Cinema & Library 3D World & Dual-Mode Reader Engine
============================================================================================
Powers Project Eagle's 30 World Venues (15 Cinemas CIN-01..15 + 15 Libraries LIB-01..15)
and 4 Sky-Grid 3D Interiors on Area Code = 1 (Z = +1650.0m):
  - INT-CIN-GRAND (2700.0, -1650.0, 1650.0) : Grand Lobby + 60-Seat IMAX Stepped Auditorium
  - INT-CIN-SMALL (3150.0, -1650.0, 1650.0) : Boutique Art-Deco Lobby + 24-Seat Screening Room
  - INT-LIB-GRAND (2700.0, -2150.0, 1650.0) : 2-Story Grand Library & Reading Hall
  - INT-LIB-SMALL (3150.0, -2150.0, 1650.0) : Cozy Bookstore & Book-Cafe

Key Architectural Guarantees:
  1. 100% Decoupled Engine & Data Catalog: Automatically scans `data/project-eagle/literature/packs/*`,
     `ui_assets/literature_packs/*`, and `D:\\Eagle Literature Studio\\packages\\*` plus
     `data/project-eagle/literature/schedule_and_catalog.json`. Zero code changes needed to add works.
  2. Double-Buffer 16:9 CG Streaming (`models/txd/LITCG0.TXD` & `LITCG1.TXD`) with strict 16:9 aspect lock.
  3. Dual-Mode Reader (`adv_cinema` IMAX Subtitles & `nvl_book` Multi-Paragraph Novel Page) with instant
     `[V]` mode switching, `[A]` 3-speed word-timed Auto-Play, `[H]` Hide UI, `[B]` Backlog, `[T]` Chapter TOC,
     Mouse Wheel (`0x00CAFE78`) + Touchscreen (`0x00CAFE60`), and persistent bookmarking (`eagle_lit_save.json`).
  4. Dedicated Win32 MCI BGM worker (`alias lit_bgm`) + `GTAPerProcessAudioMuter` with `atexit`/`finally` unmute.
  5. Strict 3D Room-by-Room State Machine:
     - Cinema: Street -> Lobby -> Ticket/Popcorn Counter -> Auditorium Door -> Seat -> `adv_cinema` HUD
       -> Closing HUD leaves Aether AT HIS SEAT inside the 3D Auditorium -> Walk to Auditorium `EXIT` door
       -> Lobby -> Walk to Lobby Main Exit -> Street (`return_xyz`).
     - Library: Street -> Library Hall -> Librarian Counter (Rent 7d / Buy Forever) -> Reading Table
       -> `nvl_book` HUD -> Closing HUD leaves Aether AT THE TABLE inside the Library -> Walk to Main Exit -> Street.
"""
from __future__ import annotations

import atexit
import ctypes
import json
import math
import os
import queue
import re
import struct
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from eagle_waifulink_bgm import GTAPerProcessAudioMuter
except Exception:
    GTAPerProcessAudioMuter = None


# ============================================================================
# CONSTANTS & MEMORY ADDRESSES
# ============================================================================
LIT_MAGIC = 0x454C4954              # 'ELIT'
CAFE_LIT_MAGIC_ADDR = 0x00CAFE50
CAFE_LIT_PTR_ADDR = 0x00CAFE54

ADDR_CLOCK_HOURS = 0x00B70153       # uint8 (0..23)
ADDR_CLOCK_MINS = 0x00B70152        # uint8 (0..59)
ADDR_CLOCK_DAYS = 0x00B70158        # uint16 days passed
ADDR_GAME_PAUSED = 0x00BA67A4       # uint8 (1 when paused)

SHOWTIMES_DEFAULT = ["09:00", "13:00", "17:00", "20:30", "23:30"]
AUTO_SPEED_LABELS = ["OFF", "SLOW 1.0x", "MED 1.5x", "FAST 2.0x"]
AUTO_SPEED_MULTS = [0.0, 1.0, 1.5, 2.0]

# Custom Ped IDs 49466..49485 (20 dedicated Literature NPCs; tuple = (ped_id, x, y, z, heading, pose))
# pose: 0 = attentive staff (stand still), 1 = chatting/browsing (IDLE_chat), 2 = seated (SEAT_idle)
INTERIOR_NPCS_BY_SHELL = {
    "INT-CIN-GRAND": [
        # Staff behind Box Office, Popcorn Bar, and IMAX Portal
        (49466, 2689.7, -1651.8, 1650.05, 270.0, 0),
        (49467, 2689.7, -1648.2, 1650.05, 270.0, 0),
        (49468, 2710.3, -1651.8, 1650.05, 90.0, 0),
        (49469, 2710.3, -1648.2, 1650.05, 90.0, 0),
        (49470, 2696.6, -1639.0, 1650.05, 180.0, 0),
        (49471, 2703.4, -1639.0, 1650.05, 180.0, 0),
        # Lobby Queue, Lounge Couples & Poster Admirer
        (49472, 2692.4, -1652.2, 1650.05, 90.0, 1),
        (49473, 2707.6, -1652.2, 1650.05, 270.0, 1),
        (49474, 2694.2, -1656.0, 1650.05, 270.0, 1),
        (49475, 2695.8, -1656.0, 1650.05, 90.0, 1),
        (49476, 2704.2, -1656.0, 1650.05, 270.0, 1),
        (49477, 2705.8, -1656.0, 1650.05, 90.0, 1),
        (49478, 2687.6, -1642.6, 1650.05, 90.0, 1),
        # Seated Moviegoers inside Stepped IMAX Auditorium (facing North screen)
        (49479, 2690.2, -1633.0, 1652.88, 0.0, 2),
        (49480, 2709.8, -1633.0, 1652.88, 0.0, 2),
        (49481, 2690.8, -1630.0, 1652.28, 0.0, 2),
        (49482, 2709.2, -1630.0, 1652.28, 0.0, 2),
        (49483, 2696.2, -1626.0, 1651.68, 0.0, 2),
        (49484, 2703.8, -1626.0, 1651.68, 0.0, 2),
        (49485, 2692.4, -1626.0, 1651.68, 0.0, 2),
        (49466, 2696.4, -1621.1, 1650.68, 0.0, 2),
        (49467, 2703.6, -1621.1, 1650.68, 0.0, 2),
    ],
    "INT-CIN-SMALL": [
        # Boutique Counter & Door Staff
        (49470, 3142.3, -1650.2, 1650.05, 270.0, 0),
        (49471, 3142.3, -1646.8, 1650.05, 270.0, 0),
        (49472, 3147.2, -1642.8, 1650.05, 180.0, 0),
        # Lobby Cinephiles & Bistro Booth Couples
        (49473, 3144.6, -1651.4, 1650.05, 90.0, 1),
        (49474, 3155.8, -1655.4, 1650.05, 270.0, 1),
        (49475, 3157.2, -1655.4, 1650.05, 90.0, 1),
        (49476, 3155.8, -1646.6, 1650.05, 270.0, 1),
        (49477, 3158.2, -1648.0, 1650.05, 270.0, 1),
        # Seated Boutique Screening Room Audience
        (49478, 3144.6, -1636.0, 1651.38, 0.0, 2),
        (49479, 3155.4, -1636.0, 1651.38, 0.0, 2),
        (49480, 3142.6, -1632.5, 1650.88, 0.0, 2),
        (49481, 3157.4, -1632.5, 1650.88, 0.0, 2),
        (49482, 3144.8, -1628.0, 1650.38, 0.0, 2),
        (49483, 3147.4, -1628.0, 1650.38, 0.0, 2),
        (49484, 3152.6, -1628.0, 1650.38, 0.0, 2),
        (49485, 3155.2, -1628.0, 1650.38, 0.0, 2),
    ],
    "INT-LIB-GRAND": [
        # Librarians & Curators
        (49474, 2689.3, -2155.8, 1650.05, 270.0, 0),
        (49475, 2689.3, -2152.5, 1650.05, 270.0, 0),
        (49476, 2710.7, -2154.2, 1650.05, 90.0, 0),
        # Book Browsers & Upper Mezzanine Scholars
        (49477, 2691.8, -2156.2, 1650.05, 90.0, 1),
        (49478, 2708.2, -2156.2, 1650.05, 270.0, 1),
        (49479, 2686.5, -2146.5, 1650.05, 90.0, 1),
        (49480, 2713.5, -2140.5, 1650.05, 270.0, 1),
        (49481, 2696.5, -2134.8, 1650.05, 0.0, 1),
        (49482, 2686.6, -2148.0, 1655.05, 270.0, 1),
        (49483, 2713.4, -2148.0, 1655.05, 90.0, 1),
        # Seated Scholars & Readers at Mahogany Banker's Lamp Tables
        (49484, 2700.5, -2144.5, 1650.05, 180.0, 2),
        (49485, 2694.0, -2144.5, 1650.05, 180.0, 2),
        (49466, 2706.0, -2144.5, 1650.05, 180.0, 2),
        (49467, 2700.5, -2137.5, 1650.05, 180.0, 2),
        (49468, 2689.5, -2140.5, 1650.05, 180.0, 2),
        (49469, 2710.5, -2140.5, 1650.05, 180.0, 2),
        (49470, 2694.5, -2139.0, 1650.05, 0.0, 2),
        (49471, 2705.5, -2139.0, 1650.05, 0.0, 2),
        (49472, 2694.5, -2151.2, 1650.05, 0.0, 2),
        (49473, 2705.5, -2151.2, 1650.05, 0.0, 2),
    ],
    "INT-LIB-SMALL": [
        # Barista, Cashier & Light Novel Specialist
        (49478, 3141.8, -2152.5, 1650.05, 270.0, 0),
        (49479, 3141.8, -2149.5, 1650.05, 270.0, 0),
        (49480, 3158.2, -2151.0, 1650.05, 90.0, 0),
        # Tsutaya Book-Cafe Browsers
        (49481, 3144.2, -2153.2, 1650.05, 90.0, 1),
        (49482, 3155.8, -2153.2, 1650.05, 270.0, 1),
        (49483, 3143.0, -2140.5, 1650.05, 0.0, 1),
        (49484, 3157.0, -2140.5, 1650.05, 0.0, 1),
        (49485, 3158.8, -2148.5, 1650.05, 270.0, 1),
        # Seated Book-Cafe Readers
        (49466, 3150.0, -2143.5, 1650.05, 180.0, 2),
        (49467, 3146.5, -2143.5, 1650.05, 180.0, 2),
        (49468, 3153.5, -2143.5, 1650.05, 180.0, 2),
        (49469, 3150.0, -2139.7, 1650.05, 180.0, 2),
        (49470, 3142.6, -2144.2, 1650.05, 0.0, 2),
        (49471, 3157.4, -2144.2, 1650.05, 0.0, 2),
        (49472, 3145.8, -2141.2, 1650.05, 0.0, 2),
        (49473, 3154.2, -2141.2, 1650.05, 0.0, 2),
    ],
}

# 3D Interior Spatial Anchors (Area Code = 1, Z = 1650.0m)
INTERIOR_SHELL_LAYOUTS = {
    "INT-CIN-GRAND": {
        "origin": (2700.0, -1650.0, 1650.0),
        "lobby_spawn": (2700.0, -1658.2, 1650.05, 0.0),
        "lobby_exit_door": (2700.0, -1660.2, 1650.05),
        "ticket_counter": (2691.8, -1650.0, 1650.05),
        "popcorn_counter": (2708.2, -1650.0, 1650.05),
        "poster_board": (2693.5, -1643.5, 1650.05),
        "lounge_anchor": (2700.0, -1653.0, 1650.05),
        "auditorium_entry_door": (2700.0, -1639.2, 1650.05),
        "auditorium_return_lobby": (2700.0, -1641.2, 1650.05, 180.0),
        "auditorium_spawn": (2700.0, -1634.2, 1652.85, 0.0),
        "auditorium_exit_door": (2700.0, -1636.2, 1652.85),
        "screen_pos": (2700.0, -1607.5, 1654.5),
        "seat_anchors": [
            {"id": "imax_vip_center", "label": "IMAX VIP Center Row D", "pos": (2700.0, -1626.0, 1651.65), "couple": False},
            {"id": "imax_couple_1",   "label": "Waifu Couple Box W1",   "pos": (2695.0, -1630.0, 1652.25), "couple": True},
            {"id": "imax_couple_2",   "label": "Waifu Couple Box C1",   "pos": (2700.0, -1630.0, 1652.25), "couple": True},
            {"id": "imax_couple_3",   "label": "Waifu Couple Box E1",   "pos": (2705.0, -1630.0, 1652.25), "couple": True},
            {"id": "imax_front_row",  "label": "IMAX Front Row B",      "pos": (2700.0, -1620.0, 1650.65), "couple": False},
            {"id": "imax_back_row",   "label": "IMAX Panorama Row E",   "pos": (2700.0, -1633.0, 1652.85), "couple": True},
        ],
    },
    "INT-CIN-SMALL": {
        "origin": (3150.0, -1650.0, 1650.0),
        "lobby_spawn": (3150.0, -1656.2, 1650.05, 0.0),
        "lobby_exit_door": (3150.0, -1658.0, 1650.05),
        "ticket_counter": (3144.2, -1649.5, 1650.05),
        "popcorn_counter": (3144.2, -1646.5, 1650.05),
        "poster_board": (3155.5, -1649.5, 1650.05),
        "lounge_anchor": (3153.5, -1653.0, 1650.05),
        "auditorium_entry_door": (3150.0, -1642.8, 1650.05),
        "auditorium_return_lobby": (3150.0, -1644.8, 1650.05, 180.0),
        "auditorium_spawn": (3150.0, -1637.2, 1651.35, 0.0),
        "auditorium_exit_door": (3150.0, -1639.5, 1651.35),
        "screen_pos": (3150.0, -1621.5, 1653.0),
        "seat_anchors": [
            {"id": "boutique_center", "label": "Boutique Center Row B", "pos": (3150.0, -1632.5, 1650.85), "couple": False},
            {"id": "boutique_cpl_w",  "label": "Velvet Couple Sofa W",  "pos": (3146.5, -1632.5, 1650.85), "couple": True},
            {"id": "boutique_cpl_e",  "label": "Velvet Couple Sofa E",  "pos": (3153.5, -1632.5, 1650.85), "couple": True},
            {"id": "boutique_front",  "label": "Boutique Front Row A",  "pos": (3150.0, -1628.0, 1650.35), "couple": False},
            {"id": "boutique_back",   "label": "Boutique Back Row C",   "pos": (3150.0, -1636.0, 1651.35), "couple": True},
        ],
    },
    "INT-LIB-GRAND": {
        "origin": (2700.0, -2150.0, 1650.0),
        "hall_spawn": (2700.0, -2163.5, 1650.05, 0.0),
        "hall_exit_door": (2700.0, -2166.0, 1650.05),
        "librarian_counter": (2691.5, -2154.0, 1650.05),
        "featured_shelf": (2708.5, -2154.0, 1650.05),
        "lounge_anchor": (2700.0, -2158.0, 1650.05),
        "table_anchors": [
            {"id": "lib_grand_t1", "label": "Grand Reading Desk C1", "pos": (2700.0, -2146.0, 1650.05), "couple": False},
            {"id": "lib_grand_t2", "label": "Grand Reading Desk W1", "pos": (2694.5, -2146.0, 1650.05), "couple": False},
            {"id": "lib_grand_t3", "label": "Grand Reading Desk E1", "pos": (2705.5, -2146.0, 1650.05), "couple": False},
            {"id": "lib_grand_t4", "label": "North Archive Desk C2", "pos": (2700.0, -2139.0, 1650.05), "couple": False},
            {"id": "lib_grand_cw", "label": "Couple Study Booth W",  "pos": (2689.5, -2142.0, 1650.05), "couple": True},
            {"id": "lib_grand_ce", "label": "Couple Study Booth E",  "pos": (2710.5, -2142.0, 1650.05), "couple": True},
        ],
    },
    "INT-LIB-SMALL": {
        "origin": (3150.0, -2150.0, 1650.0),
        "hall_spawn": (3150.0, -2158.0, 1650.05, 0.0),
        "hall_exit_door": (3150.0, -2160.2, 1650.05),
        "librarian_counter": (3143.8, -2151.0, 1650.05),
        "featured_shelf": (3156.2, -2151.0, 1650.05),
        "lounge_anchor": (3150.0, -2154.5, 1650.05),
        "table_anchors": [
            {"id": "lib_small_t1", "label": "Book-Cafe Center Table", "pos": (3150.0, -2145.0, 1650.05), "couple": False},
            {"id": "lib_small_t2", "label": "Window Reading Table W", "pos": (3146.5, -2145.0, 1650.05), "couple": True},
            {"id": "lib_small_t3", "label": "Window Reading Table E", "pos": (3153.5, -2145.0, 1650.05), "couple": True},
            {"id": "lib_small_t4", "label": "Quiet Alcove Desk N",    "pos": (3150.0, -2141.2, 1650.05), "couple": False},
        ],
    },
}


# ============================================================================
# CFONT ASCII SANITIZER & TEXT WRAPPING HELPERS
# ============================================================================
_VI_TRANS = str.maketrans(
    "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
    "ÀÁẠẢÃÂẦẤẬẨẪĂẰẮẶẲẴÈÉẸẺẼÊỀẾỆỂỄÌÍỊỈĨÒÓỌỎÕÔỒỐỘỔỖƠỜỚỢỞỠÙÚỤỦŨƯỪỨỰỬỮỲÝỴỶỸĐ",
    "aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd"
    "AAAAAAAAAAAAAAAAAEEEEEEEEEEEIIIIIOOOOOOOOOOOOOOOOOUUUUUUUUUUUYYYYYD",
)


def clean_cfont_ascii(text: str) -> str:
    """Strictly sanitize any string to GTA SA RenderWare CFont-safe printable ASCII (32..126)."""
    if not text:
        return ""
    s = str(text).translate(_VI_TRANS)
    s = (
        s.replace("—", " - ")
        .replace("–", " - ")
        .replace("―", " - ")
        .replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("…", "...")
        .replace("~", "-")
        .replace("|", "/")
        .replace("*", "+")
        .replace("@", "a")
    )
    s = re.sub(r"[^\x20-\x7E]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_hhmm_to_minutes(hhmm: str) -> int:
    try:
        parts = str(hhmm).strip().split(":")
        return (int(parts[0]) % 24) * 60 + (int(parts[1]) % 60)
    except Exception:
        return 540


def _circular_minute_diff(now_min: int, target_min: int) -> int:
    """Signed difference (now_min - target_min) in [-720, +720] across midnight."""
    diff = (now_min - target_min) % 1440
    if diff > 720:
        diff -= 1440
    return diff


# ============================================================================
# DEDICATED WIN32 MCI BGM WORKER (`alias lit_bgm`)
# ============================================================================
class _LitMciBgmWorker(threading.Thread):
    """
    Plays instrumental BGM tracks from Literature Packs using Win32 `mciSendStringW`
    under `alias lit_bgm` so it never collides with `wl_bgm`, `sq_bgm`, or GTA Radio.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="EagleLitMciBgmThread")
        self._cmd_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._winmm = getattr(ctypes, "windll", None).winmm if hasattr(ctypes, "windll") else None
        self.current_path: str = ""
        self.current_loop: bool = True
        self.current_vol_pct: int = 80
        self.is_playing: bool = False
        self.is_paused_by_menu: bool = False
        self.start()

    def _mci(self, cmd: str) -> tuple[int, str]:
        if self._winmm is None:
            return (-1, "")
        buf = ctypes.create_unicode_buffer(256)
        rc = int(self._winmm.mciSendStringW(str(cmd), buf, 255, 0))
        return (rc, buf.value.strip())

    def play(self, abs_path: str | Path, loop: bool = True, volume_pct: int = 80) -> None:
        p_str = str(Path(abs_path).resolve()) if abs_path else ""
        self._cmd_q.put(("play", (p_str, bool(loop), max(0, min(100, int(volume_pct))))))

    def pause_for_menu(self, paused: bool) -> None:
        self._cmd_q.put(("pause_menu", bool(paused)))

    def stop(self) -> None:
        self._cmd_q.put(("stop", None))

    def run(self) -> None:
        last_poll = 0.0
        while True:
            try:
                cmd, arg = self._cmd_q.get(timeout=0.25)
                if cmd == "play":
                    p_str, loop, vol_pct = arg
                    if not p_str or not os.path.isfile(p_str):
                        self._mci("stop lit_bgm")
                        self._mci("close lit_bgm")
                        self.current_path = ""
                        self.is_playing = False
                        continue
                    if p_str == self.current_path and self.is_playing:
                        continue
                    self._mci("stop lit_bgm")
                    self._mci("close lit_bgm")
                    rc, _ = self._mci(f'open "{p_str}" type mpegvideo alias lit_bgm')
                    if rc != 0:
                        rc, _ = self._mci(f'open "{p_str}" alias lit_bgm')
                    if rc == 0:
                        self._mci(f"setaudio lit_bgm volume to {int(vol_pct) * 10}")
                        play_cmd = "play lit_bgm repeat" if loop else "play lit_bgm"
                        rc_p, _ = self._mci(play_cmd)
                        if rc_p != 0:
                            self._mci("play lit_bgm")
                        self.current_path = p_str
                        self.current_loop = loop
                        self.current_vol_pct = vol_pct
                        self.is_playing = True
                        self.is_paused_by_menu = False
                elif cmd == "pause_menu":
                    want_pause = bool(arg)
                    if self.is_playing and want_pause and not self.is_paused_by_menu:
                        self._mci("pause lit_bgm")
                        self.is_paused_by_menu = True
                    elif self.is_playing and not want_pause and self.is_paused_by_menu:
                        self._mci("resume lit_bgm")
                        self.is_paused_by_menu = False
                elif cmd == "stop":
                    self._mci("stop lit_bgm")
                    self._mci("close lit_bgm")
                    self.current_path = ""
                    self.is_playing = False
                    self.is_paused_by_menu = False
            except queue.Empty:
                pass
            except Exception:
                pass

            now = time.time()
            if self.is_playing and not self.is_paused_by_menu and self.current_loop and (now - last_poll) > 1.2:
                last_poll = now
                rc, mode = self._mci("status lit_bgm mode")
                if rc == 0 and mode.lower() == "stopped":
                    self._mci("seek lit_bgm to start")
                    rc_p, _ = self._mci("play lit_bgm repeat")
                    if rc_p != 0:
                        self._mci("play lit_bgm")


# ============================================================================
# PACK VALIDATOR & INDEXED CHAPTER DATA STRUCTURES
# ============================================================================
def validate_literature_pack(pack_dir: str | Path) -> dict[str, Any]:
    """
    Offline & runtime validator for an `eagle_lit_v1` package directory.
    Returns `{"valid": bool, "work_id": str, "errors": list[str], "stats": dict}`.
    """
    pdir = Path(pack_dir)
    errors: list[str] = []
    manifest_path = pdir / "manifest.json"
    if not manifest_path.is_file():
        return {"valid": False, "work_id": pdir.name, "errors": ["Missing manifest.json"], "stats": {}}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"valid": False, "work_id": pdir.name, "errors": [f"Invalid manifest JSON: {exc}"], "stats": {}}

    if manifest.get("schema_version") != "eagle_lit_v1":
        errors.append(f"Unsupported schema_version: {manifest.get('schema_version')!r}")

    work_id = str(manifest.get("work_id") or pdir.name).strip()
    for req_key in ("title", "venue_type", "default_read_mode", "supported_read_modes", "chapters"):
        if req_key not in manifest:
            errors.append(f"Missing required manifest field: {req_key}")

    cg_cat = manifest.get("cg_catalog") or {}
    for cg_id, cg_meta in cg_cat.items():
        rel_f = str(cg_meta.get("file") or "")
        cg_p = pdir / rel_f
        if not cg_p.is_file():
            errors.append(f"Missing CG file for {cg_id}: {rel_f}")

    bgm_cat = manifest.get("bgm_catalog") or {}
    for bgm_id, bgm_meta in bgm_cat.items():
        rel_f = str(bgm_meta.get("file") or "")
        bgm_p = pdir / rel_f
        if not bgm_p.is_file():
            errors.append(f"Missing BGM file for {bgm_id}: {rel_f}")

    chapters_meta = manifest.get("chapters") or []
    if not chapters_meta:
        errors.append("manifest.chapters is empty")

    total_blocks = 0
    total_beats = 0
    for ch_entry in chapters_meta:
        ch_rel = str(ch_entry.get("file") or "")
        ch_p = pdir / ch_rel
        if not ch_p.is_file():
            errors.append(f"Missing chapter file: {ch_rel}")
            continue
        try:
            ch_data = json.loads(ch_p.read_text(encoding="utf-8"))
            ch_blocks = 0
            for sc in ch_data.get("scenes") or []:
                for blk in sc.get("blocks") or []:
                    beats = blk.get("vn_beats") or []
                    if beats or blk.get("nvl_paragraph") or blk.get("cg"):
                        total_blocks += 1
                        ch_blocks += 1
                        total_beats += max(1, len(beats))
            if ch_blocks == 0:
                errors.append(f"No readable blocks in {ch_rel}")
        except Exception as exc:
            errors.append(f"Invalid chapter JSON {ch_rel}: {exc}")

    return {
        "valid": len(errors) == 0,
        "work_id": work_id,
        "title": clean_cfont_ascii(manifest.get("title", work_id)),
        "errors": errors,
        "stats": {
            "chapters": len(chapters_meta),
            "cgs": len(cg_cat),
            "bgms": len(bgm_cat),
            "total_blocks": total_blocks,
            "total_beats": total_beats,
        },
    }


class LiteraturePack:
    """In-memory representation of a validated `eagle_lit_v1` work with lazy-loaded chapter indexing."""

    def __init__(self, pack_dir: Path, manifest: dict[str, Any]) -> None:
        self.pack_dir = pack_dir
        self.manifest = manifest
        self.work_id: str = str(manifest.get("work_id") or pack_dir.name).strip()
        self.title: str = clean_cfont_ascii(manifest.get("title") or self.work_id)
        self.subtitle: str = clean_cfont_ascii(manifest.get("subtitle") or "")
        self.author: str = clean_cfont_ascii(manifest.get("author") or "Eagle Literature Studio")
        self.series: str = clean_cfont_ascii(manifest.get("series") or manifest.get("franchise") or "")
        raw_tags = manifest.get("genre_tags") or manifest.get("tags") or []
        self.genre_tags: list[str] = [clean_cfont_ascii(g) for g in raw_tags]
        self.age_rating: str = clean_cfont_ascii(manifest.get("age_rating") or "ALL")
        self.venue_type: str = str(manifest.get("venue_type") or "both").lower()
        self.default_read_mode: str = str(manifest.get("default_read_mode") or "adv_cinema").lower()
        self.supported_read_modes: list[str] = [
            str(m).lower() for m in (manifest.get("supported_read_modes") or ["adv_cinema", "nvl_book"])
        ]
        econ = manifest.get("economy") or {}
        self.estimated_minutes: int = int(
            manifest.get("estimated_minutes") or econ.get("estimated_runtime_min") or 15
        )
        self.cinema_ticket_usd: int = int(
            econ.get("cinema_ticket_usd") or econ.get("ticket_price_usd") or 25
        )
        self.library_rent_usd: int = int(
            econ.get("library_rent_usd") or econ.get("rent_price_usd") or 6
        )
        self.library_buy_usd: int = int(
            econ.get("library_buy_usd") or econ.get("buy_price_usd") or 30
        )
        self.synopsis: str = clean_cfont_ascii(manifest.get("synopsis") or "")
        rgb = manifest.get("theme_color_rgb") or [80, 200, 255]
        self.theme_rgb: tuple[int, int, int] = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        raw_cast = manifest.get("cast") or {}
        if isinstance(raw_cast, list):
            self.cast: dict[str, dict[str, Any]] = {
                str(c.get("char_key") or c.get("id") or i): c
                for i, c in enumerate(raw_cast)
                if isinstance(c, dict)
            }
        elif isinstance(raw_cast, dict):
            self.cast = raw_cast
        else:
            self.cast = {}
        self.cg_catalog: dict[str, dict[str, Any]] = manifest.get("cg_catalog") or {}
        self.bgm_catalog: dict[str, dict[str, Any]] = manifest.get("bgm_catalog") or {}
        self.chapters: list[dict[str, Any]] = sorted(
            list(manifest.get("chapters") or []),
            key=lambda c: int(c.get("index") or 1),
        )
        self._chapter_cache: dict[str, dict[str, Any]] = {}

    def resolve_cover_cg_path(self) -> Path | None:
        cov = self.manifest.get("cover_assets") or {}
        for k in ("banner_16x9", "banner_16x9_rel", "poster_2x3", "poster_2x3_rel"):
            rel = cov.get(k)
            if rel:
                p = self.pack_dir / str(rel)
                if p.is_file():
                    return p
        for fallback_name in ("banner_16x9.jpg", "poster.jpg"):
            p = self.pack_dir / fallback_name
            if p.is_file():
                return p
        for cg_meta in self.cg_catalog.values():
            rel = cg_meta.get("file")
            if rel:
                p = self.pack_dir / str(rel)
                if p.is_file():
                    return p
        return None

    def resolve_cg_path(self, cg_id: str | None) -> Path | None:
        if not cg_id:
            return self.resolve_cover_cg_path()
        meta = self.cg_catalog.get(str(cg_id))
        if meta and meta.get("file"):
            p = self.pack_dir / str(meta["file"])
            if p.is_file():
                return p
        return self.resolve_cover_cg_path()

    def resolve_bgm_info(self, bgm_id: str | None) -> tuple[Path | None, str, bool, int]:
        if not bgm_id:
            return (None, "", True, 80)
        meta = self.bgm_catalog.get(str(bgm_id))
        if not meta or not meta.get("file"):
            return (None, "", True, 80)
        p = self.pack_dir / str(meta["file"])
        if not p.is_file():
            return (None, "", True, 80)
        title = clean_cfont_ascii(meta.get("title") or str(bgm_id))
        loop = bool(meta.get("loop", True))
        if "volume_pct" in meta:
            vol = int(meta["volume_pct"])
        elif "base_mci_volume" in meta:
            vol = max(15, min(100, int(round(float(meta["base_mci_volume"]) / 10.0))))
        else:
            vol = 80
        return (p, title, loop, vol)

    def get_indexed_chapter(self, chapter_id: str | None = None) -> dict[str, Any]:
        if not self.chapters:
            return {"chapter_id": "ch01", "chapter_title": self.title, "flat_beats": [], "flat_blocks": []}
        ch_meta = None
        if chapter_id:
            ch_meta = next((c for c in self.chapters if c.get("chapter_id") == chapter_id), None)
        if ch_meta is None:
            ch_meta = self.chapters[0]
        cid = str(ch_meta.get("chapter_id") or "ch01")
        if cid in self._chapter_cache:
            return self._chapter_cache[cid]

        ch_path = self.pack_dir / str(ch_meta.get("file") or f"chapters/{cid}.json")
        raw = json.loads(ch_path.read_text(encoding="utf-8"))
        ch_title = clean_cfont_ascii(raw.get("chapter_title") or ch_meta.get("title") or cid)
        default_bgm = raw.get("default_bgm")

        flat_beats: list[dict[str, Any]] = []
        flat_blocks: list[dict[str, Any]] = []

        for sc in raw.get("scenes") or []:
            sc_id = str(sc.get("scene_id") or "sc01")
            loc_lbl = clean_cfont_ascii(sc.get("location_label") or ch_title)
            sc_bgm = sc.get("bgm") or default_bgm
            sc_cg = sc.get("initial_cg")

            for blk in sc.get("blocks") or []:
                blk_idx = len(flat_blocks)
                blk_id = str(blk.get("block_id") or f"b{blk_idx + 1:03d}")
                blk_cg = blk.get("cg") or sc_cg
                blk_bgm = blk.get("bgm") or sc_bgm
                nvl_para = clean_cfont_ascii(blk.get("nvl_paragraph") or "")
                raw_beats = blk.get("vn_beats") or []

                first_beat_idx = len(flat_beats)
                beat_lines_for_nvl: list[str] = []

                if not raw_beats and not nvl_para:
                    if blk.get("cg"):
                        nvl_para = "[Illustration]"
                        raw_beats = [
                            {
                                "beat_id": f"{blk_id}_01",
                                "speaker": "narrator",
                                "text": "[Illustration]",
                                "cg": blk_cg,
                                "transition": "fade",
                            }
                        ]
                    else:
                        continue
                elif not raw_beats and nvl_para:
                    raw_beats = [
                        {
                            "beat_id": f"{blk_id}_01",
                            "speaker": "narrator",
                            "text": nvl_para,
                            "cg": blk_cg,
                            "transition": "none",
                        }
                    ]

                for rb in raw_beats:
                    b_idx = len(flat_beats)
                    b_id = str(rb.get("beat_id") or f"{blk_id}_{b_idx + 1:02d}")
                    spk_key = str(rb.get("speaker") or "narrator").strip()
                    cast_entry = self.cast.get(spk_key) or {}
                    spk_name = clean_cfont_ascii(cast_entry.get("display_name") or ("" if spk_key == "narrator" else spk_key.upper()))
                    c_rgb = cast_entry.get("nameplate_color_rgb") or [220, 225, 240]
                    spk_rgb = (int(c_rgb[0]), int(c_rgb[1]), int(c_rgb[2]))
                    b_text = clean_cfont_ascii(rb.get("text") or nvl_para)
                    b_cg = rb.get("cg") or blk_cg
                    words = max(1, len(b_text.split()))

                    if spk_name:
                        beat_lines_for_nvl.append(f'{spk_name}: "{b_text}"')
                    else:
                        beat_lines_for_nvl.append(b_text)

                    flat_beats.append(
                        {
                            "beat_idx": b_idx,
                            "beat_id": b_id,
                            "block_idx": blk_idx,
                            "block_id": blk_id,
                            "scene_id": sc_id,
                            "location_label": loc_lbl,
                            "speaker_key": spk_key,
                            "speaker_name": spk_name,
                            "speaker_rgb": spk_rgb,
                            "text": b_text,
                            "cg_id": b_cg,
                            "bgm_id": blk_bgm,
                            "words": words,
                        }
                    )

                if not nvl_para:
                    nvl_para = " ".join(beat_lines_for_nvl)

                flat_blocks.append(
                    {
                        "block_idx": blk_idx,
                        "block_id": blk_id,
                        "first_beat_idx": first_beat_idx,
                        "scene_id": sc_id,
                        "location_label": loc_lbl,
                        "nvl_paragraph": nvl_para,
                        "cg_id": blk_cg,
                        "bgm_id": blk_bgm,
                        "words": max(1, len(nvl_para.split())),
                    }
                )

        indexed = {
            "chapter_id": cid,
            "chapter_index": int(ch_meta.get("index") or 1),
            "chapter_title": ch_title,
            "flat_beats": flat_beats,
            "flat_blocks": flat_blocks,
        }
        self._chapter_cache[cid] = indexed
        return indexed


# ============================================================================
# MAIN ENGINE: EagleLiteratureEngine
# ============================================================================
class EagleLiteratureEngine:
    """
    Core runtime manager for Cinema & Library 3D Venues, Interior State Machine,
    Schedule/Shelf Catalog, Double-Buffer CG TXD Loader, and Dual-Mode HUD.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.lit_data_dir = self.base_dir / "data" / "project-eagle" / "literature"
        self.lit_data_dir.mkdir(parents=True, exist_ok=True)
        self.packs_dir = self.lit_data_dir / "packs"
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.lit_data_dir / "schedule_and_catalog.json"
        self.save_path = self.lit_data_dir / "eagle_lit_save.json"
        self.venues_path = self.lit_data_dir / "venues_30_verified.json"

        # Audio Muter & MCI BGM Worker
        self.audio_muter = GTAPerProcessAudioMuter() if GTAPerProcessAudioMuter is not None else None
        self.bgm_worker = _LitMciBgmWorker()
        atexit.register(self.shutdown)

        # Double-Buffer CG state (`models/txd/LITCG0.TXD` & `LITCG1.TXD`)
        self._cg_slot: int = 0
        self._cg_ver: int = 0
        self._cg_last_path: str = ""
        self._ensure_lit_cg_txd_slots()

        # Persistent Player Literature Save (`bookmarks`, `owned_books`, `rented_books`, `active_tickets`)
        self.save_data: dict[str, Any] = {
            "schema_version": "eagle_lit_save_v1",
            "bookmarks": {},
            "owned_books": ["_smoke_test_demo"],
            "rented_books": {},
            "active_tickets": {},
            "stats": {"movies_watched": 0, "books_read": 0, "popcorn_combos": 0, "coffee_combos": 0},
        }
        self._load_save()

        # Load 30 Verified Venues & Catalog/Packs
        self.venues: dict[str, dict[str, Any]] = {}
        self.venues_list: list[dict[str, Any]] = []
        self._load_venues()

        self.catalog: dict[str, Any] = {}
        self.packs: dict[str, LiteraturePack] = {}
        self._last_pack_scan_ts: float = 0.0
        self.reload_catalog_and_packs()

        # Live 3D World & Interior State Machine
        # States:
        #   "OUTSIDE"
        #   "CINEMA_LOBBY"
        #   "MODAL_TICKET_COUNTER"
        #   "CINEMA_AUDITORIUM"
        #   "READING_ADV_CINEMA"
        #   "LIBRARY_HALL"
        #   "MODAL_LIBRARY_COUNTER"
        #   "READING_NVL_BOOK"
        self.state: str = "OUTSIDE"
        self.return_state_after_reader: str = "OUTSIDE"
        self.active_venue_id: str = ""
        self.active_shell_id: str = ""
        self.return_xyz: tuple[float, float, float] = (1481.0, -1768.0, 18.8)
        self.return_heading: float = 0.0
        self.seat_return_xyz: tuple[float, float, float] = (2700.0, -1626.0, 1651.65)
        self.near_venue_id: str = ""
        self.near_anchor_kind: str = ""
        self.near_anchor_label: str = ""

        # Counter Modal state
        self.counter_selected_idx: int = 0
        self.counter_showtime_idx: int = 0
        self.has_popcorn_combo: bool = False
        self.has_coffee_buff: bool = False
        self.status_banner: str = ""
        self.status_banner_until: float = 0.0

        # Active Reader state (`adv_cinema` or `nvl_book`)
        self.active_work_id: str = ""
        self.active_chapter_idx: int = 0
        self.active_beat_idx: int = 0
        self.active_block_idx: int = 0
        self.reader_mode: str = "adv_cinema"
        self.auto_speed_idx: int = 0  # 0=OFF, 1=SLOW, 2=MED, 3=FAST
        self.next_auto_beat_ts: float = 0.0
        self.ui_hidden: bool = False
        self.backlog_open: bool = False
        self.toc_open: bool = False
        self.reader_opened_at: float = 0.0

        # CLEO SHM command sequence (`0x00CAFE50`)
        self._cleo_cmd_seq: int = 0
        self._last_touch_seq: int = 0
        self._last_wheel_seq: int = 0
        self._last_rmb_seq: int = 0

    # ------------------------------------------------------------------------
    # INITIALIZATION, SAVE & CATALOG DISCOVERY
    # ------------------------------------------------------------------------
    def _load_venues(self) -> None:
        src_candidates = [
            self.venues_path,
            Path(r"D:\GTA Archive\Staging\project-eagle\cinema-library-builder-20261106\venues_30_v2_verified.json"),
            Path(r"D:\GTA Archive\Staging\project-eagle\cinema-library-survey-20261106\venues_30_verified.json"),
        ]
        for cand in src_candidates:
            if cand.is_file():
                try:
                    raw = json.loads(cand.read_text(encoding="utf-8"))
                    v_list = raw.get("venues") or []
                    self.venues_list = v_list
                    self.venues = {(v.get("id") or v.get("venue_id")): v for v in v_list}
                    if cand != self.venues_path:
                        self.venues_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
                    break
                except Exception:
                    pass

    def _load_save(self) -> None:
        if self.save_path.is_file():
            try:
                loaded = json.loads(self.save_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.save_data.update(loaded)
            except Exception:
                pass
        if "_smoke_test_demo" not in self.save_data.setdefault("owned_books", []):
            self.save_data["owned_books"].append("_smoke_test_demo")

    def _write_save(self) -> None:
        try:
            tmp = self.save_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.save_data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.save_path)
        except Exception:
            pass

    def reload_catalog_and_packs(self) -> None:
        """
        100% Decoupled Discovery: Scans `schedule_and_catalog.json` and all configured pack directories
        so Integrator can drop new packages in without modifying any engine code.
        """
        self._last_pack_scan_ts = time.time()
        if self.catalog_path.is_file():
            try:
                self.catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            except Exception:
                self.catalog = {}

        scan_dirs = [
            self.packs_dir,
            self.base_dir / "ui_assets" / "literature_packs",
            Path(r"D:\Eagle Literature Studio\packages"),
        ]
        for rel_d in self.catalog.get("auto_discover_pack_dirs") or []:
            p = self.base_dir / str(rel_d)
            if p not in scan_dirs:
                scan_dirs.append(p)

        discovered: dict[str, LiteraturePack] = {}
        for sdir in scan_dirs:
            if not sdir.is_dir():
                continue
            for child in sorted(sdir.iterdir()):
                if not child.is_dir():
                    continue
                m_path = child / "manifest.json"
                if not m_path.is_file():
                    continue
                try:
                    manifest = json.loads(m_path.read_text(encoding="utf-8"))
                    if manifest.get("schema_version") != "eagle_lit_v1":
                        continue
                    pack = LiteraturePack(child, manifest)
                    if pack.chapters and pack.work_id not in discovered:
                        discovered[pack.work_id] = pack
                except Exception:
                    continue

        self.packs = discovered

    def get_venue_works(self, venue_id: str) -> list[LiteraturePack]:
        """Returns the ordered list of works scheduled/shelved at `venue_id`."""
        if (time.time() - self._last_pack_scan_ts) > 10.0:
            self.reload_catalog_and_packs()

        v = self.venues.get(venue_id) or {}
        vtype = str(v.get("type") or "cinema").lower()
        tier = str(v.get("tier") or "GRAND").upper()

        explicit_ids: list[str] = []
        if vtype == "cinema":
            sched = (self.catalog.get("cinema_schedules") or {}).get(venue_id) or {}
            explicit_ids = list(sched.get("featured_works") or [])
        else:
            shelf = (self.catalog.get("library_shelves") or {}).get(venue_id) or {}
            explicit_ids = list(shelf.get("shelf_works") or [])

        result: list[LiteraturePack] = []
        seen: set[str] = set()
        for wid in explicit_ids:
            if wid in self.packs and wid not in seen:
                result.append(self.packs[wid])
                seen.add(wid)

        if self.catalog.get("auto_include_new_packs", True):
            for wid, pack in self.packs.items():
                if wid in seen:
                    continue
                if vtype == "cinema" and (pack.venue_type in ("cinema", "both") or "adv_cinema" in pack.supported_read_modes):
                    result.append(pack)
                    seen.add(wid)
                elif vtype == "library" and (pack.venue_type in ("library", "both") or "nvl_book" in pack.supported_read_modes):
                    result.append(pack)
                    seen.add(wid)

        if vtype == "cinema":
            max_slots = 5 if tier == "GRAND" else 2
            # Always keep _smoke_test_demo accessible for QA while respecting tier slots
            if len(result) > max_slots:
                result = result[:max_slots]
        return result

    def get_waifu_venue_hooks(self, venue_id: str) -> dict[str, Any]:
        """Exposes WaifuLink date hooks (`dropoff_coords`, `couple_seat_pool`, `post_date_lobby_chat_anchor`)."""
        v = self.venues.get(venue_id) or {}
        shell_id = str(v.get("interior_id") or v.get("interior_shell") or "INT-CIN-GRAND")
        layout = INTERIOR_SHELL_LAYOUTS.get(shell_id) or INTERIOR_SHELL_LAYOUTS["INT-CIN-GRAND"]
        anchors = layout.get("seat_anchors") or layout.get("table_anchors") or []
        couple_pool = [a["pos"] for a in anchors if a.get("couple")]
        dp = v.get("door_pos") or [1481.0, -1768.0, 18.8]
        drop = v.get("dropoff_pos") or dp
        return {
            "venue_id": venue_id,
            "dropoff_coords": (float(drop[0]), float(drop[1]), float(drop[2])),
            "door_coords": (float(dp[0]), float(dp[1]), float(dp[2])),
            "interior_shell": shell_id,
            "couple_seat_pool": couple_pool,
            "post_date_lobby_chat_anchor": layout.get("lounge_anchor"),
        }

    # ------------------------------------------------------------------------
    # DOUBLE-BUFFER CG RENDERWARE TXD STREAMING (`LITCG0.TXD` & `LITCG1.TXD`)
    # ------------------------------------------------------------------------
    @staticmethod
    def _build_lit_cg_txd_bytes(src_path: Path | None) -> bytes:
        """
        Packs a 16:9 image into a 1024x512 32-bit BGRA RenderWare `.TXD` (`ep_lit_cg`).
        Smart-crops non-16:9 images (e.g. 2:3 posters) with dark cinematic pillarbox bars
        so when drawn into a 16:9 screen quad (`drawW : drawH = 16 : 9`), zero stretching occurs!
        """
        w, h = 1024, 512
        target_aspect = 16.0 / 9.0
        if src_path is not None and src_path.is_file():
            with Image.open(src_path) as im:
                rgba_src = im.convert("RGBA")
                sw, sh = rgba_src.size
                if sw > 0 and sh > 0:
                    cur_aspect = sw / sh
                    if abs(cur_aspect - target_aspect) > 0.04:
                        # Fit inside 16:9 canvas with dark cinematic pillarbox/letterbox bars
                        canvas_w = 1376
                        canvas_h = 768
                        canvas = Image.new("RGBA", (canvas_w, canvas_h), (12, 16, 26, 255))
                        scale = min(canvas_w / sw, canvas_h / sh)
                        nw = max(1, int(round(sw * scale)))
                        nh = max(1, int(round(sh * scale)))
                        resized = rgba_src.resize((nw, nh), Image.Resampling.LANCZOS)
                        ox = (canvas_w - nw) // 2
                        oy = (canvas_h - nh) // 2
                        canvas.paste(resized, (ox, oy))
                        rgba_src = canvas
                rgba = rgba_src.resize((w, h), Image.Resampling.LANCZOS)
                bgra = rgba.tobytes("raw", "BGRA")
        else:
            bgra = b"\x18\x12\x0c\xff" * (w * h)

        rw_ver = 0x1803FFFF
        name_b = b"ep_lit_cg".ljust(32, b"\x00")
        mask_b = b"\x00" * 32
        tex_header = struct.pack(
            "<II32s32sIIHHBBBB",
            9,
            0x00001102,
            name_b,
            mask_b,
            0x00000500,
            0x00000015,
            w,
            h,
            32,
            1,
            4,
            0x01,
        )
        struct_body = tex_header + struct.pack("<I", len(bgra)) + bgra
        struct_chunk = struct.pack("<III", 0x01, len(struct_body), rw_ver) + struct_body
        ext_chunk = struct.pack("<III", 0x03, 0, rw_ver)
        native_body = struct_chunk + ext_chunk
        native_chunk = struct.pack("<III", 0x15, len(native_body), rw_ver) + native_body

        dict_struct_body = struct.pack("<HH", 1, 0)
        dict_struct = struct.pack("<III", 0x01, len(dict_struct_body), rw_ver) + dict_struct_body
        dict_body = dict_struct + native_chunk + ext_chunk
        return struct.pack("<III", 0x16, len(dict_body), rw_ver) + dict_body

    def _ensure_lit_cg_txd_slots(self) -> None:
        """Pre-seeds `models/txd/LITCG0.TXD` & `LITCG1.TXD` before `gta_pe.exe` boots."""
        try:
            txd_dir = self.base_dir / "models" / "txd"
            txd_dir.mkdir(parents=True, exist_ok=True)
            seed_candidates = [
                self.packs_dir / "_smoke_test_demo" / "cgs" / "cg_demo_01.jpg",
                Path(r"D:\GTA Archive\Staging\project-eagle\cinema-library-builder-20261106\packs\_smoke_test_demo\cgs\cg_demo_01.jpg"),
            ]
            seed_src = next((p for p in seed_candidates if p.is_file()), None)
            payload = None
            for slot in (0, 1):
                dst = txd_dir / f"LITCG{slot}.TXD"
                if not dst.is_file() or dst.stat().st_size != 2097320:
                    if payload is None:
                        payload = self._build_lit_cg_txd_bytes(seed_src)
                    dst.write_bytes(payload)
        except Exception:
            pass

    def _sync_dynamic_lit_cg_txd(self, cg_path: Path | None) -> tuple[str, str, str]:
        if cg_path is None or not cg_path.is_file():
            return ("0", str(self._cg_slot), str(self._cg_ver))
        p_str = str(cg_path.resolve())
        if p_str == self._cg_last_path and self._cg_ver > 0:
            return ("1", str(self._cg_slot), str(self._cg_ver))
        try:
            txd_bytes = self._build_lit_cg_txd_bytes(cg_path)
            next_slot = 1 - int(self._cg_slot)
            txd_dir = self.base_dir / "models" / "txd"
            txd_dir.mkdir(parents=True, exist_ok=True)
            dst_txd = txd_dir / f"LITCG{next_slot}.TXD"
            dst_txd.write_bytes(txd_bytes)
            self._cg_slot = next_slot
            self._cg_ver += 1
            self._cg_last_path = p_str
            return ("1", str(self._cg_slot), str(self._cg_ver))
        except Exception:
            return ("0", str(self._cg_slot), str(self._cg_ver))

    # ------------------------------------------------------------------------
    # CLEO SHARED MEMORY BRIDGE (`0x00CAFE50`) & TELEPORT HELPERS
    # ------------------------------------------------------------------------
    def _get_cleo_shm_ptr(self, bridge: Any) -> int:
        if bridge is None or not getattr(bridge, "handle", None):
            return 0
        magic = bridge.read_u32(CAFE_LIT_MAGIC_ADDR)
        if magic != LIT_MAGIC:
            return 0
        ptr = bridge.read_u32(CAFE_LIT_PTR_ADDR) or 0
        return ptr if ptr > 0x00400000 else 0

    def _send_cleo_teleport_and_npcs(
        self,
        bridge: Any,
        cmd_type: int,
        area_code: int,
        x: float,
        y: float,
        z: float,
        heading: float,
        npc_list: list[tuple] | None = None,
    ) -> None:
        """
        Executes both direct RAM teleport (`bridge.teleport_player`) AND CLEO native
        `02EB + 04E4 + 04BB + 0860 + 00A1 + 0173 + 0373 + 24-NPC Crowd Spawn/Despawn` via `0x00CAFE50`.
        """
        if bridge is not None and hasattr(bridge, "teleport_player"):
            try:
                bridge.teleport_player(float(x), float(y), float(z), area_code=int(area_code))
            except Exception:
                pass

        shm_ptr = self._get_cleo_shm_ptr(bridge)
        if not shm_ptr:
            return

        npcs = (npc_list or [])[:24]
        bridge.write_i32(shm_ptr + 192, len(npcs))
        if npcs:
            npc_buf = bytearray()
            for item in npcs:
                if len(item) >= 6:
                    ped_id, nx, ny, nz, nh, pose = item[:6]
                else:
                    ped_id, nx, ny, nz, nh = item[:5]
                    pose = 0
                npc_buf += struct.pack(
                    "<iffffi",
                    int(ped_id),
                    float(nx),
                    float(ny),
                    float(nz),
                    float(nh),
                    int(pose),
                )
            bridge.write_bytes(shm_ptr + 196, bytes(npc_buf))

        self._cleo_cmd_seq = (self._cleo_cmd_seq + 1) & 0x7FFFFFFF or 1
        hdr = struct.pack(
            "<iiffff",
            int(cmd_type),
            int(area_code),
            float(x),
            float(y),
            float(z),
            float(heading),
        )
        bridge.write_bytes(shm_ptr + 16, hdr)
        bridge.write_i32(shm_ptr + 8, self._cleo_cmd_seq)

    def _update_cleo_coronas(self, bridge: Any, coronas: list[tuple[float, float, float, int, int, int]]) -> None:
        shm_ptr = self._get_cleo_shm_ptr(bridge)
        if not shm_ptr:
            return
        c_list = coronas[:6]
        buf = bytearray()
        for cx, cy, cz, cr, cg, cb in c_list:
            buf += struct.pack("<fffiii", float(cx), float(cy), float(cz), int(cr), int(cg), int(cb))
        if buf:
            bridge.write_bytes(shm_ptr + 48, bytes(buf))
        bridge.write_i32(shm_ptr + 44, len(c_list))

    # ------------------------------------------------------------------------
    # GAME CLOCK & SHOWTIME WINDOW LOGIC (`±60` MINUTES)
    # ------------------------------------------------------------------------
    @staticmethod
    def _read_player_snapshot(bridge: Any) -> dict[str, Any] | None:
        if bridge is None:
            return None
        if hasattr(bridge, "read_live_player"):
            return bridge.read_live_player()
        if hasattr(bridge, "get_player_snapshot"):
            return bridge.get_player_snapshot()
        return None

    def get_game_clock(self, bridge: Any) -> tuple[int, int, int]:
        """Returns `(hour, minute, day_counter)` from GTA SA RAM."""
        if bridge is not None and getattr(bridge, "handle", None):
            h = bridge.read_u8(ADDR_CLOCK_HOURS)
            m = bridge.read_u8(ADDR_CLOCK_MINS)
            d = 1
            if hasattr(bridge, "read_u16"):
                d = bridge.read_u16(ADDR_CLOCK_DAYS) or 1
            elif hasattr(bridge, "read_bytes"):
                raw_d = bridge.read_bytes(ADDR_CLOCK_DAYS, 2)
                if raw_d and len(raw_d) == 2:
                    d = struct.unpack("<H", raw_d)[0] or 1
            if h is not None and m is not None:
                return (int(h) % 24, int(m) % 60, int(d or 1))
        return (20, 15, 1)

    def set_game_clock(self, bridge: Any, hour: int, minute: int) -> None:
        if bridge is not None and getattr(bridge, "handle", None):
            bridge.write_bytes(ADDR_CLOCK_HOURS, struct.pack("<B", int(hour) % 24))
            bridge.write_bytes(ADDR_CLOCK_MINS, struct.pack("<B", int(minute) % 60))

    def evaluate_showtime_status(self, now_h: int, now_m: int, showtime_hhmm: str) -> dict[str, Any]:
        """
        Checks if `now_h:now_m` is within the flexible `±60` minute entry window of `showtime_hhmm`.
        """
        now_total = (int(now_h) % 24) * 60 + (int(now_m) % 60)
        st_total = _parse_hhmm_to_minutes(showtime_hhmm)
        diff = _circular_minute_diff(now_total, st_total)
        grace = int((self.catalog.get("showtime_policy") or {}).get("entry_grace_window_minutes") or 60)
        in_window = abs(diff) <= grace
        if in_window:
            status = "OPEN NOW (+/-60m)"
        elif diff > grace:
            status = "MISSED (FREE EXCHANGE)"
        else:
            status = f"IN {-diff}m"
        return {
            "showtime": showtime_hhmm,
            "in_window": in_window,
            "diff_minutes": diff,
            "status": status,
        }

    def find_best_showtime_index(self, now_h: int, now_m: int, showtimes: list[str] | None = None) -> int:
        sts = showtimes or SHOWTIMES_DEFAULT
        now_total = (int(now_h) % 24) * 60 + (int(now_m) % 60)
        best_idx = 0
        best_score = 999999
        for i, st in enumerate(sts):
            diff = _circular_minute_diff(now_total, _parse_hhmm_to_minutes(st))
            if abs(diff) <= 60:
                return i
            # Prefer nearest upcoming showtime
            wait_min = (-diff) if diff < 0 else (1440 - diff)
            if wait_min < best_score:
                best_score = wait_min
                best_idx = i
        return best_idx

    # ------------------------------------------------------------------------
    # STATE MACHINE TRANSITIONS (OUTSIDE <-> LOBBY <-> AUDITORIUM <-> READER)
    # ------------------------------------------------------------------------
    def _set_banner(self, msg: str, duration: float = 4.5) -> None:
        self.status_banner = clean_cfont_ascii(msg)
        self.status_banner_until = time.time() + duration

    def enter_venue_from_street(self, engine: Any, venue_id: str) -> bool:
        v = self.venues.get(venue_id)
        if not v:
            return False
        br = getattr(engine, "bridge", None)
        snap = self._read_player_snapshot(br)
        if snap and snap.get("pos"):
            px, py, pz = snap["pos"]
            self.return_xyz = (float(px), float(py), float(pz))
            self.return_heading = float(snap.get("heading") or 0.0)
        else:
            dp = v.get("door_pos") or [1481.0, -1768.0, 18.8]
            self.return_xyz = (float(dp[0]), float(dp[1]), float(dp[2]))
            self.return_heading = float(v.get("heading") or 0.0)

        self.active_venue_id = venue_id
        self.active_shell_id = str(
            v.get("interior_id")
            or v.get("interior_shell")
            or ("INT-CIN-GRAND" if str(v.get("type")).lower() == "cinema" else "INT-LIB-GRAND")
        )
        layout = INTERIOR_SHELL_LAYOUTS.get(self.active_shell_id) or INTERIOR_SHELL_LAYOUTS["INT-CIN-GRAND"]
        npcs = INTERIOR_NPCS_BY_SHELL.get(self.active_shell_id) or []

        self.counter_selected_idx = 0
        now_h, now_m, _ = self.get_game_clock(br)
        self.counter_showtime_idx = self.find_best_showtime_index(now_h, now_m, SHOWTIMES_DEFAULT)

        if str(v.get("type")).lower() == "cinema":
            sx, sy, sz, sh = layout["lobby_spawn"]
            self.state = "CINEMA_LOBBY"
            self._send_cleo_teleport_and_npcs(br, 1, 1, sx, sy, sz, sh, npcs)
            self._set_banner(f"Entered {v.get('name_en', venue_id)} Lobby. Visit Box Office [E] to buy tickets!")
        else:
            sx, sy, sz, sh = layout["hall_spawn"]
            self.state = "LIBRARY_HALL"
            self._send_cleo_teleport_and_npcs(br, 1, 1, sx, sy, sz, sh, npcs)
            self._set_banner(f"Entered {v.get('name_en', venue_id)}. Visit Librarian Counter [E] to rent or buy books!")
        return True

    def exit_venue_to_street(self, engine: Any) -> bool:
        self.close_reader(engine)
        br = getattr(engine, "bridge", None)
        rx, ry, rz = self.return_xyz
        rh = self.return_heading
        v = self.venues.get(self.active_venue_id) or {}
        v_name = clean_cfont_ascii(v.get("name_en") or self.active_venue_id or "Street")
        self.state = "OUTSIDE"
        self.active_venue_id = ""
        self.active_shell_id = ""
        self._send_cleo_teleport_and_npcs(br, 3, 0, rx, ry, rz, rh, [])
        self._set_banner(f"Exited to {v_name}.")
        return True

    def enter_cinema_auditorium(self, engine: Any) -> bool:
        """
        Transitions from `CINEMA_LOBBY` -> `CINEMA_AUDITORIUM` after validating the player's
        ticket and `±60` minute showtime window (with automatic FREE exchange if missed!).
        """
        if self.state not in ("CINEMA_LOBBY", "MODAL_TICKET_COUNTER"):
            return False
        br = getattr(engine, "bridge", None)
        works = self.get_venue_works(self.active_venue_id)
        if not works:
            self._set_banner("No movies scheduled at this cinema.")
            return False

        tickets = self.save_data.setdefault("active_tickets", {})
        t_info = tickets.get(self.active_venue_id)
        if not t_info or t_info.get("work_id") not in self.packs:
            self._set_banner("Please buy a movie ticket at the Box Office [E] before entering the Auditorium!")
            return False

        now_h, now_m, _ = self.get_game_clock(br)
        st_eval = self.evaluate_showtime_status(now_h, now_m, str(t_info.get("showtime") or "20:30"))
        if not st_eval["in_window"]:
            best_idx = self.find_best_showtime_index(now_h, now_m, SHOWTIMES_DEFAULT)
            new_st = SHOWTIMES_DEFAULT[best_idx]
            if st_eval["diff_minutes"] > 60:
                # Missed earlier showtime -> FREE exchange to next showtime + sync clock to screening start!
                t_info["showtime"] = new_st
                self._write_save()
                sh_h, sh_m = [int(x) for x in new_st.split(":")]
                self.set_game_clock(br, sh_h, sh_m)
                self._set_banner(f"Missed earlier slot -> FREE Exchange ($0) to {new_st} screening!")
            else:
                # Arrived early before -60m window -> sync clock to ticket showtime so screening starts now!
                sh_h, sh_m = [int(x) for x in str(t_info.get("showtime") or "20:30").split(":")]
                self.set_game_clock(br, sh_h, sh_m)
                self._set_banner(f"Doors opened for {t_info.get('showtime')} screening! Walk to any seat [E].")
        else:
            self._set_banner(f"Ticket verified ({t_info.get('showtime')}). Walk to any Auditorium seat and press [E]!")

        layout = INTERIOR_SHELL_LAYOUTS.get(self.active_shell_id) or INTERIOR_SHELL_LAYOUTS["INT-CIN-GRAND"]
        ax, ay, az, ah = layout["auditorium_spawn"]
        self.state = "CINEMA_AUDITORIUM"
        self.active_work_id = str(t_info["work_id"])
        self._send_cleo_teleport_and_npcs(br, 2, 1, ax, ay, az, ah)
        return True

    def exit_auditorium_to_lobby(self, engine: Any) -> bool:
        """
        Walks out the green `EXIT` door of the Auditorium back into `CINEMA_LOBBY`.
        """
        if self.state != "CINEMA_AUDITORIUM":
            return False
        br = getattr(engine, "bridge", None)
        layout = INTERIOR_SHELL_LAYOUTS.get(self.active_shell_id) or INTERIOR_SHELL_LAYOUTS["INT-CIN-GRAND"]
        lx, ly, lz, lh = layout["auditorium_return_lobby"]
        self.state = "CINEMA_LOBBY"
        self._send_cleo_teleport_and_npcs(br, 2, 1, lx, ly, lz, lh)
        self._set_banner("Returned to Cinema Lobby. Walk to Main Exit [E] to return to the street.")
        return True

    # ------------------------------------------------------------------------
    # COUNTER ACTIONS (BUY TICKET / POPCORN / RENT BOOK / BUY BOOK)
    # ------------------------------------------------------------------------
    def buy_or_exchange_cinema_ticket(self, engine: Any) -> bool:
        works = self.get_venue_works(self.active_venue_id)
        if not works:
            return False
        pack = works[self.counter_selected_idx % len(works)]
        showtime = SHOWTIMES_DEFAULT[self.counter_showtime_idx % len(SHOWTIMES_DEFAULT)]
        v = self.venues.get(self.active_venue_id) or {}
        tier = str(v.get("tier") or "GRAND").upper()
        price = 25 if tier == "GRAND" else 12

        br = getattr(engine, "bridge", None)
        now_h, now_m, now_day = self.get_game_clock(br)
        tickets = self.save_data.setdefault("active_tickets", {})
        existing = tickets.get(self.active_venue_id)

        # Free exchange if player already bought a ticket at this cinema today!
        is_free_exchange = bool(existing and existing.get("work_id") == pack.work_id)
        if not is_free_exchange:
            self._deduct_cash(engine, price)

        tickets[self.active_venue_id] = {
            "work_id": pack.work_id,
            "title": pack.title,
            "showtime": showtime,
            "day": now_day,
            "tier": tier,
        }
        self.active_work_id = pack.work_id
        self._write_save()

        # Sync preview CG to movie poster/banner
        self._sync_dynamic_lit_cg_txd(pack.resolve_cover_cg_path())

        # If chosen showtime is outside +/-60m right now, sync game clock to showtime so player can walk right in
        st_eval = self.evaluate_showtime_status(now_h, now_m, showtime)
        if not st_eval["in_window"]:
            sh_h, sh_m = [int(x) for x in showtime.split(":")]
            self.set_game_clock(br, sh_h, sh_m)

        if is_free_exchange:
            self._set_banner(f"FREE Showtime Exchange ($0): {pack.title} @ {showtime}! Proceed to Auditorium Door [E].")
        else:
            self._set_banner(f"Bought Ticket (${price}): {pack.title} @ {showtime}! Proceed to Auditorium Door [E].")
        return True

    def buy_popcorn_combo(self, engine: Any) -> bool:
        v = self.venues.get(self.active_venue_id) or {}
        tier = str(v.get("tier") or "GRAND").upper()
        price = 10 if tier == "GRAND" else 6
        self._deduct_cash(engine, price)
        self.has_popcorn_combo = True
        self.save_data.setdefault("stats", {})["popcorn_combos"] = int(self.save_data.get("stats", {}).get("popcorn_combos", 0)) + 1
        self._write_save()
        br = getattr(engine, "bridge", None)
        if br and hasattr(br, "add_health"):
            br.add_health(50.0, set_full=True)
        self._set_banner(f"Purchased Caramel Popcorn & Large Soda Combo (-${price}, HP Restored)!")
        return True

    def rent_or_buy_library_book(self, engine: Any, permanent_buy: bool = False) -> bool:
        works = self.get_venue_works(self.active_venue_id)
        if not works:
            return False
        pack = works[self.counter_selected_idx % len(works)]
        v = self.venues.get(self.active_venue_id) or {}
        tier = str(v.get("tier") or "GRAND").upper()
        br = getattr(engine, "bridge", None)
        _, _, now_day = self.get_game_clock(br)

        owned_list: list[str] = self.save_data.setdefault("owned_books", [])
        rented_map: dict[str, int] = self.save_data.setdefault("rented_books", {})

        if permanent_buy:
            if pack.work_id in owned_list:
                self.active_work_id = pack.work_id
                self._set_banner(f"You already own '{pack.title}'! Walk to any Reading Table [E] or open EaglePhone.")
                return True
            price = max(20, min(60, int(pack.library_buy_usd or (35 if tier == "GRAND" else 25))))
            self._deduct_cash(engine, price)
            owned_list.append(pack.work_id)
            self.active_work_id = pack.work_id
            self._write_save()
            self._set_banner(f"Purchased '{pack.title}' (-${price})! Added to EaglePhone Bookshelf & ready at Reading Tables [E].")
            return True
        else:
            if pack.work_id in owned_list or int(rented_map.get(pack.work_id, -1)) >= now_day:
                self.active_work_id = pack.work_id
                self._set_banner(f"Selected '{pack.title}'. Walk to any Reading Table and press [E] to read!")
                return True
            price = 8 if tier == "GRAND" else 5
            self._deduct_cash(engine, price)
            rented_map[pack.work_id] = now_day + 7
            self.active_work_id = pack.work_id
            self._write_save()
            self._set_banner(f"Rented '{pack.title}' for 7 Game Days (-${price})! Sit at any Reading Table [E] to read.")
            return True

    def buy_library_coffee(self, engine: Any) -> bool:
        v = self.venues.get(self.active_venue_id) or {}
        tier = str(v.get("tier") or "GRAND").upper()
        price = 5 if tier == "GRAND" else 4
        self._deduct_cash(engine, price)
        self.has_coffee_buff = True
        self.save_data.setdefault("stats", {})["coffee_combos"] = int(self.save_data.get("stats", {}).get("coffee_combos", 0)) + 1
        self._write_save()
        br = getattr(engine, "bridge", None)
        if br and hasattr(br, "add_health"):
            br.add_health(35.0, set_full=True)
        self._set_banner(f"Ordered Artisan Espresso & Pastry (-${price}, HP Restored)!")
        return True

    @staticmethod
    def _deduct_cash(engine: Any, amount: int) -> None:
        if amount <= 0:
            return
        br = getattr(engine, "bridge", None)
        if br and hasattr(br, "add_money"):
            br.add_money(-int(amount))
        if hasattr(engine, "cash"):
            try:
                engine.cash = max(0, int(engine.cash) - int(amount))
            except Exception:
                pass

    # ------------------------------------------------------------------------
    # DUAL-MODE READER (`adv_cinema` & `nvl_book`) + BOOKMARK PERSISTENCE
    # ------------------------------------------------------------------------
    def open_reader(
        self,
        engine: Any,
        work_id: str | None = None,
        mode: str | None = None,
        return_state: str | None = None,
        resume_bookmark: bool = True,
    ) -> bool:
        wid = str(work_id or self.active_work_id or "").strip()
        if wid not in self.packs:
            works = self.get_venue_works(self.active_venue_id)
            if works:
                wid = works[0].work_id
            elif self.packs:
                wid = next(iter(self.packs.keys()))
        if wid not in self.packs:
            return False

        pack = self.packs[wid]
        self.active_work_id = wid
        self.return_state_after_reader = return_state or (
            "CINEMA_AUDITORIUM"
            if self.state == "CINEMA_AUDITORIUM"
            else ("LIBRARY_HALL" if self.state in ("LIBRARY_HALL", "MODAL_LIBRARY_COUNTER") else "OUTSIDE")
        )

        # Save exact seat/table position before opening HUD so closing HUD leaves Aether right there!
        br = getattr(engine, "bridge", None)
        if br:
            br.write_i32(0x00CAFE78, 0)
            snap = self._read_player_snapshot(br)
            if snap and snap.get("pos"):
                px, py, pz = snap["pos"]
                self.seat_return_xyz = (float(px), float(py), float(pz))

        target_mode = str(mode or ("adv_cinema" if self.return_state_after_reader == "CINEMA_AUDITORIUM" else "nvl_book")).lower()
        self.reader_mode = target_mode
        self.state = "READING_ADV_CINEMA" if target_mode == "adv_cinema" else "READING_NVL_BOOK"
        self.ui_hidden = False
        self.backlog_open = False
        self.toc_open = False
        self.auto_speed_idx = 0
        self.reader_opened_at = time.time()

        # Restore bookmark if available
        bm = (self.save_data.get("bookmarks") or {}).get(wid) if resume_bookmark else None
        self.active_chapter_idx = 0
        self.active_beat_idx = 0
        self.active_block_idx = 0
        if bm and isinstance(bm, dict):
            ch_id = bm.get("chapter_id")
            for idx, ch_m in enumerate(pack.chapters):
                if ch_m.get("chapter_id") == ch_id:
                    self.active_chapter_idx = idx
                    break
            ch_data = pack.get_indexed_chapter(pack.chapters[self.active_chapter_idx]["chapter_id"])
            if target_mode == "nvl_book" and bm.get("block_id"):
                for b_i, blk in enumerate(ch_data["flat_blocks"]):
                    if blk["block_id"] == bm.get("block_id"):
                        self.active_block_idx = b_i
                        self.active_beat_idx = int(blk["first_beat_idx"])
                        break
            elif bm.get("beat_id"):
                for bt_i, bt in enumerate(ch_data["flat_beats"]):
                    if bt["beat_id"] == bm.get("beat_id"):
                        self.active_beat_idx = bt_i
                        self.active_block_idx = int(bt["block_idx"])
                        break
            else:
                self.active_block_idx = max(0, min(len(ch_data["flat_blocks"]) - 1, int(bm.get("block_idx", 0))))
                self.active_beat_idx = max(0, min(len(ch_data["flat_beats"]) - 1, int(bm.get("beat_idx", 0))))

        # Mute GTA ambient audio and start pack BGM
        if self.audio_muter is not None:
            try:
                self.audio_muter.mute()
            except Exception:
                pass

        self._sync_active_reader_media()
        self.save_bookmark()
        return True

    def save_bookmark(self) -> None:
        if not self.active_work_id or self.active_work_id not in self.packs:
            return
        pack = self.packs[self.active_work_id]
        if not pack.chapters:
            return
        ch_idx = max(0, min(len(pack.chapters) - 1, self.active_chapter_idx))
        ch_id = pack.chapters[ch_idx]["chapter_id"]
        ch_data = pack.get_indexed_chapter(ch_id)
        beats = ch_data["flat_beats"]
        blocks = ch_data["flat_blocks"]
        bt_idx = max(0, min(len(beats) - 1, self.active_beat_idx)) if beats else 0
        bk_idx = max(0, min(len(blocks) - 1, self.active_block_idx)) if blocks else 0

        total_ch = max(1, len(pack.chapters))
        ch_frac = (bt_idx + 1) / max(1, len(beats)) if self.reader_mode == "adv_cinema" else (bk_idx + 1) / max(1, len(blocks))
        pct = round(((ch_idx + ch_frac) / total_ch) * 100.0, 1)

        self.save_data.setdefault("bookmarks", {})[self.active_work_id] = {
            "work_id": self.active_work_id,
            "chapter_id": ch_id,
            "chapter_index": ch_idx + 1,
            "beat_idx": bt_idx,
            "beat_id": beats[bt_idx]["beat_id"] if beats else "",
            "block_idx": bk_idx,
            "block_id": blocks[bk_idx]["block_id"] if blocks else "",
            "last_mode": self.reader_mode,
            "progress_pct": pct,
            "updated_at": int(time.time()),
        }
        self._write_save()

    def close_reader(self, engine: Any = None, finished_work: bool = False) -> None:
        """
        Closes the `adv_cinema` or `nvl_book` HUD, unmutes GTA audio, stops `lit_bgm`,
        saves the bookmark, and leaves Aether at his exact 3D seat/table inside the interior!
        """
        if self.state in ("READING_ADV_CINEMA", "READING_NVL_BOOK"):
            self.save_bookmark()
            if finished_work:
                st = self.save_data.setdefault("stats", {})
                if self.reader_mode == "adv_cinema":
                    st["movies_watched"] = int(st.get("movies_watched", 0)) + 1
                else:
                    st["books_read"] = int(st.get("books_read", 0)) + 1
                self._write_save()

            self.state = self.return_state_after_reader
            if self.state == "CINEMA_AUDITORIUM":
                self._set_banner("Screening paused/finished. You are still at your Auditorium seat — walk to green EXIT door [E] to return to Lobby.")
            elif self.state == "LIBRARY_HALL":
                self._set_banner("Book closed & bookmarked! Walk to Main Exit [E] to leave or press [E] at table to resume.")

        self.bgm_worker.stop()
        if self.audio_muter is not None:
            try:
                self.audio_muter.unmute()
            except Exception:
                pass
        if engine is not None and hasattr(engine, "_update_touch_capture"):
            try:
                engine._update_touch_capture(bool(getattr(engine, "phone_open", 0) == 1 and getattr(engine, "touch_mode", False)))
            except Exception:
                pass

    def shutdown(self) -> None:
        try:
            self.bgm_worker.stop()
        except Exception:
            pass
        if self.audio_muter is not None:
            try:
                self.audio_muter.unmute()
            except Exception:
                pass

    def _sync_active_reader_media(self) -> None:
        if self.active_work_id not in self.packs:
            return
        pack = self.packs[self.active_work_id]
        if not pack.chapters:
            return
        self.active_chapter_idx = max(0, min(len(pack.chapters) - 1, self.active_chapter_idx))
        ch_id = pack.chapters[self.active_chapter_idx]["chapter_id"]
        ch_data = pack.get_indexed_chapter(ch_id)
        beats = ch_data["flat_beats"]
        blocks = ch_data["flat_blocks"]

        cg_id = None
        bgm_id = None
        words = 12
        if self.reader_mode == "adv_cinema" and beats:
            self.active_beat_idx = max(0, min(len(beats) - 1, self.active_beat_idx))
            bt = beats[self.active_beat_idx]
            self.active_block_idx = int(bt["block_idx"])
            cg_id = bt.get("cg_id")
            bgm_id = bt.get("bgm_id")
            words = int(bt.get("words") or 12)
        elif blocks:
            self.active_block_idx = max(0, min(len(blocks) - 1, self.active_block_idx))
            blk = blocks[self.active_block_idx]
            self.active_beat_idx = int(blk["first_beat_idx"])
            cg_id = blk.get("cg_id")
            bgm_id = blk.get("bgm_id")
            words = int(blk.get("words") or 20)

        cg_path = pack.resolve_cg_path(cg_id)
        self._sync_dynamic_lit_cg_txd(cg_path)

        bgm_path, _, loop, vol = pack.resolve_bgm_info(bgm_id)
        if bgm_path is not None:
            self.bgm_worker.play(bgm_path, loop=loop, volume_pct=vol)

        if self.auto_speed_idx > 0:
            mult = AUTO_SPEED_MULTS[self.auto_speed_idx % len(AUTO_SPEED_MULTS)]
            delay = max(2.0, (1.6 + words * 0.24) / max(0.5, mult))
            self.next_auto_beat_ts = time.time() + delay

    def step_reader(self, engine: Any, delta: int) -> None:
        if self.active_work_id not in self.packs:
            return
        pack = self.packs[self.active_work_id]
        if not pack.chapters:
            return
        ch_id = pack.chapters[self.active_chapter_idx]["chapter_id"]
        ch_data = pack.get_indexed_chapter(ch_id)
        beats = ch_data["flat_beats"]
        blocks = ch_data["flat_blocks"]

        if self.reader_mode == "adv_cinema":
            new_idx = self.active_beat_idx + int(delta)
            if new_idx >= len(beats):
                if self.active_chapter_idx + 1 < len(pack.chapters):
                    self.active_chapter_idx += 1
                    self.active_beat_idx = 0
                    self.active_block_idx = 0
                else:
                    # End of final chapter -> close HUD cleanly while keeping Aether inside Auditorium!
                    self.close_reader(engine, finished_work=True)
                    return
            elif new_idx < 0:
                if self.active_chapter_idx > 0:
                    self.active_chapter_idx -= 1
                    prev_ch = pack.get_indexed_chapter(pack.chapters[self.active_chapter_idx]["chapter_id"])
                    self.active_beat_idx = max(0, len(prev_ch["flat_beats"]) - 1)
                else:
                    self.active_beat_idx = 0
            else:
                self.active_beat_idx = new_idx
        else:
            new_blk = self.active_block_idx + int(delta)
            if new_blk >= len(blocks):
                if self.active_chapter_idx + 1 < len(pack.chapters):
                    self.active_chapter_idx += 1
                    self.active_block_idx = 0
                    self.active_beat_idx = 0
                else:
                    self.active_block_idx = max(0, len(blocks) - 1)
            elif new_blk < 0:
                if self.active_chapter_idx > 0:
                    self.active_chapter_idx -= 1
                    prev_ch = pack.get_indexed_chapter(pack.chapters[self.active_chapter_idx]["chapter_id"])
                    self.active_block_idx = max(0, len(prev_ch["flat_blocks"]) - 1)
                else:
                    self.active_block_idx = 0
            else:
                self.active_block_idx = new_blk

        self._sync_active_reader_media()
        self.save_bookmark()

    def jump_to_chapter(self, chapter_idx: int) -> None:
        if self.active_work_id not in self.packs:
            return
        pack = self.packs[self.active_work_id]
        if not pack.chapters:
            return
        self.active_chapter_idx = max(0, min(len(pack.chapters) - 1, int(chapter_idx)))
        self.active_beat_idx = 0
        self.active_block_idx = 0
        self.toc_open = False
        self._sync_active_reader_media()
        self.save_bookmark()

    def toggle_reader_mode(self) -> None:
        if self.reader_mode == "adv_cinema":
            self.reader_mode = "nvl_book"
            self.state = "READING_NVL_BOOK"
        else:
            self.reader_mode = "adv_cinema"
            self.state = "READING_ADV_CINEMA"
        self._sync_active_reader_media()
        self.save_bookmark()

    def open_phone_bookshelf_reader(self, engine: Any, work_id: str | None = None) -> bool:
        """
        Opens a permanently purchased book (`owned_books`) from the player's EaglePhone Personal Bookshelf
        in `nvl_book` mode, automatically resuming from its saved `bookmark`!
        """
        owned = list(self.save_data.get("owned_books") or [])
        target_wid = str(work_id or "").strip()
        if not target_wid or target_wid not in owned:
            target_wid = next((w for w in owned if w in self.packs), "")
        if not target_wid and self.packs:
            target_wid = next(iter(self.packs.keys()))
        if not target_wid:
            return False
        cur_return = self.state if self.state in ("CINEMA_LOBBY", "CINEMA_AUDITORIUM", "LIBRARY_HALL") else "OUTSIDE"
        return self.open_reader(engine, work_id=target_wid, mode="nvl_book", return_state=cur_return, resume_bookmark=True)

    def handle_touch(self, engine: Any, item_idx: int, action: int) -> bool:
        """Handles Touchscreen / Mouse Click (`app_id == 19`) from `EagleLiteratureHud.lua`."""
        idx = int(item_idx)
        # 101: Next Beat/Page, 102: Prev Beat/Page, 103: Auto-Play, 104: Hide UI,
        # 105: Backlog, 106: TOC, 107: Mode Switch [V], 108: Close Reader/Modal
        if self.state in ("READING_ADV_CINEMA", "READING_NVL_BOOK"):
            if idx == 101:
                if self.ui_hidden:
                    self.ui_hidden = False
                else:
                    self.step_reader(engine, +1)
                return True
            if idx == 102:
                self.step_reader(engine, -1)
                return True
            if idx == 103:
                self.auto_speed_idx = (self.auto_speed_idx + 1) % len(AUTO_SPEED_LABELS)
                self._sync_active_reader_media()
                return True
            if idx == 104:
                self.ui_hidden = not self.ui_hidden
                return True
            if idx == 105:
                self.backlog_open = not self.backlog_open
                self.toc_open = False
                return True
            if idx == 106:
                self.toc_open = not self.toc_open
                self.backlog_open = False
                return True
            if idx == 107:
                self.toggle_reader_mode()
                return True
            if idx == 108:
                if self.backlog_open:
                    self.backlog_open = False
                elif self.toc_open:
                    self.toc_open = False
                else:
                    self.close_reader(engine, finished_work=False)
                return True
            if 200 <= idx < 208:
                self.jump_to_chapter(idx - 200)
                return True
        elif self.state in ("MODAL_TICKET_COUNTER", "MODAL_LIBRARY_COUNTER"):
            works = max(1, len(self.get_venue_works(self.active_venue_id)))
            if 0 <= idx < 5:
                self.counter_selected_idx = idx % works
                self._preview_selected_counter_work()
                if action == 2:
                    if self.state == "MODAL_TICKET_COUNTER":
                        self.buy_or_exchange_cinema_ticket(engine)
                        self.state = "CINEMA_LOBBY"
                    else:
                        self.rent_or_buy_library_book(engine, permanent_buy=False)
                        self.state = "LIBRARY_HALL"
                return True
            if idx == 108:
                self.state = "CINEMA_LOBBY" if self.state == "MODAL_TICKET_COUNTER" else "LIBRARY_HALL"
                return True
        return False

    def handle_api_command(self, engine: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """HTTP / QA automation endpoint (`/api/literature/cmd`)."""
        cmd = str(payload.get("cmd") or "").strip().lower()
        vid = str(payload.get("venue_id") or self.active_venue_id or "CIN-01").strip()
        wid = str(payload.get("work_id") or self.active_work_id or "").strip()

        if cmd == "enter_venue":
            ok = self.enter_venue_from_street(engine, vid)
        elif cmd == "open_counter":
            v = self.venues.get(self.active_venue_id or vid) or {}
            self.state = "MODAL_TICKET_COUNTER" if str(v.get("type")).lower() == "cinema" else "MODAL_LIBRARY_COUNTER"
            self._preview_selected_counter_work()
            ok = True
        elif cmd == "buy_ticket":
            ok = self.buy_or_exchange_cinema_ticket(engine)
            self.state = "CINEMA_LOBBY"
        elif cmd == "buy_popcorn":
            ok = self.buy_popcorn_combo(engine)
        elif cmd == "enter_auditorium":
            ok = self.enter_cinema_auditorium(engine)
        elif cmd == "sit_watch":
            ok = self.open_reader(engine, work_id=wid or None, mode="adv_cinema", return_state="CINEMA_AUDITORIUM")
        elif cmd == "rent_book":
            ok = self.rent_or_buy_library_book(engine, permanent_buy=False)
            self.state = "LIBRARY_HALL"
        elif cmd == "buy_book":
            ok = self.rent_or_buy_library_book(engine, permanent_buy=True)
            self.state = "LIBRARY_HALL"
        elif cmd == "buy_coffee":
            ok = self.buy_library_coffee(engine)
        elif cmd == "sit_read":
            ok = self.open_reader(engine, work_id=wid or None, mode="nvl_book", return_state="LIBRARY_HALL")
        elif cmd == "open_phone_bookshelf":
            ok = self.open_phone_bookshelf_reader(engine, work_id=wid or None)
        elif cmd == "step_reader":
            self.step_reader(engine, int(payload.get("delta", 1)))
            ok = True
        elif cmd == "toggle_mode":
            self.toggle_reader_mode()
            ok = True
        elif cmd == "toggle_backlog":
            self.backlog_open = not self.backlog_open
            self.toc_open = False
            ok = True
        elif cmd == "toggle_toc":
            self.toc_open = not self.toc_open
            self.backlog_open = False
            ok = True
        elif cmd == "toggle_hide_ui":
            self.ui_hidden = not self.ui_hidden
            ok = True
        elif cmd == "cycle_auto":
            self.auto_speed_idx = (self.auto_speed_idx + 1) % len(AUTO_SPEED_LABELS)
            self._sync_active_reader_media()
            ok = True
        elif cmd == "close_reader":
            self.close_reader(engine, finished_work=bool(payload.get("finished", False)))
            ok = True
        elif cmd == "exit_auditorium":
            ok = self.exit_auditorium_to_lobby(engine)
        elif cmd == "exit_venue":
            ok = self.exit_venue_to_street(engine)
        elif cmd == "reload_packs":
            self.reload_catalog_and_packs()
            ok = True
        elif cmd == "select_item":
            works = max(1, len(self.get_venue_works(self.active_venue_id or vid)))
            self.counter_selected_idx = int(payload.get("index", 0)) % works
            self._preview_selected_counter_work()
            ok = True
        elif cmd == "select_showtime":
            self.counter_showtime_idx = int(payload.get("index", 0)) % len(SHOWTIMES_DEFAULT)
            ok = True
        elif cmd == "jump_chapter":
            self.jump_to_chapter(int(payload.get("index", 0)))
            ok = True
        elif cmd == "cleo_teleport":
            br = getattr(engine, "bridge", None)
            t_area = int(payload.get("area", 0))
            tx = float(payload.get("x", 0.0))
            ty = float(payload.get("y", 0.0))
            tz = float(payload.get("z", 0.0))
            th = float(payload.get("h", 0.0))
            if payload.get("clear_banner"):
                self.banner_msg = ""
                self.banner_until = 0.0
            ok = self._send_cleo_teleport_and_npcs(br, 2, t_area, tx, ty, tz, th)
        elif cmd in ("status", ""):
            ok = True
        else:
            ok = False

        if cmd not in ("status", "") and hasattr(engine, "_write_hud_dat"):
            engine._write_hud_dat(force=True)

        return {
            "ok": bool(ok),
            "state": self.state,
            "active_venue_id": self.active_venue_id,
            "active_shell_id": self.active_shell_id,
            "active_work_id": self.active_work_id,
            "near_venue_id": self.near_venue_id,
            "near_anchor_kind": self.near_anchor_kind,
            "near_anchor_label": self.near_anchor_label,
            "return_xyz": list(self.return_xyz),
            "reader_mode": self.reader_mode,
            "active_chapter_idx": self.active_chapter_idx,
            "active_beat_idx": self.active_beat_idx,
            "active_block_idx": self.active_block_idx,
            "ui_hidden": self.ui_hidden,
            "backlog_open": self.backlog_open,
            "toc_open": self.toc_open,
            "auto_speed_idx": self.auto_speed_idx,
            "cg_slot": self._cg_slot,
            "cg_ver": self._cg_ver,
            "has_popcorn_combo": self.has_popcorn_combo,
            "has_coffee_buff": self.has_coffee_buff,
            "discovered_packs": list(self.packs.keys()),
            "active_tickets": self.save_data.get("active_tickets", {}),
            "rented_books": self.save_data.get("rented_books", {}),
            "bookmarks": self.save_data.get("bookmarks", {}),
            "owned_books": self.save_data.get("owned_books", []),
        }

    # ------------------------------------------------------------------------
    # 20HZ RUNTIME TICK & HOTKEY PROCESSING
    # ------------------------------------------------------------------------
    def tick(self, engine: Any) -> bool:
        """
        Called ~20Hz from `EagleCompanion.poll_ingame_hotkeys` under `engine.lock`.
        Manages proximity prompts, 3D coronas, Pause Menu BGM pause, and Auto-Play timer.
        """
        br = getattr(engine, "bridge", None)
        if br is None or not getattr(br, "handle", None):
            return False

        # Auto-pause `lit_bgm` if GTA Pause Menu (0x00BA67A4) is open
        paused_byte = br.read_u8(ADDR_GAME_PAUSED) or 0
        self.bgm_worker.pause_for_menu(bool(paused_byte != 0))
        if paused_byte != 0:
            return False

        # Auto-Play timer & Mouse Wheel accumulator (0x00CAFE78) in Reader
        now = time.time()
        if self.state in ("READING_ADV_CINEMA", "READING_NVL_BOOK"):
            wheel_delta = br.read_i32(0x00CAFE78) or 0
            if wheel_delta != 0:
                br.write_i32(0x00CAFE78, 0)
                if not self.backlog_open and not self.toc_open:
                    self.step_reader(engine, -1 if wheel_delta > 0 else +1)
                    if hasattr(engine, "_write_hud_dat"):
                        engine._write_hud_dat(force=True)
                    return True
            if (
                self.auto_speed_idx > 0
                and not self.backlog_open
                and not self.toc_open
                and now >= self.next_auto_beat_ts
            ):
                self.step_reader(engine, +1)
                return True

        snap = self._read_player_snapshot(br)
        if not snap or not snap.get("pos"):
            return False
        px, py, pz = snap["pos"]

        prev_near_v = self.near_venue_id
        prev_near_k = self.near_anchor_kind

        # 1. OUTSIDE: Check distance to all 30 venues
        if self.state == "OUTSIDE":
            best_vid = ""
            best_d2 = 999999.0
            nearest_corona: list[tuple[float, float, float, int, int, int]] = []
            for v in self.venues_list:
                dp = v.get("door_pos")
                if not dp:
                    continue
                dx = px - float(dp[0])
                dy = py - float(dp[1])
                dz = pz - float(dp[2])
                d2 = dx * dx + dy * dy + dz * dz
                if d2 < best_d2:
                    best_d2 = d2
                    best_vid = str(v.get("id") or v.get("venue_id"))
            if best_vid and best_d2 <= 65.0 * 65.0:
                v_near = self.venues[best_vid]
                dp = v_near["door_pos"]
                is_cin = str(v_near.get("type")).lower() == "cinema"
                rgb = (255, 195, 70) if is_cin else (80, 210, 255)
                nearest_corona.append((float(dp[0]), float(dp[1]), float(dp[2]) + 0.35, rgb[0], rgb[1], rgb[2]))
            self._update_cleo_coronas(br, nearest_corona)

            if best_vid and best_d2 <= 4.5 * 4.5:
                v_near = self.venues[best_vid]
                self.near_venue_id = best_vid
                self.near_anchor_kind = "VENUE_ENTRANCE"
                self.near_anchor_label = clean_cfont_ascii(
                    f"[E] Enter {v_near.get('name_en', best_vid)} ({v_near.get('tier', 'GRAND')})"
                )
            else:
                self.near_venue_id = ""
                self.near_anchor_kind = ""
                self.near_anchor_label = ""

        # 2. CINEMA_LOBBY: Check proximity to Ticket Counter, Popcorn, Posters, Auditorium Door, Exit Door
        elif self.state == "CINEMA_LOBBY":
            layout = INTERIOR_SHELL_LAYOUTS.get(self.active_shell_id) or INTERIOR_SHELL_LAYOUTS["INT-CIN-GRAND"]
            tc = layout["ticket_counter"]
            pc = layout["popcorn_counter"]
            ad = layout["auditorium_entry_door"]
            ex = layout["lobby_exit_door"]
            pb = layout["poster_board"]
            self._update_cleo_coronas(
                br,
                [
                    (tc[0], tc[1], tc[2] + 0.3, 255, 205, 75),
                    (pc[0], pc[1], pc[2] + 0.3, 255, 130, 90),
                    (ad[0], ad[1], ad[2] + 0.3, 80, 225, 255),
                    (ex[0], ex[1], ex[2] + 0.3, 120, 240, 140),
                ],
            )
            candidates = [
                ("TICKET_COUNTER", tc, "[E] Box Office Counter (Select Movie & Buy Ticket)"),
                ("POPCORN_COUNTER", pc, "[E] Popcorn & Drinks Counter (Buy Fresh Combo)"),
                ("POSTER_BOARD", pb, "[E] Weekly Movie Poster Gallery (Inspect Schedule)"),
                ("AUDITORIUM_ENTRY", ad, "[E] Enter Screening Auditorium (Ticket Check)"),
                ("LOBBY_EXIT", ex, "[E] Exit Cinema Lobby -> Return to Street"),
            ]
            self._pick_nearest_interior_anchor(px, py, pz, candidates)

        # 3. CINEMA_AUDITORIUM: Check proximity to Seat Rows & Green EXIT Door
        elif self.state == "CINEMA_AUDITORIUM":
            layout = INTERIOR_SHELL_LAYOUTS.get(self.active_shell_id) or INTERIOR_SHELL_LAYOUTS["INT-CIN-GRAND"]
            ex = layout["auditorium_exit_door"]
            seats = layout["seat_anchors"]
            coronas = [(ex[0], ex[1], ex[2] + 0.3, 80, 245, 130)]
            for s in seats[:3]:
                sp = s["pos"]
                coronas.append((sp[0], sp[1], sp[2] + 0.3, 255, 205, 85))
            self._update_cleo_coronas(br, coronas)

            candidates = [("AUDITORIUM_EXIT", ex, "[E] Green EXIT Door -> Return to Cinema Lobby")]
            for s in seats:
                candidates.append(("AUDITORIUM_SEAT", s["pos"], f"[E] Sit at {s['label']} -> Start Movie Screening"))
            self._pick_nearest_interior_anchor(px, py, pz, candidates, max_dist=3.4)

        # 4. LIBRARY_HALL: Check proximity to Librarian Counter, Shelf, Reading Tables, Exit Door
        elif self.state == "LIBRARY_HALL":
            layout = INTERIOR_SHELL_LAYOUTS.get(self.active_shell_id) or INTERIOR_SHELL_LAYOUTS["INT-LIB-GRAND"]
            lc = layout["librarian_counter"]
            sh = layout["featured_shelf"]
            ex = layout["hall_exit_door"]
            tables = layout["table_anchors"]
            coronas = [
                (lc[0], lc[1], lc[2] + 0.3, 90, 215, 255),
                (sh[0], sh[1], sh[2] + 0.3, 255, 210, 95),
                (ex[0], ex[1], ex[2] + 0.3, 110, 240, 140),
            ]
            if tables:
                tp = tables[0]["pos"]
                coronas.append((tp[0], tp[1], tp[2] + 0.3, 255, 225, 130))
            self._update_cleo_coronas(br, coronas)

            candidates = [
                ("LIBRARIAN_COUNTER", lc, "[E] Librarian Counter (Rent 7 Days / Buy Book / Coffee)"),
                ("FEATURED_SHELF", sh, "[E] Featured Literature Shelf (Browse Catalog)"),
                ("LIBRARY_EXIT", ex, "[E] Exit Library -> Return to Street"),
            ]
            for tb in tables:
                candidates.append(("READING_TABLE", tb["pos"], f"[E] Sit at {tb['label']} -> Open Book (NVL Mode)"))
            self._pick_nearest_interior_anchor(px, py, pz, candidates, max_dist=3.4)

        return (self.near_venue_id != prev_near_v) or (self.near_anchor_kind != prev_near_k)

    def _pick_nearest_interior_anchor(
        self,
        px: float,
        py: float,
        pz: float,
        candidates: list[tuple[str, tuple[float, float, float], str]],
        max_dist: float = 3.2,
    ) -> None:
        best_kind = ""
        best_lbl = ""
        best_d2 = max_dist * max_dist
        for kind, pos, lbl in candidates:
            dx = px - float(pos[0])
            dy = py - float(pos[1])
            dz = pz - float(pos[2])
            d2 = dx * dx + dy * dy + dz * dz
            if d2 <= best_d2:
                best_d2 = d2
                best_kind = kind
                best_lbl = lbl
        self.near_anchor_kind = best_kind
        self.near_anchor_label = clean_cfont_ascii(best_lbl)

    def handle_hotkeys(self, engine: Any) -> bool:
        """
        Processes rising-edge hotkeys, mouse wheel (`0x00CAFE78`), and touchscreen (`0x00CAFE60`).
        Returns `True` when a Literature modal or Reader HUD consumed the input.
        """
        br = getattr(engine, "bridge", None)
        if br is None or not br.is_game_active_and_unpaused(allow_self_overlay_fg=True):
            return False

        # 1. ACTIVE READER (`READING_ADV_CINEMA` or `READING_NVL_BOOK`)
        if self.state in ("READING_ADV_CINEMA", "READING_NVL_BOOK"):
            if (time.time() - self.reader_opened_at) < 0.30:
                br.clear_hotkey_states()
                return True

            if hasattr(engine, "_update_touch_capture"):
                if br.poll_hotkey_edge(0x74) or br.poll_hotkey_edge(0x04):  # F5 / MMB
                    engine.touch_mode = not getattr(engine, "touch_mode", False)
                    engine._update_touch_capture(bool(engine.touch_mode))
                    engine._write_hud_dat(force=True)
                    return True
                engine._update_touch_capture(bool(getattr(engine, "touch_mode", False)))
                if getattr(engine, "_touch_capture_active", False) and hasattr(engine, "_poll_touch_mailbox"):
                    if engine._poll_touch_mailbox():
                        return True

            # Poll Mouse Wheel accumulator (0x00CAFE78) for smooth NVL scrolling & ADV beat stepping
            wheel_delta = br.read_i32(0x00CAFE78) or 0
            if wheel_delta != 0:
                br.write_i32(0x00CAFE78, 0)
                if not self.backlog_open and not self.toc_open:
                    self.step_reader(engine, -1 if wheel_delta > 0 else +1)
                    engine._write_hud_dat(force=True)
                    return True

            # [Backspace] (0x08) or [Esc] (0x1B): Close sub-overlay or close Reader HUD (remains in 3D room!)
            if br.poll_hotkey_edge(0x08) or br.poll_hotkey_edge(0x1B):
                if self.backlog_open:
                    self.backlog_open = False
                elif self.toc_open:
                    self.toc_open = False
                else:
                    self.close_reader(engine, finished_work=False)
                br.clear_hotkey_states()
                engine._write_hud_dat(force=True)
                return True

            # [H] (0x48): Toggle Hide UI to admire 16:9 CG
            if br.poll_hotkey_edge(0x48):
                self.ui_hidden = not self.ui_hidden
                engine._write_hud_dat(force=True)
                return True

            # [A] (0x41): Cycle 3-Speed Auto-Play (OFF -> SLOW -> MED -> FAST -> OFF)
            if br.poll_hotkey_edge(0x41):
                self.auto_speed_idx = (self.auto_speed_idx + 1) % len(AUTO_SPEED_LABELS)
                self._sync_active_reader_media()
                engine._write_hud_dat(force=True)
                return True

            # [B] (0x42): Toggle Backlog
            if br.poll_hotkey_edge(0x42):
                self.backlog_open = not self.backlog_open
                self.toc_open = False
                engine._write_hud_dat(force=True)
                return True

            # [T] (0x54): Toggle Chapter TOC
            if br.poll_hotkey_edge(0x54):
                self.toc_open = not self.toc_open
                self.backlog_open = False
                engine._write_hud_dat(force=True)
                return True

            # [V] (0x56): Instant Dual-Mode Switch (adv_cinema <-> nvl_book)
            if br.poll_hotkey_edge(0x56):
                self.toggle_reader_mode()
                engine._write_hud_dat(force=True)
                return True

            # If TOC is open: [1..9] or Up/Down + Enter jumps chapter
            if self.toc_open:
                for k_idx in range(9):
                    if br.poll_hotkey_edge(0x31 + k_idx):
                        self.jump_to_chapter(k_idx)
                        engine._write_hud_dat(force=True)
                        return True

            # Next Beat / Page: [Space] (0x20), [Enter] (0x0D), [Down] (0x28), [Right] (0x27), [S] (0x53), [D] (0x44)
            if (
                br.poll_hotkey_edge(0x20)
                or br.poll_hotkey_edge(0x0D)
                or br.poll_hotkey_edge(0x28)
                or br.poll_hotkey_edge(0x27)
                or br.poll_hotkey_edge(0x53)
                or br.poll_hotkey_edge(0x44)
            ):
                if self.ui_hidden:
                    self.ui_hidden = False
                else:
                    self.step_reader(engine, +1)
                engine._write_hud_dat(force=True)
                return True

            # Prev Beat / Page: [Up] (0x26), [Left] (0x25), [W] (0x57)
            if br.poll_hotkey_edge(0x26) or br.poll_hotkey_edge(0x25) or br.poll_hotkey_edge(0x57):
                self.step_reader(engine, -1)
                engine._write_hud_dat(force=True)
                return True

            return True

        # 2. COUNTER MODALS (`MODAL_TICKET_COUNTER` or `MODAL_LIBRARY_COUNTER`)
        if self.state in ("MODAL_TICKET_COUNTER", "MODAL_LIBRARY_COUNTER"):
            if hasattr(engine, "_update_touch_capture"):
                if br.poll_hotkey_edge(0x74) or br.poll_hotkey_edge(0x04):
                    engine.touch_mode = not getattr(engine, "touch_mode", False)
                    engine._update_touch_capture(bool(engine.touch_mode))
                    engine._write_hud_dat(force=True)
                    return True
                engine._update_touch_capture(bool(getattr(engine, "touch_mode", False)))
                if getattr(engine, "_touch_capture_active", False) and hasattr(engine, "_poll_touch_mailbox"):
                    if engine._poll_touch_mailbox():
                        return True
            works = max(1, len(self.get_venue_works(self.active_venue_id)))
            if br.poll_hotkey_edge(0x08) or br.poll_hotkey_edge(0x1B):
                self.state = "CINEMA_LOBBY" if self.state == "MODAL_TICKET_COUNTER" else "LIBRARY_HALL"
                if hasattr(engine, "_update_touch_capture"):
                    engine._update_touch_capture(False)
                br.clear_hotkey_states()
                engine._write_hud_dat(force=True)
                return True

            # Up/Down (W/S): Select Movie or Book
            if br.poll_hotkey_edge(0x26) or br.poll_hotkey_edge(0x57):
                self.counter_selected_idx = (self.counter_selected_idx - 1) % works
                self._preview_selected_counter_work()
                engine._write_hud_dat(force=True)
                return True
            if br.poll_hotkey_edge(0x28) or br.poll_hotkey_edge(0x53):
                self.counter_selected_idx = (self.counter_selected_idx + 1) % works
                self._preview_selected_counter_work()
                engine._write_hud_dat(force=True)
                return True

            if self.state == "MODAL_TICKET_COUNTER":
                # Left/Right (A/D or Q/R): Cycle Showtimes
                if br.poll_hotkey_edge(0x25) or br.poll_hotkey_edge(0x41) or br.poll_hotkey_edge(0x51):
                    self.counter_showtime_idx = (self.counter_showtime_idx - 1) % len(SHOWTIMES_DEFAULT)
                    engine._write_hud_dat(force=True)
                    return True
                if br.poll_hotkey_edge(0x27) or br.poll_hotkey_edge(0x44) or br.poll_hotkey_edge(0x52):
                    self.counter_showtime_idx = (self.counter_showtime_idx + 1) % len(SHOWTIMES_DEFAULT)
                    engine._write_hud_dat(force=True)
                    return True
                # [C] (0x43): Buy Popcorn + Drink Combo
                if br.poll_hotkey_edge(0x43):
                    self.buy_popcorn_combo(engine)
                    engine._write_hud_dat(force=True)
                    return True
                # [Enter] (0x0D) or [Space] (0x20): Buy / Exchange Ticket & close modal so player can walk to Auditorium
                if br.poll_hotkey_edge(0x0D) or br.poll_hotkey_edge(0x20):
                    self.buy_or_exchange_cinema_ticket(engine)
                    self.state = "CINEMA_LOBBY"
                    if hasattr(engine, "_update_touch_capture"):
                        engine._update_touch_capture(False)
                    engine._write_hud_dat(force=True)
                    return True
            else:
                # Library Counter: [P] (0x50) Permanent Purchase, [C] (0x43) Coffee, [Enter/Space] Rent 7 Days
                if br.poll_hotkey_edge(0x50):
                    self.rent_or_buy_library_book(engine, permanent_buy=True)
                    self.state = "LIBRARY_HALL"
                    if hasattr(engine, "_update_touch_capture"):
                        engine._update_touch_capture(False)
                    engine._write_hud_dat(force=True)
                    return True
                if br.poll_hotkey_edge(0x43):
                    self.buy_library_coffee(engine)
                    engine._write_hud_dat(force=True)
                    return True
                if br.poll_hotkey_edge(0x0D) or br.poll_hotkey_edge(0x20):
                    self.rent_or_buy_library_book(engine, permanent_buy=False)
                    self.state = "LIBRARY_HALL"
                    if hasattr(engine, "_update_touch_capture"):
                        engine._update_touch_capture(False)
                    engine._write_hud_dat(force=True)
                    return True
            return True

        # 3. WORLD / INTERIOR INTERACTION `[E]` (0x45)
        if not self.near_anchor_kind:
            return False

        if br.poll_hotkey_edge(0x45):
            kind = self.near_anchor_kind
            if kind == "VENUE_ENTRANCE" and self.near_venue_id:
                self.enter_venue_from_street(engine, self.near_venue_id)
                engine._write_hud_dat(force=True)
                return True
            if kind in ("TICKET_COUNTER", "POSTER_BOARD"):
                self.state = "MODAL_TICKET_COUNTER"
                self._preview_selected_counter_work()
                engine._write_hud_dat(force=True)
                return True
            if kind == "POPCORN_COUNTER":
                self.buy_popcorn_combo(engine)
                engine._write_hud_dat(force=True)
                return True
            if kind == "AUDITORIUM_ENTRY":
                self.enter_cinema_auditorium(engine)
                engine._write_hud_dat(force=True)
                return True
            if kind == "LOBBY_EXIT":
                self.exit_venue_to_street(engine)
                engine._write_hud_dat(force=True)
                return True
            if kind == "AUDITORIUM_SEAT":
                self.open_reader(engine, work_id=self.active_work_id, mode="adv_cinema", return_state="CINEMA_AUDITORIUM")
                engine._write_hud_dat(force=True)
                return True
            if kind == "AUDITORIUM_EXIT":
                self.exit_auditorium_to_lobby(engine)
                engine._write_hud_dat(force=True)
                return True
            if kind in ("LIBRARIAN_COUNTER", "FEATURED_SHELF"):
                self.state = "MODAL_LIBRARY_COUNTER"
                self._preview_selected_counter_work()
                engine._write_hud_dat(force=True)
                return True
            if kind == "READING_TABLE":
                self.open_reader(engine, work_id=self.active_work_id, mode="nvl_book", return_state="LIBRARY_HALL")
                engine._write_hud_dat(force=True)
                return True
            if kind == "LIBRARY_EXIT":
                self.exit_venue_to_street(engine)
                engine._write_hud_dat(force=True)
                return True

        return False

    def _preview_selected_counter_work(self) -> None:
        works = self.get_venue_works(self.active_venue_id)
        if works:
            pack = works[self.counter_selected_idx % len(works)]
            self._sync_dynamic_lit_cg_txd(pack.resolve_cover_cg_path())

    # ------------------------------------------------------------------------
    # EXPORT HUD KEY-VALUES FOR `cleo/EagleLiteratureHud.lua`
    # ------------------------------------------------------------------------
    def build_hud_kv(self, engine: Any) -> dict[str, str]:
        """
        Exports strictly sanitized ASCII keys (`lit_*`) into `EaglePhoneState`
        for `cleo/EagleLiteratureHud.lua`.
        """
        is_fullscreen = self.state in (
            "MODAL_TICKET_COUNTER",
            "MODAL_LIBRARY_COUNTER",
            "READING_ADV_CINEMA",
            "READING_NVL_BOOK",
        )
        show_any = (self.state != "OUTSIDE") or bool(self.near_anchor_label)
        if not show_any:
            return {
                "lit_show": "0",
                "lit_state": "OUTSIDE",
                "lit_hud_hide_gta": "0",
                "lit_prompt": "",
            }

        br = getattr(engine, "bridge", None)
        now_h, now_m, now_day = self.get_game_clock(br)
        clock_str = f"{now_h:02d}:{now_m:02d}"
        v = self.venues.get(self.active_venue_id) or self.venues.get(self.near_venue_id) or {}
        v_name = clean_cfont_ascii(v.get("name_en") or v.get("name_vi") or self.active_venue_id or "Eagle Venue")
        v_tier = clean_cfont_ascii(v.get("tier") or "GRAND")
        cash_val = int(getattr(engine, "cash", 0) or 0)

        banner = self.status_banner if time.time() <= self.status_banner_until else ""
        kv: dict[str, str] = {
            "lit_show": "1",
            "lit_state": self.state,
            "lit_hud_hide_gta": "1" if is_fullscreen else "0",
            "lit_venue_id": clean_cfont_ascii(self.active_venue_id or self.near_venue_id),
            "lit_venue_name": v_name[:56],
            "lit_venue_tier": v_tier,
            "lit_clock": clock_str,
            "lit_cash": f"${cash_val:,}",
            "lit_prompt": clean_cfont_ascii(self.near_anchor_label)[:92],
            "lit_banner": clean_cfont_ascii(banner)[:96],
            "lit_cg_has": "1" if self._cg_ver > 0 else "0",
            "lit_cg_slot": str(self._cg_slot),
            "lit_cg_ver": str(self._cg_ver),
            "lit_combo_badge": (
                "POPCORN+SODA ACTIVE"
                if self.has_popcorn_combo
                else ("COFFEE BUFF ACTIVE" if self.has_coffee_buff else "")
            ),
        }

        # Populate Counter Modal keys
        if self.state in ("MODAL_TICKET_COUNTER", "MODAL_LIBRARY_COUNTER"):
            works = self.get_venue_works(self.active_venue_id)
            kv["lit_item_count"] = str(len(works))
            kv["lit_item_sel"] = str(self.counter_selected_idx % max(1, len(works)))
            for idx, w_pack in enumerate(works[:5]):
                kv[f"lit_item_{idx}_title"] = w_pack.title[:44]
                kv[f"lit_item_{idx}_meta"] = clean_cfont_ascii(
                    f"{w_pack.author} | {w_pack.estimated_minutes}m | {', '.join(w_pack.genre_tags[:2])}"
                )[:58]

            if works:
                sel_pack = works[self.counter_selected_idx % len(works)]
                kv["lit_sel_title"] = sel_pack.title[:56]
                kv["lit_sel_sub"] = (sel_pack.subtitle or sel_pack.series or sel_pack.author)[:64]
                syn_lines = textwrap.wrap(sel_pack.synopsis, width=68)[:3]
                for li in range(3):
                    kv[f"lit_sel_syn_{li}"] = syn_lines[li] if li < len(syn_lines) else ""

                if self.state == "MODAL_TICKET_COUNTER":
                    st_str = SHOWTIMES_DEFAULT[self.counter_showtime_idx % len(SHOWTIMES_DEFAULT)]
                    st_eval = self.evaluate_showtime_status(now_h, now_m, st_str)
                    t_price = 25 if v_tier == "GRAND" else 12
                    c_price = 10 if v_tier == "GRAND" else 6
                    kv["lit_showtime_str"] = f"{st_str} ({st_eval['status']})"
                    kv["lit_showtime_all"] = "  ".join(
                        f"[{s}]" if i == (self.counter_showtime_idx % len(SHOWTIMES_DEFAULT)) else s
                        for i, s in enumerate(SHOWTIMES_DEFAULT)
                    )
                    kv["lit_price_line"] = f"TICKET: ${t_price}  |  [C] POPCORN+DRINK COMBO: ${c_price}  |  MISSED = FREE EXCHANGE ($0)"
                else:
                    owned = sel_pack.work_id in (self.save_data.get("owned_books") or [])
                    rent_exp = int((self.save_data.get("rented_books") or {}).get(sel_pack.work_id, -1))
                    r_price = 8 if v_tier == "GRAND" else 5
                    b_price = max(20, min(60, int(sel_pack.library_buy_usd or (35 if v_tier == "GRAND" else 25))))
                    c_price = 5 if v_tier == "GRAND" else 4
                    status_tag = "OWNED FOREVER" if owned else (f"RENTED ({rent_exp - now_day}d left)" if rent_exp >= now_day else "AVAILABLE")
                    kv["lit_showtime_str"] = f"STATUS: {status_tag}"
                    kv["lit_price_line"] = (
                        f"[ENTER] RENT 7 DAYS: ${r_price}  |  [P] BUY FOREVER (EAGLEPHONE): ${b_price}  |  [C] COFFEE: ${c_price}"
                    )

        # Populate Active Reader keys (`adv_cinema` & `nvl_book`)
        elif self.state in ("READING_ADV_CINEMA", "READING_NVL_BOOK") and self.active_work_id in self.packs:
            pack = self.packs[self.active_work_id]
            ch_idx = max(0, min(len(pack.chapters) - 1, self.active_chapter_idx))
            ch_id = pack.chapters[ch_idx]["chapter_id"]
            ch_data = pack.get_indexed_chapter(ch_id)
            beats = ch_data["flat_beats"]
            blocks = ch_data["flat_blocks"]

            bt_idx = max(0, min(len(beats) - 1, self.active_beat_idx)) if beats else 0
            bk_idx = max(0, min(len(blocks) - 1, self.active_block_idx)) if blocks else 0
            bt = beats[bt_idx] if beats else {}
            blk = blocks[bk_idx] if blocks else {}

            _, bgm_title, _, _ = pack.resolve_bgm_info(bt.get("bgm_id") or blk.get("bgm_id"))
            r, g, b = bt.get("speaker_rgb") or pack.theme_rgb
            bm = (self.save_data.get("bookmarks") or {}).get(self.active_work_id) or {}

            kv.update(
                {
                    "lit_work_title": pack.title[:54],
                    "lit_ch_title": ch_data["chapter_title"][:64],
                    "lit_ch_badge": f"CH {ch_idx + 1}/{len(pack.chapters)}",
                    "lit_loc_label": clean_cfont_ascii(bt.get("location_label") or blk.get("location_label") or "")[:54],
                    "lit_bgm_title": clean_cfont_ascii(bgm_title or "Ambient Silence")[:44],
                    "lit_auto_label": AUTO_SPEED_LABELS[self.auto_speed_idx % len(AUTO_SPEED_LABELS)],
                    "lit_ui_hidden": "1" if self.ui_hidden else "0",
                    "lit_backlog_open": "1" if self.backlog_open else "0",
                    "lit_toc_open": "1" if self.toc_open else "0",
                    "lit_progress_pct": f"{bm.get('progress_pct', 0.0)}%",
                    "lit_spk_name": clean_cfont_ascii(bt.get("speaker_name") or "")[:32],
                    "lit_spk_r": str(r),
                    "lit_spk_g": str(g),
                    "lit_spk_b": str(b),
                    "lit_beat_pos": f"BEAT {bt_idx + 1}/{max(1, len(beats))}",
                    "lit_block_pos": f"PAGE {bk_idx + 1}/{max(1, len(blocks))}",
                }
            )

            # Wrap ADV subtitle into 3 clean lines (max 88 chars/line for crisp 1920x1080 readability)
            adv_lines = textwrap.wrap(clean_cfont_ascii(bt.get("text") or ""), width=88)[:3]
            for i in range(3):
                kv[f"lit_adv_line_{i}"] = adv_lines[i] if i < len(adv_lines) else ""

            # Build NVL Multi-Paragraph Page (shows current block + up to 2 following blocks on the same page!)
            nvl_lines: list[str] = []
            for p_off in range(3):
                b_i = bk_idx + p_off
                if b_i >= len(blocks):
                    break
                p_txt = clean_cfont_ascii(blocks[b_i].get("nvl_paragraph") or "")
                prefix = ">> " if p_off == 0 else "   "
                wrapped_p = textwrap.wrap(prefix + p_txt, width=62)[:5]
                nvl_lines.extend(wrapped_p)
                nvl_lines.append("")
            for i in range(14):
                kv[f"lit_nvl_line_{i}"] = nvl_lines[i] if i < len(nvl_lines) else ""

            # Backlog lines (last 6 spoken beats up to `bt_idx`)
            start_bl = max(0, bt_idx - 5)
            bl_slice = beats[start_bl : bt_idx + 1]
            for i in range(6):
                if i < len(bl_slice):
                    b_item = bl_slice[i]
                    sn = b_item.get("speaker_name") or "NARRATOR"
                    kv[f"lit_bl_line_{i}"] = clean_cfont_ascii(f"[{sn}] {b_item.get('text', '')}")[:96]
                else:
                    kv[f"lit_bl_line_{i}"] = ""

            # TOC chapters (up to 8 chapters)
            kv["lit_toc_count"] = str(min(8, len(pack.chapters)))
            for i in range(8):
                if i < len(pack.chapters):
                    c_m = pack.chapters[i]
                    kv[f"lit_toc_{i}"] = clean_cfont_ascii(f"[{i + 1}] {c_m.get('title', c_m.get('chapter_id', ''))}")[:68]
                else:
                    kv[f"lit_toc_{i}"] = ""

        return kv
