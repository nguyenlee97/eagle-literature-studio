#!/usr/bin/env python3
"""Win32 MCI Safe BGM Normalizer & Acoustic Profiler.

Enforces Section 6 & Section 7 (Gate 6) of GTA_EAGLE_LIT_CONTRACT_V1.md:
- Verifies playable duration >= 55.0s via ffprobe/ffmpeg.
- Detects lead silence and automatically trims intros when lead_silence_sec > 2.0s.
- Encodes CBR 192kbps, 44.1kHz, 2-channel stereo MP3 with 100% stripped cover art,
  ID3v2 metadata, and Xing VBR headers (-vn -map_metadata -1 -write_xing 0 -id3v2_version 0)
  to prevent Win32 mciSendStringW Error 277 in GTA San Andreas.
- Measures mean_volume_db, max_volume_db, and late_surge_db, and calibrates
  base_mci_volume inside [185, 240] (anchored at 215 for -19.5 dBFS).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

VALID_MOOD_BUCKETS: frozenset[str] = frozenset(
    {
        "hub_cozy",
        "vn_daily_sweet",
        "vn_tender_romance",
        "vn_melancholy_memory",
        "vn_mystic_tension",
    }
)

MIN_DURATION_SEC = 55.0
MAX_LEAD_SILENCE_SEC = 2.0
TARGET_POST_TRIM_SILENCE_SEC = 0.35
ASCII_MP3_NAME_RE = re.compile(r"^[a-z0-9_]+\.mp3$")

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9]+(?:\.[0-9]+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9]+(?:\.[0-9]+)?)")
_MEAN_VOL_RE = re.compile(r"mean_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB")
_MAX_VOL_RE = re.compile(r"max_volume:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*dB")


class AudioNormalizationError(RuntimeError):
    """Raised when an audio file fails Win32 MCI normalization or contract rules."""


def probe_audio_file(path: Path) -> dict[str, Any]:
    """Run ffprobe and return stream + format metadata."""
    if not path.is_file():
        raise AudioNormalizationError(f"Audio file not found: {path}")

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AudioNormalizationError(
            f"ffprobe failed on {path}: {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AudioNormalizationError(
            f"Invalid ffprobe JSON output for {path}"
        ) from exc


def detect_lead_silence_sec(
    path: Path,
    noise_db: float = -48.0,
    min_silence_dur: float = 0.15,
    probe_window_sec: float = 30.0,
) -> float:
    """Measure leading silence duration in seconds from t=0."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-t",
        f"{probe_window_sec:.2f}",
        "-i",
        str(path),
        "-vn",
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence_dur}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    log = proc.stderr or ""

    starts = [float(m.group(1)) for m in _SILENCE_START_RE.finditer(log)]
    ends = [float(m.group(1)) for m in _SILENCE_END_RE.finditer(log)]

    if not starts:
        return 0.0

    # Only count silence that begins right at the start of the track (<= 0.15s).
    first_start = starts[0]
    if first_start > 0.15:
        return 0.0

    if ends:
        return round(max(0.0, ends[0]), 3)

    # Silence started at 0 and never ended within probe_window_sec.
    return round(probe_window_sec, 3)


