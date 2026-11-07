# Eagle Literature Studio

Eagle Literature Studio is the full content pipeline for Project Eagle's in-game literature system —
cinemas, libraries, visual novel story quests, and Re:Zero web novel adaptation.

## Repository Structure

```
engine/
  eagle_literature_engine.py    — Core engine: 30 venues, 4 sky-grid interiors, double-buffer CG streaming
tools/
  codex_cg_batch_runner.py      — Default CG batch runner (local codex.exe, gpt-6.1-terra)
  openai_cg_batch_runner.py     — Optional OpenAI runner (gpt-image-2.6-sunburst, on-demand only)
  merge_arc_clusters.py         — Merge Arc 7 cluster JSONs into gameplay-lab
  cfont_sanitizer.py            — Sanitize custom fonts for literature UI
  build_preview_html.py         — Build HTML preview for packs
  validate_pack.py              — Validate package structure and manifest integrity
  audio_mci_normalizer.py       — Normalize audio assets for MCI playback
```

## Venue System

- **30 venues**: CIN-01..15 (cinemas) + LIB-01..15 (libraries), IDs 15110–15139
- **4 sky-grid interiors**: Area Code=1, Z=+1650m
- **Double-buffer CG streaming**: LITCG0.TXD / LITCG1.TXD
- **Auto-discovery**: Packs from `data/project-eagle/literature/packs/` + `D:\Eagle Literature Studio\packages\`

## Current Packages (07/11/2026)

| Package | Type | Chapters | CGs | Status |
|---------|------|----------|-----|--------|
| rezero_ayamatsu_pride_if | cinema (CIN) | 6 | 50 | ✅ Certified |
| rezero_rem_sloth_if | cinema (CIN) | 9 | 44 | ✅ Done |
| rezero_kasaneru_greed_if | cinema (CIN) | 6 | 42 | ✅ Done |
| rezero_tsugihagu_gluttony_if | cinema (CIN) | 6 | 42 | ✅ Done |
| rezero_oboreru_wrath_if | cinema (CIN) | 6 | 36 | ✅ Done |
| rezero_butterfly_lust_if | cinema (CIN) | 8 | 28 | ✅ Done |
| rezero_arc7_wn | library (LIB) | 116 | 0 (book mode) | 🔄 In Progress |

## CG Generation

Default engine: `codex_cg_batch_runner.py` using local `codex.exe` with `gpt-6.1-terra` model.

> ⚠️ `openai_cg_batch_runner.py` is an **optional** runner for the OpenAI `gpt-image-2.6-sunburst` model.
> It is only activated on explicit user request and subject to quota limits.

## Integration

Game target: `C:\Users\LENOVO\Desktop\GTA\Project Eagle\gta sa gameplay-lab`

Literature packs deploy to: `gameplay-lab/ui_assets/literature_packs/{pack_id}/`