def measure_volume_segment_db(
    path: Path,
    start_sec: float | None = None,
    duration_sec: float | None = None,
) -> tuple[float, float]:
    """Return (mean_volume_db, max_volume_db) for the track or segment."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start_sec is not None and start_sec > 0.0:
        cmd += ["-ss", f"{start_sec:.3f}"]
    if duration_sec is not None and duration_sec > 0.0:
        cmd += ["-t", f"{duration_sec:.3f}"]
    cmd += [
        "-i",
        str(path),
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    log = proc.stderr or ""

    mean_match = _MEAN_VOL_RE.search(log)
    max_match = _MAX_VOL_RE.search(log)
    mean_db = float(mean_match.group(1)) if mean_match else -20.0
    max_db = float(max_match.group(1)) if max_match else -3.0
    return round(mean_db, 1), round(max_db, 1)


def compute_late_surge_db(path: Path, total_duration_sec: float) -> float:
    """Measure how much louder the middle/late sections surge above the intro third."""
    if total_duration_sec < 9.0:
        return 0.0
    seg_len = total_duration_sec / 3.0
    intro_mean, _ = measure_volume_segment_db(path, start_sec=0.0, duration_sec=seg_len)
    mid_mean, _ = measure_volume_segment_db(
        path, start_sec=seg_len, duration_sec=seg_len
    )
    late_mean, _ = measure_volume_segment_db(
        path, start_sec=seg_len * 2.0, duration_sec=seg_len
    )
    surge = max(0.0, max(mid_mean, late_mean) - intro_mean)
    return round(surge, 1)


def compute_base_mci_volume(mean_volume_db: float, late_surge_db: float = 0.0) -> int:
    """Compute Win32 MCI volume in [185, 240], anchored at 215 for -19.5 dBFS."""
    effective_db = mean_volume_db + max(0.0, late_surge_db - 4.0) * 0.35
    delta_from_ref = -19.5 - effective_db
    scaled = round(215.0 + delta_from_ref * 5.0)
    return max(185, min(240, int(scaled)))


def _strip_residual_id3_tags(mp3_path: Path) -> None:
    """Ensure zero leading ID3v2 header or trailing ID3v1 tag bytes exist on disk."""
    data = mp3_path.read_bytes()
    changed = False

    while len(data) >= 10 and data[:3] == b"ID3":
        # Synchsafe 28-bit integer size at bytes 6..9
        tag_size = (
            ((data[6] & 0x7F) << 21)
            | ((data[7] & 0x7F) << 14)
            | ((data[8] & 0x7F) << 7)
            | (data[9] & 0x7F)
        )
        has_footer = bool(data[5] & 0x10)
        total_header = 10 + tag_size + (10 if has_footer else 0)
        if total_header >= len(data):
            break
        data = data[total_header:]
        changed = True

    if len(data) > 128 and data[-128:-125] == b"TAG":
        data = data[:-128]
        changed = True

    if changed:
        mp3_path.write_bytes(data)


def normalize_bgm_track(
    input_path: Path,
    output_path: Path,
    *,
    bgm_id: str | None = None,
    title: str = "",
    composer_or_origin: str = "",
    fallback_mood_bucket: str = "vn_mystic_tension",
    loop: bool = True,
    rel_dir_prefix: str = "bgm",
    allow_short: bool = False,
) -> dict[str, Any]:
    """Normalize an audio track into a Win32 MCI-safe 192kbps CBR MP3 and return its catalog entry."""
    if fallback_mood_bucket not in VALID_MOOD_BUCKETS:
        raise AudioNormalizationError(
            f"Invalid fallback_mood_bucket '{fallback_mood_bucket}'. "
            f"Expected one of: {sorted(VALID_MOOD_BUCKETS)}"
        )

    out_name = output_path.name
    if not ASCII_MP3_NAME_RE.match(out_name):
        raise AudioNormalizationError(
            f"Output BGM filename '{out_name}' must be lowercase ASCII matching ^[a-z0-9_]+\\.mp3$"
        )

    in_probe = probe_audio_file(input_path)
    audio_streams = [
        s for s in in_probe.get("streams", []) if s.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise AudioNormalizationError(f"No audio stream found in {input_path}")

    raw_duration = float(
        in_probe.get("format", {}).get("duration")
        or audio_streams[0].get("duration")
        or 0.0
    )
    raw_lead_silence = detect_lead_silence_sec(
        input_path, probe_window_sec=min(max(raw_duration, 5.0), 45.0)
    )

    trim_offset = 0.0
    if raw_lead_silence > MAX_LEAD_SILENCE_SEC:
        trim_offset = round(
            max(0.0, raw_lead_silence - TARGET_POST_TRIM_SILENCE_SEC), 3
        )

    expected_duration = raw_duration - trim_offset
    if not allow_short and expected_duration < MIN_DURATION_SEC:
        raise AudioNormalizationError(
            f"Track {input_path.name} duration after lead-silence trim is "
            f"{expected_duration:.2f}s (< {MIN_DURATION_SEC:.1f}s contract minimum)."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if trim_offset > 0.0:
        cmd += ["-ss", f"{trim_offset:.3f}"]
    cmd += [
        "-i",
        str(input_path),
        "-vn",
        "-sn",
        "-dn",
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
    ]
    if trim_offset > 0.0:
        cmd += ["-af", "afade=t=in:st=0:d=0.06"]
    cmd += [
        "-c:a",
        "libmp3lame",
        "-b:a",
        "192k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-write_xing",
        "0",
        "-id3v2_version",
        "0",
        str(output_path),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AudioNormalizationError(
            f"ffmpeg MP3 normalization failed for {input_path}: {proc.stderr.strip()}"
        )

    _strip_residual_id3_tags(output_path)

    # Verify normalized output with ffprobe & acoustic analyzers.
    out_probe = probe_audio_file(output_path)
    out_streams = out_probe.get("streams", [])
    out_format = out_probe.get("format", {})
    nb_streams = int(out_format.get("nb_streams", len(out_streams)))
    if nb_streams != 1 or len(out_streams) != 1:
        raise AudioNormalizationError(
            f"Normalized MP3 {output_path} has nb_streams={nb_streams} (must be 1 with zero cover art)."
        )

    stream0 = out_streams[0]
    if stream0.get("codec_name") != "mp3":
        raise AudioNormalizationError(
            f"Normalized file {output_path} codec is {stream0.get('codec_name')}, expected 'mp3'."
        )
    if int(stream0.get("sample_rate", 0)) != 44100 or int(stream0.get("channels", 0)) != 2:
        raise AudioNormalizationError(
            f"Normalized MP3 {output_path} must be 44100Hz 2ch stereo."
        )

    header_bytes = output_path.read_bytes()[:3]
    if header_bytes == b"ID3":
        raise AudioNormalizationError(
            f"Normalized MP3 {output_path} still starts with ID3v2 tag bytes."
        )

    final_duration = round(
        float(out_format.get("duration") or stream0.get("duration") or 0.0), 2
    )
    if not allow_short and final_duration < MIN_DURATION_SEC:
        raise AudioNormalizationError(
            f"Normalized MP3 {output_path.name} duration is {final_duration:.2f}s (< {MIN_DURATION_SEC:.1f}s)."
        )

    final_lead_silence = round(
        detect_lead_silence_sec(
            output_path, probe_window_sec=min(max(final_duration, 5.0), 10.0)
        ),
        2,
    )
    if final_lead_silence > MAX_LEAD_SILENCE_SEC:
        raise AudioNormalizationError(
            f"Normalized MP3 {output_path.name} still has lead_silence_sec={final_lead_silence:.2f}s (> 2.0s)."
        )

    mean_db, max_db = measure_volume_segment_db(output_path)
    late_surge_db = compute_late_surge_db(output_path, final_duration)
    base_mci_vol = compute_base_mci_volume(mean_db, late_surge_db)

    resolved_id = bgm_id or output_path.stem
    rel_file = f"{rel_dir_prefix}/{out_name}" if rel_dir_prefix else out_name

    entry = {
        "bgm_id": resolved_id,
        "catalog_entry": {
            "file": rel_file,
            "title": title or resolved_id,
            "composer_or_origin": composer_or_origin or "Original Franchise OST",
            "fallback_mood_bucket": fallback_mood_bucket,
            "duration_sec": final_duration,
            "lead_silence_sec": final_lead_silence,
            "mean_volume_db": mean_db,
            "max_volume_db": max_db,
            "late_surge_db": late_surge_db,
            "base_mci_volume": base_mci_vol,
            "loop": bool(loop),
        },
        "normalization_log": {
            "source_file": str(input_path),
            "raw_duration_sec": round(raw_duration, 2),
            "raw_lead_silence_sec": round(raw_lead_silence, 2),
            "trimmed_lead_sec": trim_offset,
        },
    }
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize BGM audio into Win32 MCI-safe 192kbps CBR MP3 (stripped ID3v2/cover-art) and compute volume profile."
    )
    parser.add_argument("--input", "-i", required=True, help="Input audio file path.")
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output .mp3 path (e.g. packages/<work_id>/bgm/bgm_01_dread.mp3).",
    )
    parser.add_argument(
        "--bgm-id",
        help="Catalog key in manifest.bgm_catalog (defaults to output filename stem).",
    )
    parser.add_argument("--title", default="", help="Display track title (ASCII).")
    parser.add_argument(
        "--composer",
        default="",
        help="Composer / OST origin credit (ASCII).",
    )
    parser.add_argument(
        "--mood-bucket",
        default="vn_mystic_tension",
        choices=sorted(VALID_MOOD_BUCKETS),
        help="Fallback GTA mood bucket.",
    )
    parser.add_argument(
        "--no-loop",
        action="store_true",
        help="Set loop=false in catalog entry.",
    )
    parser.add_argument(
        "--allow-short",
        action="store_true",
        help="Diagnostic override allowing tracks shorter than 55.0s (never use for production packs).",
    )
    parser.add_argument(
        "--json-out",
        help="Optional path to save resulting JSON metadata snippet.",
    )

    args = parser.parse_args(argv)

    try:
        result = normalize_bgm_track(
            input_path=Path(args.input),
            output_path=Path(args.output),
            bgm_id=args.bgm_id,
            title=args.title,
            composer_or_origin=args.composer,
            fallback_mood_bucket=args.mood_bucket,
            loop=not args.no_loop,
            allow_short=args.allow_short,
        )
    except AudioNormalizationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    rendered = json.dumps(result, indent=2, ensure_ascii=True)
    if args.json_out:
        out_json = Path(args.json_out)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
