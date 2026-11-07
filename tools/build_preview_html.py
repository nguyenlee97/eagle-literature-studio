#!/usr/bin/env python3
"""Standalone CORS-Free Dual-Mode HTML Preview Builder for EagleLit Pack v1.0.

Generates `<pack_dir>/preview.html` which opens directly over `file:///` in any
modern browser with zero local web server required:
  - Embeds manifest.json, all chapters/*.json, and cg_generation_manifest.json
    directly inside `<script id="eagle-lit-pack-data" type="application/json">`.
  - Mode 1 (`adv_cinema`): Fullscreen 16:9 CG stage + frosted bottom HUD (~22%),
    colored speaker nameplates, inner_thought line, scene transitions.
  - Mode 2 (`nvl_book`): Split-Screen 46% Left CG + Scene/BGM cards & 54% Right
    scrollable verbatim book page; scrolling automatically crossfades Left CG
    (`cg_id`) and switches `<audio>` BGM (`bgm_id`) with zero position drift on `[V]`.
  - Interactive features: `[V]` Mode Switch, `[A]` Auto-Play (1.0x/1.5x/2.0x),
    `[B]` Backlog, `[H]` Hide UI, `[G]` CG Gallery, Chapter/Scene selector, and
    dual-deck `<audio>` BGM crossfader scaled by `base_mci_volume`.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import sys
from typing import Any


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__PAGE_TITLE__ -- EagleLit Dual-Mode Preview</title>
<style>
  :root {
    --accent-r: __ACCENT_R__;
    --accent-g: __ACCENT_G__;
    --accent-b: __ACCENT_B__;
    --accent: rgb(var(--accent-r), var(--accent-g), var(--accent-b));
    --accent-soft: rgba(var(--accent-r), var(--accent-g), var(--accent-b), 0.24);
    --bg-deep: #090b10;
    --bg-panel: #11151f;
    --bg-book: #141822;
    --text-main: #eef1f7;
    --text-muted: #9aa4b8;
    --border-subtle: rgba(255, 255, 255, 0.11);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  html, body {
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: var(--bg-deep);
    color: var(--text-main);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    user-select: none;
  }

  /* Top Studio Control Bar */
  #topbar {
    position: fixed;
    top: 0; left: 0; right: 0;
    height: 48px;
    z-index: 60;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    background: rgba(9, 11, 16, 0.88);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border-subtle);
    transition: opacity 0.25s ease, transform 0.25s ease;
  }

  body.ui-hidden #topbar {
    opacity: 0;
    pointer-events: none;
    transform: translateY(-100%);
  }

  .top-left, .top-center, .top-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .brand-pill {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 4px 8px;
    border-radius: 4px;
    background: var(--accent-soft);
    border: 1px solid var(--accent);
    color: #fff;
  }

  .work-short-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-main);
    max-width: 260px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  select.nav-select, button.ctrl-btn {
    background: rgba(255, 255, 255, 0.06);
    color: var(--text-main);
    border: 1px solid var(--border-subtle);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
  }

  select.nav-select:hover, button.ctrl-btn:hover {
    background: rgba(255, 255, 255, 0.13);
    border-color: rgba(255, 255, 255, 0.28);
  }

  select.nav-select option {
    background: #141925;
    color: #eef1f7;
  }

  button.ctrl-btn.active {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }

  .vol-wrap {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    color: var(--text-muted);
  }

  .vol-wrap input[type="range"] {
    width: 68px;
    accent-color: var(--accent);
    cursor: pointer;
  }

  /* Main Viewport Container */
  #app-root {
    position: fixed;
    top: 48px;
    left: 0;
    right: 0;
    bottom: 0;
  }

  body.ui-hidden #app-root {
    top: 0;
  }

  /* =========================================================
     MODE 1: adv_cinema (Fullscreen 16:9 Stage + Bottom HUD)
     ========================================================= */
  #adv-cinema-view {
    width: 100%;
    height: 100%;
    display: none;
    align-items: center;
    justify-content: center;
    background: #040508;
    position: relative;
  }

  body[data-mode="adv_cinema"] #adv-cinema-view {
    display: flex;
  }

  #cinema-stage-16x9 {
    position: relative;
    width: min(100%, calc(100vh * 16 / 9));
    aspect-ratio: 16 / 9;
    max-height: 100%;
    background: #080a10;
    overflow: hidden;
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.85);
    cursor: pointer;
  }

  .cg-layer {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    opacity: 0;
    transition: opacity 0.42s ease;
    pointer-events: none;
  }

  .cg-layer.visible {
    opacity: 1;
  }

  .cg-pending-fallback {
    position: absolute;
    inset: 0;
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 32px;
    text-align: center;
    background: radial-gradient(circle at 50% 38%, rgba(var(--accent-r), var(--accent-g), var(--accent-b), 0.22), #090c14 76%);
    color: var(--text-main);
    z-index: 3;
  }

  .cg-pending-fallback.visible {
    display: flex;
  }

  .cg-pending-badge {
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--accent);
    background: rgba(0, 0, 0, 0.55);
    margin-bottom: 10px;
  }

  /* Transition FX overlay */
  #scene-fx-overlay {
    position: absolute;
    inset: 0;
    pointer-events: none;
    opacity: 0;
    z-index: 12;
    transition: opacity 0.48s ease;
  }

  /* Safe-Zone QA Guide (y = 0.08..0.75) */
  #safe-zone-guide {
    position: absolute;
    left: 3%;
    right: 3%;
    top: 8%;
    height: 67%;
    border: 1.5px dashed rgba(80, 230, 160, 0.82);
    border-radius: 6px;
    pointer-events: none;
    z-index: 25;
    display: none;
  }

  #safe-zone-guide.visible {
    display: block;
  }

  #safe-zone-guide span {
    position: absolute;
    top: 6px;
    left: 8px;
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    background: rgba(0, 0, 0, 0.75);
    color: #50e6a0;
  }

  /* Bottom ~22% Frosted HUD Box */
  #cinema-hud {
    position: absolute;
    left: 3.5%;
    right: 3.5%;
    bottom: 3%;
    min-height: 21.5%;
    max-height: 26%;
    z-index: 20;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 16px 24px 12px;
    background: linear-gradient(180deg, rgba(10, 13, 20, 0.80) 0%, rgba(7, 9, 14, 0.92) 100%);
    backdrop-filter: blur(14px);
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 12px;
    box-shadow: 0 12px 36px rgba(0, 0, 0, 0.72);
    transition: opacity 0.22s ease, transform 0.22s ease;
  }

  body.ui-hidden #cinema-hud {
    opacity: 0;
    pointer-events: none;
    transform: translateY(14px);
  }

  .hud-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
  }

  #hud-speaker {
    display: inline-block;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.03em;
    padding: 3px 12px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.1);
    border-left: 3px solid var(--accent);
  }

  #hud-meta-pill {
    font-size: 11px;
    color: var(--text-muted);
  }

  #hud-text {
    font-size: clamp(15px, 1.45vw, 20px);
    line-height: 1.52;
    color: #f5f7fc;
    user-select: text;
    flex: 1;
  }

  #hud-inner-thought {
    font-size: clamp(13px, 1.15vw, 16px);
    font-style: italic;
    color: #b7c4e0;
    margin-top: 5px;
  }

  .hud-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 11px;
    color: var(--text-muted);
    margin-top: 8px;
    position: relative;
  }

  #autoplay-bar {
    position: absolute;
    left: 0;
    bottom: -12px;
    height: 3px;
    width: 0%;
    background: var(--accent);
    border-radius: 0 0 12px 12px;
  }

  /* =========================================================
     MODE 2: nvl_book (46% Left CG + 54% Right Scrollable Book)
     ========================================================= */
  #nvl-book-view {
    width: 100%;
    height: 100%;
    display: none;
    background: var(--bg-deep);
  }

  body[data-mode="nvl_book"] #nvl-book-view {
    display: flex;
  }

  #nvl-left-pane {
    width: 46%;
    height: 100%;
    padding: 20px 18px 20px 22px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 14px;
    background: #0c0f17;
    border-right: 1px solid var(--border-subtle);
  }

  #nvl-cg-frame {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #07090e;
    border-radius: 10px;
    overflow: hidden;
    border: 1px solid rgba(255, 255, 255, 0.14);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65);
  }

  .nvl-info-card {
    background: var(--bg-panel);
    border: 1px solid var(--border-subtle);
    border-radius: 10px;
    padding: 12px 15px;
  }

  .nvl-card-kicker {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--accent);
    margin-bottom: 4px;
  }

  .nvl-card-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-main);
    margin-bottom: 4px;
  }

  .nvl-card-sub {
    font-size: 12px;
    color: var(--text-muted);
    line-height: 1.45;
  }

  #nvl-right-pane {
    width: 54%;
    height: 100%;
    overflow-y: auto;
    scroll-behavior: smooth;
    background: var(--bg-book);
    padding: 40px clamp(24px, 4.5vw, 68px) 48vh;
    user-select: text;
  }

  .book-chapter-header {
    margin-bottom: 28px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border-subtle);
  }

  .book-chapter-header h1 {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 24px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 6px;
  }

  .book-chapter-header p {
    font-size: 12px;
    color: var(--text-muted);
  }

  .book-scene-divider {
    margin: 26px 0 14px;
    padding: 6px 10px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-muted);
    background: rgba(255, 255, 255, 0.03);
    border-left: 2px solid var(--accent);
    border-radius: 0 4px 4px 0;
  }

  .book-block {
    font-family: Georgia, "Palatino Linotype", serif;
    font-size: 17.5px;
    line-height: 1.78;
    color: #d8deeb;
    padding: 10px 14px;
    margin-bottom: 10px;
    border-radius: 8px;
    border-left: 3px solid transparent;
    cursor: pointer;
    transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease;
  }

  .book-block:hover {
    background: rgba(255, 255, 255, 0.03);
  }

  .book-block.active-block {
    background: rgba(var(--accent-r), var(--accent-g), var(--accent-b), 0.11);
    border-left-color: var(--accent);
    color: #ffffff;
  }

  .book-block.kind-internal {
    font-style: italic;
    color: #c7d2ea;
  }

  .book-block.kind-epigraph {
    font-style: italic;
    text-align: center;
    color: #b8c3dc;
    border-left: none;
    padding: 14px 24px;
  }

  .book-block.kind-scene_break {
    text-align: center;
    letter-spacing: 0.35em;
    color: var(--text-muted);
    font-family: monospace;
    padding: 8px 0;
    cursor: default;
  }

  .book-chapter-nav-footer {
    margin-top: 32px;
    padding-top: 18px;
    border-top: 1px solid var(--border-subtle);
    display: flex;
    justify-content: space-between;
  }

  /* =========================================================
     MODALS: Backlog [B], CG Gallery [G], Pack Info [I]
     ========================================================= */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 90;
    background: rgba(5, 7, 12, 0.84);
    backdrop-filter: blur(10px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 28px;
  }

  .modal-backdrop.open {
    display: flex;
  }

  .modal-panel {
    width: min(1040px, 95vw);
    max-height: 88vh;
    background: #111622;
    border: 1px solid rgba(255, 255, 255, 0.16);
    border-radius: 14px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.85);
  }

  .modal-head {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border-subtle);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .modal-head h2 {
    font-size: 16px;
    font-weight: 600;
  }

  .modal-body {
    padding: 18px 20px;
    overflow-y: auto;
    user-select: text;
  }

  .backlog-item {
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    cursor: pointer;
    border-radius: 6px;
  }

  .backlog-item:hover {
    background: rgba(255, 255, 255, 0.05);
  }

  .backlog-speaker {
    font-size: 12px;
    font-weight: 700;
    margin-bottom: 3px;
  }

  .backlog-text {
    font-size: 14px;
    color: #e2e7f2;
    line-height: 1.48;
  }

  /* CG Gallery Grid */
  .cg-gallery-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 14px;
  }

  .cg-card {
    background: #0b0e16;
    border: 1px solid var(--border-subtle);
    border-radius: 10px;
    overflow: hidden;
    cursor: pointer;
    transition: transform 0.15s ease, border-color 0.15s ease;
  }

  .cg-card:hover {
    transform: translateY(-2px);
    border-color: var(--accent);
  }

  .cg-card img {
    width: 100%;
    aspect-ratio: 16 / 9;
    object-fit: cover;
    display: block;
    background: #07090e;
  }

  .cg-card.poster-card img {
    aspect-ratio: 2 / 3;
    max-height: 220px;
    object-fit: contain;
  }

  .cg-card-meta {
    padding: 9px 11px;
  }

  .cg-card-id {
    font-size: 11px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 3px;
  }

  .cg-card-desc {
    font-size: 11px;
    color: var(--text-muted);
    line-height: 1.38;
  }

  /* Lightbox for full CG inspection */
  #cg-lightbox {
    position: fixed;
    inset: 0;
    z-index: 120;
    background: rgba(3, 4, 8, 0.94);
    display: none;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 20px;
    cursor: zoom-out;
  }

  #cg-lightbox.open {
    display: flex;
  }

  #cg-lightbox img {
    max-width: 94vw;
    max-height: 82vh;
    border-radius: 8px;
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.9);
  }

  #cg-lightbox-caption {
    margin-top: 10px;
    font-size: 13px;
    color: var(--text-muted);
    text-align: center;
    max-width: 820px;
  }

  .info-hero {
    display: flex;
    gap: 20px;
    align-items: flex-start;
  }

  .info-poster {
    width: 170px;
    border-radius: 8px;
    border: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .stat-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 12px 0;
  }

  .stat-badge {
    font-size: 11px;
    padding: 4px 9px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid var(--border-subtle);
  }
</style>
</head>
<body data-mode="__DEFAULT_MODE__">

<!-- Top Studio Navigation & Mode Switcher -->
<header id="topbar">
  <div class="top-left">
    <span class="brand-pill" id="venue-badge">EAGLELIT</span>
    <span class="work-short-title" id="top-title"></span>
    <button class="ctrl-btn" id="btn-info" title="Work Synopsis & Provenance [I]">Info</button>
  </div>

  <div class="top-center">
    <select class="nav-select" id="chapter-select" title="Jump to Chapter"></select>
    <select class="nav-select" id="scene-select" title="Jump to Scene"></select>
    <button class="ctrl-btn" id="btn-mode-toggle" title="Switch Dual-Mode Instantaneously [V]">Mode [V]</button>
    <button class="ctrl-btn" id="btn-autoplay" title="Toggle Auto-Play [A]">Auto [A]: OFF</button>
    <button class="ctrl-btn" id="btn-speed" title="Cycle Auto-Play Speed">1.0x</button>
  </div>

  <div class="top-right">
    <button class="ctrl-btn" id="btn-backlog" title="Dialogue Backlog [B]">Backlog [B]</button>
    <button class="ctrl-btn" id="btn-gallery" title="Full-Color CG Gallery [G]">CG Gallery [G]</button>
    <button class="ctrl-btn" id="btn-safezone" title="Toggle Dual-Mode Safe Zone y=0.08..0.75 [S]">SafeZone</button>
    <button class="ctrl-btn" id="btn-hide-ui" title="Hide UI to Inspect CG [H]">Hide [H]</button>
    <button class="ctrl-btn" id="btn-bgm-mute" title="Toggle BGM Audio [M]">BGM: ON</button>
    <div class="vol-wrap">
      <input type="range" id="bgm-volume" min="0" max="100" value="75" title="Master BGM Volume">
    </div>
  </div>
</header>

<main id="app-root">
  <!-- MODE 1: adv_cinema -->
  <section id="adv-cinema-view">
    <div id="cinema-stage-16x9">
      <img id="cinema-cg-a" class="cg-layer visible" alt="CG Layer A">
      <img id="cinema-cg-b" class="cg-layer" alt="CG Layer B">
      <div id="cinema-cg-pending" class="cg-pending-fallback">
        <div class="cg-pending-badge">CG Pending Quota Recovery</div>
        <strong id="cinema-pending-id"></strong>
        <p id="cinema-pending-summary" style="margin-top:8px;max-width:560px;font-size:13px;color:#b8c2d8;"></p>
      </div>
      <div id="scene-fx-overlay"></div>
      <div id="safe-zone-guide"><span>Dual-Mode Safe Zone (y = 0.08 .. 0.75)</span></div>

      <div id="cinema-hud">
        <div>
          <div class="hud-header">
            <span id="hud-speaker">Narrator</span>
            <span id="hud-meta-pill"></span>
          </div>
          <div id="hud-text"></div>
          <div id="hud-inner-thought"></div>
        </div>
        <div class="hud-footer">
          <span id="hud-bgm-label">BGM: --</span>
          <span id="hud-progress-label">Beat 1 / 1  |  [Space/Click] Next  [Left] Prev  [V] Book Mode</span>
          <div id="autoplay-bar"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- MODE 2: nvl_book -->
  <section id="nvl-book-view">
    <aside id="nvl-left-pane">
      <div id="nvl-cg-frame">
        <img id="nvl-cg-a" class="cg-layer visible" alt="Book CG Layer A">
        <img id="nvl-cg-b" class="cg-layer" alt="Book CG Layer B">
        <div id="nvl-cg-pending" class="cg-pending-fallback">
          <div class="cg-pending-badge">CG Pending Quota</div>
          <strong id="nvl-pending-id"></strong>
          <p id="nvl-pending-summary" style="margin-top:6px;font-size:12px;color:#b8c2d8;"></p>
        </div>
      </div>

      <div class="nvl-info-card">
        <div class="nvl-card-kicker" id="nvl-scene-kicker">SCENE</div>
        <div class="nvl-card-title" id="nvl-scene-title">--</div>
        <div class="nvl-card-sub" id="nvl-scene-loc">--</div>
        <div class="nvl-card-sub" id="nvl-scene-ambience" style="margin-top:4px;font-style:italic;"></div>
      </div>

      <div class="nvl-info-card">
        <div class="nvl-card-kicker">NOW PLAYING (WIN32 MCI NORMALIZED OST)</div>
        <div class="nvl-card-title" id="nvl-bgm-title">--</div>
        <div class="nvl-card-sub" id="nvl-bgm-sub">--</div>
      </div>
    </aside>

    <article id="nvl-right-pane"></article>
  </section>
</main>

<!-- Backlog Modal [B] -->
<div class="modal-backdrop" id="backlog-modal">
  <div class="modal-panel">
    <div class="modal-head">
      <h2>Dialogue & Narration Backlog</h2>
      <button class="ctrl-btn" data-close="backlog-modal">Close [Esc]</button>
    </div>
    <div class="modal-body" id="backlog-list"></div>
  </div>
</div>

<!-- CG Gallery Modal [G] -->
<div class="modal-backdrop" id="cg-gallery-modal">
  <div class="modal-panel">
    <div class="modal-head">
      <h2>Full-Color Illustration Gallery (cg-gallery)</h2>
      <button class="ctrl-btn" data-close="cg-gallery-modal">Close [Esc]</button>
    </div>
    <div class="modal-body">
      <div class="cg-gallery-grid" id="cg-gallery-grid"></div>
    </div>
  </div>
</div>

<!-- Work Info Modal [I] -->
<div class="modal-backdrop" id="info-modal">
  <div class="modal-panel">
    <div class="modal-head">
      <h2>EagleLit Pack v1.0 -- Work Overview & Verbatim Provenance</h2>
      <button class="ctrl-btn" data-close="info-modal">Close [Esc]</button>
    </div>
    <div class="modal-body" id="info-body"></div>
  </div>
</div>

<!-- Fullscreen Lightbox -->
<div id="cg-lightbox">
  <img id="cg-lightbox-img" alt="Fullscreen CG">
  <div id="cg-lightbox-caption"></div>
</div>

<!-- Dual HTML5 Audio Decks for Smooth Crossfade -->
<audio id="audio-deck-a" preload="auto" loop></audio>
<audio id="audio-deck-b" preload="auto" loop></audio>

<!-- Embedded CORS-Free Pack Payload -->
<script id="eagle-lit-pack-data" type="application/json">__EMBEDDED_PACK_JSON__</script>

<script>
(function () {
  "use strict";

  const rawJson = document.getElementById("eagle-lit-pack-data").textContent;
  const PACK = JSON.parse(rawJson);
  const MANIFEST = PACK.manifest;
  const CHAPTERS = PACK.chapters;

  // Build lookup maps
  const CAST_BY_NAME = {};
  (MANIFEST.cast || []).forEach((c) => {
    CAST_BY_NAME[c.display_name.toLowerCase()] = c;
    CAST_BY_NAME[c.char_key.toLowerCase()] = c;
  });

  // Flatten linear beat index for Mode 1 & block index for Mode 2
  const FLAT_BEATS = [];
  const CHAPTER_BLOCKS = [];

  CHAPTERS.forEach((ch, chIdx) => {
    const chBlocks = [];
    (ch.scenes || []).forEach((sc, scIdx) => {
      (sc.blocks || []).forEach((blk, blkIdx) => {
        const firstFlatBeatIdx = FLAT_BEATS.length;
        const beats = blk.vn_beats || [];
        beats.forEach((bt, btIdx) => {
          FLAT_BEATS.push({
            chIdx, scIdx, blkIdx, btIdx,
            chapter: ch, scene: sc, block: blk, beat: bt
          });
        });
        chBlocks.push({
          chIdx, scIdx, blkIdx,
          chapter: ch, scene: sc, block: blk,
          firstFlatBeatIdx: beats.length > 0 ? firstFlatBeatIdx : -1
        });
      });
    });
    CHAPTER_BLOCKS.push(chBlocks);
  });

  // Reader Synchronized Cursor
  const state = {
    mode: MANIFEST.default_read_mode || "adv_cinema",
    chIdx: 0,
    scIdx: 0,
    blkIdx: 0,
    btIdx: 0,
    flatBeatIdx: 0,
    autoPlay: false,
    speedIdx: 0,
    speeds: [1.0, 1.5, 2.0],
    uiHidden: false,
    safeZoneVisible: false,
    bgmMuted: false,
    masterVol: 0.75,
    currentCgId: null,
    currentBgmId: null,
    cinemaFrontA: true,
    nvlFrontA: true,
    activeDeck: "a",
    suppressScrollSync: false
  };

  // DOM Elements
  const bodyEl = document.body;
  const topTitleEl = document.getElementById("top-title");
  const venueBadgeEl = document.getElementById("venue-badge");
  const chSelectEl = document.getElementById("chapter-select");
  const scSelectEl = document.getElementById("scene-select");
  const btnModeEl = document.getElementById("btn-mode-toggle");
  const btnAutoEl = document.getElementById("btn-autoplay");
  const btnSpeedEl = document.getElementById("btn-speed");
  const btnMuteEl = document.getElementById("btn-bgm-mute");
  const volRangeEl = document.getElementById("bgm-volume");

  const hudSpeakerEl = document.getElementById("hud-speaker");
  const hudMetaEl = document.getElementById("hud-meta-pill");
  const hudTextEl = document.getElementById("hud-text");
  const hudThoughtEl = document.getElementById("hud-inner-thought");
  const hudBgmEl = document.getElementById("hud-bgm-label");
  const hudProgEl = document.getElementById("hud-progress-label");
  const autoBarEl = document.getElementById("autoplay-bar");
  const fxOverlayEl = document.getElementById("scene-fx-overlay");

  const nvlRightEl = document.getElementById("nvl-right-pane");
  const nvlScKickerEl = document.getElementById("nvl-scene-kicker");
  const nvlScTitleEl = document.getElementById("nvl-scene-title");
  const nvlScLocEl = document.getElementById("nvl-scene-loc");
  const nvlScAmbEl = document.getElementById("nvl-scene-ambience");
  const nvlBgmTitleEl = document.getElementById("nvl-bgm-title");
  const nvlBgmSubEl = document.getElementById("nvl-bgm-sub");

  const deckA = document.getElementById("audio-deck-a");
  const deckB = document.getElementById("audio-deck-b");
  let fadeTimer = null;
  let autoTimer = null;
  let autoAnimReq = null;

  function initHeader() {
    topTitleEl.textContent = MANIFEST.short_title || MANIFEST.title;
    venueBadgeEl.textContent = (MANIFEST.venue_type || "cinema").toUpperCase();

    CHAPTERS.forEach((ch, idx) => {
      const opt = document.createElement("option");
      opt.value = String(idx);
      opt.textContent = ch.title;
      chSelectEl.appendChild(opt);
    });
    populateSceneDropdown(0);
    updateModeButtonLabel();
  }

  function populateSceneDropdown(chIdx) {
    scSelectEl.innerHTML = "";
    const ch = CHAPTERS[chIdx];
    if (!ch) return;
    (ch.scenes || []).forEach((sc, sIdx) => {
      const opt = document.createElement("option");
      opt.value = String(sIdx);
      opt.textContent = (sIdx + 1) + ". " + sc.title;
      scSelectEl.appendChild(opt);
    });
  }

  function updateModeButtonLabel() {
    bodyEl.setAttribute("data-mode", state.mode);
    btnModeEl.textContent = state.mode === "adv_cinema"
      ? "Mode [V]: Cinema 16:9"
      : "Mode [V]: Book Split";
    btnModeEl.classList.toggle("active", state.mode === "nvl_book");
  }

  // Double-buffered CG crossfader with graceful fallback for pending quota items
  function applyCgToStage(cgId, imgAId, imgBId, pendingBoxId, pendingIdEl, pendingSumEl, isCinema) {
    const cgMeta = (MANIFEST.cg_catalog || {})[cgId];
    const imgA = document.getElementById(imgAId);
    const imgB = document.getElementById(imgBId);
    const pendingBox = document.getElementById(pendingBoxId);

    const fp = (cgMeta && Array.isArray(cgMeta.focal_point)) ? cgMeta.focal_point : [0.5, 0.42];
    const objPos = (fp[0] * 100).toFixed(1) + "% " + (fp[1] * 100).toFixed(1) + "%";

    if (!cgMeta || !cgMeta.file || PACK.missing_files[cgMeta.file]) {
      imgA.classList.remove("visible");
      imgB.classList.remove("visible");
      pendingBox.classList.add("visible");
      document.getElementById(pendingIdEl).textContent = cgId || "Unknown CG";
      document.getElementById(pendingSumEl).textContent = cgMeta ? cgMeta.scene_summary : "";
      return;
    }

    pendingBox.classList.remove("visible");
    const useA = isCinema ? !state.cinemaFrontA : !state.nvlFrontA;
    const nextImg = useA ? imgA : imgB;
    const prevImg = useA ? imgB : imgA;

    nextImg.style.objectPosition = objPos;
    if (nextImg.getAttribute("data-cg") !== cgId) {
      nextImg.src = "./" + cgMeta.file;
      nextImg.setAttribute("data-cg", cgId);
    }
    nextImg.onerror = function () {
      nextImg.classList.remove("visible");
      pendingBox.classList.add("visible");
      document.getElementById(pendingIdEl).textContent = cgId;
      document.getElementById(pendingSumEl).textContent = cgMeta.scene_summary || "";
    };
    nextImg.classList.add("visible");
    prevImg.classList.remove("visible");

    if (isCinema) state.cinemaFrontA = useA;
    else state.nvlFrontA = useA;
  }

  function updateVisualCg(cgId) {
    if (!cgId || cgId === state.currentCgId) return;
    state.currentCgId = cgId;
    applyCgToStage(cgId, "cinema-cg-a", "cinema-cg-b", "cinema-cg-pending", "cinema-pending-id", "cinema-pending-summary", true);
    applyCgToStage(cgId, "nvl-cg-a", "nvl-cg-b", "nvl-cg-pending", "nvl-pending-id", "nvl-pending-summary", false);
  }

  function triggerSceneTransitionFx(kind) {
    if (!kind || kind === "cut" || kind === "dissolve") return;
    let color = "#000";
    if (kind === "fade_white") color = "#f8f9fc";
    else if (kind === "flash_crimson") color = "rgba(190, 18, 38, 0.85)";
    fxOverlayEl.style.transition = "none";
    fxOverlayEl.style.background = color;
    fxOverlayEl.style.opacity = "0.92";
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        fxOverlayEl.style.transition = "opacity 0.52s ease";
        fxOverlayEl.style.opacity = "0";
      });
    });
  }

  // Dual-Deck Audio Crossfade Engine
  function targetDeckVolume(bgmId) {
    if (state.bgmMuted) return 0;
    const meta = (MANIFEST.bgm_catalog || {})[bgmId];
    const mci = (meta && meta.base_mci_volume) ? meta.base_mci_volume : 215;
    return Math.min(1, Math.max(0, (mci / 240) * state.masterVol));
  }

  function switchBgm(bgmId) {
    const meta = (MANIFEST.bgm_catalog || {})[bgmId];
    const label = meta ? meta.title : (bgmId || "--");
    hudBgmEl.textContent = "BGM: " + label;
    nvlBgmTitleEl.textContent = label;
    nvlBgmSubEl.textContent = meta
      ? (meta.composer_or_origin + "  |  MCI Vol " + meta.base_mci_volume + "  |  " + meta.fallback_mood_bucket)
      : "--";

    if (!bgmId || bgmId === state.currentBgmId || !meta || !meta.file || PACK.missing_files[meta.file]) {
      state.currentBgmId = bgmId;
      return;
    }
    state.currentBgmId = bgmId;

    const incoming = state.activeDeck === "a" ? deckB : deckA;
    const outgoing = state.activeDeck === "a" ? deckA : deckB;
    state.activeDeck = state.activeDeck === "a" ? "b" : "a";

    incoming.src = "./" + meta.file;
    incoming.loop = meta.loop !== false;
    incoming.volume = 0;
    const playPromise = incoming.play();
    if (playPromise && typeof playPromise.catch === "function") {
      playPromise.catch(() => {});
    }

    const goalVol = targetDeckVolume(bgmId);
    const startOutVol = outgoing.volume;
    let step = 0;
    const steps = 18;
    if (fadeTimer) clearInterval(fadeTimer);
    fadeTimer = setInterval(() => {
      step++;
      const t = step / steps;
      incoming.volume = goalVol * t;
      outgoing.volume = Math.max(0, startOutVol * (1 - t));
      if (step >= steps) {
        clearInterval(fadeTimer);
        fadeTimer = null;
        outgoing.pause();
      }
    }, 45);
  }

  function refreshActiveDeckVolume() {
    const active = state.activeDeck === "a" ? deckA : deckB;
    active.volume = targetDeckVolume(state.currentBgmId);
    if (!state.bgmMuted && active.paused && active.src) {
      const p = active.play();
      if (p && typeof p.catch === "function") p.catch(() => {});
    }
  }

  // Render Mode 1 Beat
  function goToFlatBeat(idx, fromScrollSync) {
    if (!FLAT_BEATS.length) return;
    const clamped = Math.max(0, Math.min(FLAT_BEATS.length - 1, idx));
    const prevBeat = FLAT_BEATS[state.flatBeatIdx];
    const item = FLAT_BEATS[clamped];
    const sceneChanged = !prevBeat || prevBeat.scene.scene_id !== item.scene.scene_id;
    const chapterChanged = !prevBeat || prevBeat.chIdx !== item.chIdx;

    state.flatBeatIdx = clamped;
    state.chIdx = item.chIdx;
    state.scIdx = item.scIdx;
    state.blkIdx = item.blkIdx;
    state.btIdx = item.btIdx;

    if (chapterChanged) {
      chSelectEl.value = String(item.chIdx);
      populateSceneDropdown(item.chIdx);
      if (state.mode === "nvl_book" && !fromScrollSync) {
        renderBookChapter(item.chIdx);
      }
    }
    scSelectEl.value = String(item.scIdx);

    if (sceneChanged && state.mode === "adv_cinema") {
      triggerSceneTransitionFx(item.scene.transition_in);
    }

    // Speaker styling
    const speaker = item.beat.speaker || "Narrator";
    hudSpeakerEl.textContent = speaker;
    const castEntry = CAST_BY_NAME[speaker.toLowerCase()];
    if (castEntry && Array.isArray(castEntry.nameplate_color_rgb)) {
      const c = castEntry.nameplate_color_rgb;
      hudSpeakerEl.style.borderLeftColor = "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
      hudSpeakerEl.style.color = "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
    } else {
      hudSpeakerEl.style.borderLeftColor = "var(--accent)";
      hudSpeakerEl.style.color = "#cbd5e8";
    }

    hudMetaEl.textContent = item.chapter.title + "  /  " + item.scene.title;
    hudTextEl.textContent = item.beat.text || "";
    hudThoughtEl.textContent = item.beat.inner_thought || "";
    hudThoughtEl.style.display = item.beat.inner_thought ? "block" : "none";
    hudProgEl.textContent =
      "Beat " + (clamped + 1) + " / " + FLAT_BEATS.length +
      "  |  Block " + item.block.block_id +
      "  |  [Space/Click] Next  [V] Book Mode";

    updateSceneCard(item.chapter, item.scene);
    updateVisualCg(item.beat.cg_id || item.block.cg_id);
    switchBgm(item.beat.bgm_id || item.block.bgm_id || item.scene.default_bgm_id);

    if (state.mode === "nvl_book" && !fromScrollSync) {
      highlightAndScrollBookBlock(item.block.block_id, true);
    }

    scheduleNextAutoStep();
  }

  function updateSceneCard(chapter, scene) {
    nvlScKickerEl.textContent = chapter.title + "  --  " + (scene.time_of_day || "");
    nvlScTitleEl.textContent = scene.title || "";
    nvlScLocEl.textContent = scene.location || "";
    nvlScAmbEl.textContent = scene.ambience_note || "";
  }

  // Render Mode 2 Scrollable Verbatim Book Chapter
  function renderBookChapter(chIdx) {
    const ch = CHAPTERS[chIdx];
    if (!ch) return;
    state.suppressScrollSync = true;
    nvlRightEl.innerHTML = "";

    const header = document.createElement("div");
    header.className = "book-chapter-header";
    const h1 = document.createElement("h1");
    h1.textContent = ch.title;
    const sub = document.createElement("p");
    sub.textContent = "Mode 2 (nvl_book) -- 100% Verbatim Source Text  |  Scroll page to auto-sync Left CG & BGM";
    header.appendChild(h1);
    header.appendChild(sub);
    nvlRightEl.appendChild(header);

    let lastSceneId = null;
    (CHAPTER_BLOCKS[chIdx] || []).forEach((entry, flatBlkIdx) => {
      if (entry.scene.scene_id !== lastSceneId) {
        lastSceneId = entry.scene.scene_id;
        const sDiv = document.createElement("div");
        sDiv.className = "book-scene-divider";
        sDiv.textContent = entry.scene.title + "  //  " + entry.scene.location + " (" + entry.scene.time_of_day + ")";
        nvlRightEl.appendChild(sDiv);
      }

      const p = document.createElement("div");
      const kind = entry.block.kind || "narration";
      p.className = "book-block kind-" + kind;
      p.id = "dom-blk-" + entry.block.block_id;
      p.setAttribute("data-ch", String(chIdx));
      p.setAttribute("data-ch-blk-idx", String(flatBlkIdx));
      p.textContent = entry.block.book_text || "";

      p.addEventListener("click", () => {
        activateBookBlock(chIdx, flatBlkIdx, false);
      });
      nvlRightEl.appendChild(p);
    });

    const footer = document.createElement("div");
    footer.className = "book-chapter-nav-footer";
    if (chIdx > 0) {
      const prevBtn = document.createElement("button");
      prevBtn.className = "ctrl-btn";
      prevBtn.textContent = "<- Previous Chapter";
      prevBtn.onclick = () => jumpToChapter(chIdx - 1);
      footer.appendChild(prevBtn);
    } else {
      footer.appendChild(document.createElement("span"));
    }
    if (chIdx < CHAPTERS.length - 1) {
      const nextBtn = document.createElement("button");
      nextBtn.className = "ctrl-btn";
      nextBtn.textContent = "Next Chapter ->";
      nextBtn.onclick = () => jumpToChapter(chIdx + 1);
      footer.appendChild(nextBtn);
    }
    nvlRightEl.appendChild(footer);

    setTimeout(() => { state.suppressScrollSync = false; }, 120);
  }

  function highlightAndScrollBookBlock(blockId, smoothScroll) {
    const prev = nvlRightEl.querySelector(".book-block.active-block");
    if (prev) prev.classList.remove("active-block");
    const el = document.getElementById("dom-blk-" + blockId);
    if (!el) return;
    el.classList.add("active-block");
    if (smoothScroll) {
      state.suppressScrollSync = true;
      const paneRect = nvlRightEl.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const targetTop = nvlRightEl.scrollTop + (elRect.top - paneRect.top) - paneRect.height * 0.24;
      nvlRightEl.scrollTo({ top: Math.max(0, targetTop), behavior: "smooth" });
      setTimeout(() => { state.suppressScrollSync = false; }, 380);
    }
  }

  function activateBookBlock(chIdx, chBlkIdx, fromScroll) {
    const list = CHAPTER_BLOCKS[chIdx] || [];
    const entry = list[chBlkIdx];
    if (!entry) return;

    state.chIdx = chIdx;
    state.scIdx = entry.scIdx;
    state.blkIdx = entry.blkIdx;
    scSelectEl.value = String(entry.scIdx);

    updateSceneCard(entry.chapter, entry.scene);
    updateVisualCg(entry.block.cg_id);
    switchBgm(entry.block.bgm_id || entry.scene.default_bgm_id);
    highlightAndScrollBookBlock(entry.block.block_id, !fromScroll);

    // Synchronize flatBeatIdx to nearest non-break block so [V] switches with 0 drift
    if (entry.firstFlatBeatIdx >= 0) {
      state.flatBeatIdx = entry.firstFlatBeatIdx;
      state.btIdx = 0;
    } else {
      // On a `---` scene_break: anchor to the next prose block in chapter if available
      for (let i = chBlkIdx + 1; i < list.length; i++) {
        if (list[i].firstFlatBeatIdx >= 0) {
          state.flatBeatIdx = list[i].firstFlatBeatIdx;
          break;
        }
      }
    }
  }

  // Scroll listener on Mode 2 right pane -> auto-sync CG & BGM
  let scrollTicking = false;
  nvlRightEl.addEventListener("scroll", () => {
    if (state.mode !== "nvl_book" || state.suppressScrollSync) return;
    if (scrollTicking) return;
    scrollTicking = true;
    requestAnimationFrame(() => {
      scrollTicking = false;
      const paneRect = nvlRightEl.getBoundingClientRect();
      const focusY = paneRect.top + paneRect.height * 0.30;
      const blocks = nvlRightEl.querySelectorAll(".book-block");
      let bestEl = null;
      let bestDist = Infinity;
      blocks.forEach((el) => {
        const r = el.getBoundingClientRect();
        const mid = (r.top + r.bottom) * 0.5;
        const dist = Math.abs(mid - focusY);
        if (r.top <= focusY + 60 && dist < bestDist) {
          bestDist = dist;
          bestEl = el;
        }
      });
      if (!bestEl && blocks.length > 0) bestEl = blocks[0];
      if (bestEl) {
        const chIdx = parseInt(bestEl.getAttribute("data-ch"), 10);
        const blkIdx = parseInt(bestEl.getAttribute("data-ch-blk-idx"), 10);
        activateBookBlock(chIdx, blkIdx, true);
      }
    });
  });

  // Instant Dual-Mode Switch [V]
  function toggleReadMode() {
    if (state.mode === "adv_cinema") {
      state.mode = "nvl_book";
      updateModeButtonLabel();
      renderBookChapter(state.chIdx);
      const item = FLAT_BEATS[state.flatBeatIdx];
      if (item) {
        highlightAndScrollBookBlock(item.block.block_id, true);
      }
    } else {
      state.mode = "adv_cinema";
      updateModeButtonLabel();
      goToFlatBeat(state.flatBeatIdx, false);
    }
    scheduleNextAutoStep();
  }

  // Auto-Play Engine ([A] & speed [1.0x / 1.5x / 2.0x])
  function clearAutoTimers() {
    if (autoTimer) { clearTimeout(autoTimer); autoTimer = null; }
    if (autoAnimReq) { cancelAnimationFrame(autoAnimReq); autoAnimReq = null; }
    autoBarEl.style.width = "0%";
  }

  function scheduleNextAutoStep() {
    clearAutoTimers();
    if (!state.autoPlay) return;
    const speed = state.speeds[state.speedIdx];

    if (state.mode === "adv_cinema") {
      const cur = FLAT_BEATS[state.flatBeatIdx];
      if (!cur || state.flatBeatIdx >= FLAT_BEATS.length - 1) {
        setAutoPlay(false);
        return;
      }
      const holdMs = Math.max(900, Math.round((cur.beat.auto_hold_ms || 4000) / speed));
      const t0 = performance.now();
      const tick = (now) => {
        const pct = Math.min(100, ((now - t0) / holdMs) * 100);
        autoBarEl.style.width = pct.toFixed(1) + "%";
        if (pct < 100 && state.autoPlay) {
          autoAnimReq = requestAnimationFrame(tick);
        }
      };
      autoAnimReq = requestAnimationFrame(tick);
      autoTimer = setTimeout(() => {
        goToFlatBeat(state.flatBeatIdx + 1, false);
      }, holdMs);
    } else {
      // Mode 2 Book Auto-Step
      const list = CHAPTER_BLOCKS[state.chIdx] || [];
      const curDom = nvlRightEl.querySelector(".book-block.active-block");
      const curIdx = curDom ? parseInt(curDom.getAttribute("data-ch-blk-idx"), 10) : 0;
      const curEntry = list[curIdx];
      const words = curEntry ? (curEntry.block.book_text || "").split(/\s+/).length : 18;
      const holdMs = Math.max(1500, Math.round(Math.min(10000, 1800 + words * 195) / speed));
      autoTimer = setTimeout(() => {
        if (curIdx + 1 < list.length) {
          activateBookBlock(state.chIdx, curIdx + 1, false);
          scheduleNextAutoStep();
        } else if (state.chIdx + 1 < CHAPTERS.length) {
          jumpToChapter(state.chIdx + 1);
          scheduleNextAutoStep();
        } else {
          setAutoPlay(false);
        }
      }, holdMs);
    }
  }

  function setAutoPlay(enabled) {
    state.autoPlay = enabled;
    btnAutoEl.textContent = enabled ? "Auto [A]: ON" : "Auto [A]: OFF";
    btnAutoEl.classList.toggle("active", enabled);
    scheduleNextAutoStep();
  }

  function jumpToChapter(chIdx) {
    const idx = FLAT_BEATS.findIndex((x) => x.chIdx === chIdx);
    if (state.mode === "nvl_book") {
      renderBookChapter(chIdx);
    }
    if (idx >= 0) {
      goToFlatBeat(idx, false);
    }
  }

  function jumpToScene(chIdx, scIdx) {
    const idx = FLAT_BEATS.findIndex((x) => x.chIdx === chIdx && x.scIdx === scIdx);
    if (idx >= 0) {
      if (state.mode === "nvl_book" && chIdx !== state.chIdx) {
        renderBookChapter(chIdx);
      }
      goToFlatBeat(idx, false);
    }
  }

  // Backlog Modal [B]
  function openBacklog() {
    const listEl = document.getElementById("backlog-list");
    listEl.innerHTML = "";
    const start = Math.max(0, state.flatBeatIdx - 120);
    for (let i = start; i <= state.flatBeatIdx; i++) {
      const item = FLAT_BEATS[i];
      if (!item) continue;
      const row = document.createElement("div");
      row.className = "backlog-item";
      const sp = document.createElement("div");
      sp.className = "backlog-speaker";
      sp.textContent = "[" + (i + 1) + "] " + item.beat.speaker + "  (" + item.scene.title + ")";
      const castEntry = CAST_BY_NAME[(item.beat.speaker || "").toLowerCase()];
      if (castEntry && Array.isArray(castEntry.nameplate_color_rgb)) {
        const c = castEntry.nameplate_color_rgb;
        sp.style.color = "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
      } else {
        sp.style.color = "#9aa4b8";
      }
      const tx = document.createElement("div");
      tx.className = "backlog-text";
      tx.textContent = item.beat.text + (item.beat.inner_thought ? "  (" + item.beat.inner_thought + ")" : "");
      row.appendChild(sp);
      row.appendChild(tx);
      row.onclick = () => {
        closeAllModals();
        goToFlatBeat(i, false);
      };
      listEl.appendChild(row);
    }
    document.getElementById("backlog-modal").classList.add("open");
    listEl.scrollTop = listEl.scrollHeight;
  }

  // CG Gallery Modal [G]
  function buildGallery() {
    const grid = document.getElementById("cg-gallery-grid");
    grid.innerHTML = "";
    const entries = [];
    const cover = MANIFEST.cover_assets || {};
    if (cover.poster_2x3_rel) {
      entries.push({
        id: "poster (832x1248)",
        file: cover.poster_2x3_rel,
        summary: "Vertical 2:3 Cinema Poster / Library Cover",
        isPoster: true
      });
    }
    if (cover.banner_16x9_rel) {
      entries.push({
        id: "banner_16x9 (1376x768)",
        file: cover.banner_16x9_rel,
        summary: "Widescreen 16:9 Selection Marquee Banner",
        isPoster: false
      });
    }
    Object.keys(MANIFEST.cg_catalog || {}).forEach((cgId) => {
      const m = MANIFEST.cg_catalog[cgId];
      entries.push({
        id: cgId + " [" + (m.kind || "cg").toUpperCase() + "]",
        file: m.file,
        summary: m.scene_summary || "",
        isPoster: false
      });
    });

    entries.forEach((e) => {
      const card = document.createElement("div");
      card.className = "cg-card" + (e.isPoster ? " poster-card" : "");
      if (!PACK.missing_files[e.file]) {
        const img = document.createElement("img");
        img.src = "./" + e.file;
        img.alt = e.id;
        card.appendChild(img);
        card.onclick = () => openLightbox("./" + e.file, e.id + " -- " + e.summary);
      } else {
        const miss = document.createElement("div");
        miss.style.padding = "32px 14px";
        miss.style.textAlign = "center";
        miss.style.fontSize = "12px";
        miss.style.color = "#f0a050";
        miss.textContent = "[PENDING QUOTA] " + e.file;
        card.appendChild(miss);
      }
      const meta = document.createElement("div");
      meta.className = "cg-card-meta";
      const idEl = document.createElement("div");
      idEl.className = "cg-card-id";
      idEl.textContent = e.id;
      const descEl = document.createElement("div");
      descEl.className = "cg-card-desc";
      descEl.textContent = e.summary;
      meta.appendChild(idEl);
      meta.appendChild(descEl);
      card.appendChild(meta);
      grid.appendChild(card);
    });
  }

  function openLightbox(src, caption) {
    document.getElementById("cg-lightbox-img").src = src;
    document.getElementById("cg-lightbox-caption").textContent = caption;
    document.getElementById("cg-lightbox").classList.add("open");
  }

  function buildInfoModal() {
    const body = document.getElementById("info-body");
    const prov = MANIFEST.source_provenance || {};
    const cover = MANIFEST.cover_assets || {};
    const posterHtml = (cover.poster_2x3_rel && !PACK.missing_files[cover.poster_2x3_rel])
      ? '<img class="info-poster" src="./' + cover.poster_2x3_rel + '" alt="Poster">'
      : "";
    body.innerHTML =
      '<div class="info-hero">' +
        posterHtml +
        '<div>' +
          '<h3 style="font-size:20px;margin-bottom:4px;">' + escapeHtml(MANIFEST.title || "") + '</h3>' +
          '<p style="font-size:13px;color:#9aa4b8;margin-bottom:10px;">' +
            escapeHtml((MANIFEST.subtitle || "") + "  |  Author: " + (MANIFEST.author || "")) +
          '</p>' +
          '<p style="font-size:14px;line-height:1.55;margin-bottom:12px;">' + escapeHtml(MANIFEST.synopsis || "") + '</p>' +
          '<div class="stat-badges">' +
            '<span class="stat-badge">Art Style: ' + escapeHtml(MANIFEST.art_style_authority || "") + '</span>' +
            '<span class="stat-badge">Translation: ' + escapeHtml(prov.translation_credit || "") + '</span>' +
            '<span class="stat-badge">Book Words: ' + (prov.pack_book_word_count || 0) + ' / ' + (prov.raw_word_count || 0) + '</span>' +
            '<span class="stat-badge">VN Words: ' + (prov.pack_vn_word_count || 0) + '</span>' +
            '<span class="stat-badge">Chapters: ' + CHAPTERS.length + '</span>' +
            '<span class="stat-badge">Total Beats: ' + FLAT_BEATS.length + '</span>' +
            '<span class="stat-badge">CGs: ' + Object.keys(MANIFEST.cg_catalog || {}).length + '</span>' +
            '<span class="stat-badge">BGM Tracks: ' + Object.keys(MANIFEST.bgm_catalog || {}).length + '</span>' +
          '</div>' +
        '</div>' +
      '</div>';
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function closeAllModals() {
    document.querySelectorAll(".modal-backdrop.open").forEach((m) => m.classList.remove("open"));
    document.getElementById("cg-lightbox").classList.remove("open");
  }

  function anyModalOpen() {
    return Boolean(document.querySelector(".modal-backdrop.open") || document.getElementById("cg-lightbox").classList.contains("open"));
  }

  // Event Wiring
  document.getElementById("cinema-stage-16x9").addEventListener("click", () => {
    if (state.uiHidden) {
      state.uiHidden = false;
      bodyEl.classList.remove("ui-hidden");
      return;
    }
    if (state.flatBeatIdx < FLAT_BEATS.length - 1) {
      goToFlatBeat(state.flatBeatIdx + 1, false);
    }
  });

  chSelectEl.addEventListener("change", () => jumpToChapter(parseInt(chSelectEl.value, 10)));
  scSelectEl.addEventListener("change", () => jumpToScene(state.chIdx, parseInt(scSelectEl.value, 10)));
  btnModeEl.addEventListener("click", toggleReadMode);
  btnAutoEl.addEventListener("click", () => setAutoPlay(!state.autoPlay));
  btnSpeedEl.addEventListener("click", () => {
    state.speedIdx = (state.speedIdx + 1) % state.speeds.length;
    btnSpeedEl.textContent = state.speeds[state.speedIdx].toFixed(1) + "x";
    scheduleNextAutoStep();
  });

  document.getElementById("btn-backlog").addEventListener("click", openBacklog);
  document.getElementById("btn-gallery").addEventListener("click", () => {
    buildGallery();
    document.getElementById("cg-gallery-modal").classList.add("open");
  });
  document.getElementById("btn-info").addEventListener("click", () => {
    buildInfoModal();
    document.getElementById("info-modal").classList.add("open");
  });
  document.getElementById("btn-safezone").addEventListener("click", (ev) => {
    state.safeZoneVisible = !state.safeZoneVisible;
    document.getElementById("safe-zone-guide").classList.toggle("visible", state.safeZoneVisible);
    ev.currentTarget.classList.toggle("active", state.safeZoneVisible);
  });
  document.getElementById("btn-hide-ui").addEventListener("click", () => {
    state.uiHidden = !state.uiHidden;
    bodyEl.classList.toggle("ui-hidden", state.uiHidden);
  });
  btnMuteEl.addEventListener("click", () => {
    state.bgmMuted = !state.bgmMuted;
    btnMuteEl.textContent = state.bgmMuted ? "BGM: OFF" : "BGM: ON";
    refreshActiveDeckVolume();
  });
  volRangeEl.addEventListener("input", () => {
    state.masterVol = parseInt(volRangeEl.value, 10) / 100;
    refreshActiveDeckVolume();
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.getElementById(btn.getAttribute("data-close")).classList.remove("open");
    });
  });
  document.getElementById("cg-lightbox").addEventListener("click", () => {
    document.getElementById("cg-lightbox").classList.remove("open");
  });

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (anyModalOpen()) { closeAllModals(); return; }
      if (state.uiHidden) { state.uiHidden = false; bodyEl.classList.remove("ui-hidden"); return; }
    }
    if (anyModalOpen()) return;

    const k = e.key.toLowerCase();
    if (k === "v") { e.preventDefault(); toggleReadMode(); }
    else if (k === "a") { e.preventDefault(); setAutoPlay(!state.autoPlay); }
    else if (k === "b") { e.preventDefault(); openBacklog(); }
    else if (k === "g") { e.preventDefault(); buildGallery(); document.getElementById("cg-gallery-modal").classList.add("open"); }
    else if (k === "i") { e.preventDefault(); buildInfoModal(); document.getElementById("info-modal").classList.add("open"); }
    else if (k === "h") { e.preventDefault(); state.uiHidden = !state.uiHidden; bodyEl.classList.toggle("ui-hidden", state.uiHidden); }
    else if (k === "s") {
      e.preventDefault();
      state.safeZoneVisible = !state.safeZoneVisible;
      document.getElementById("safe-zone-guide").classList.toggle("visible", state.safeZoneVisible);
    } else if (k === "m") {
      e.preventDefault();
      state.bgmMuted = !state.bgmMuted;
      btnMuteEl.textContent = state.bgmMuted ? "BGM: OFF" : "BGM: ON";
      refreshActiveDeckVolume();
    } else if (state.mode === "adv_cinema") {
      if (e.key === " " || e.key === "Enter" || e.key === "ArrowRight") {
        e.preventDefault();
        if (state.flatBeatIdx < FLAT_BEATS.length - 1) goToFlatBeat(state.flatBeatIdx + 1, false);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        if (state.flatBeatIdx > 0) goToFlatBeat(state.flatBeatIdx - 1, false);
      }
    } else if (state.mode === "nvl_book") {
      const list = CHAPTER_BLOCKS[state.chIdx] || [];
      const curDom = nvlRightEl.querySelector(".book-block.active-block");
      const curIdx = curDom ? parseInt(curDom.getAttribute("data-ch-blk-idx"), 10) : 0;
      if (e.key === "ArrowDown" || e.key === " ") {
        e.preventDefault();
        if (curIdx + 1 < list.length) activateBookBlock(state.chIdx, curIdx + 1, false);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        if (curIdx > 0) activateBookBlock(state.chIdx, curIdx - 1, false);
      }
    }
  });

  // Boot Previewer
  initHeader();
  renderBookChapter(0);
  goToFlatBeat(0, false);
})();
</script>
</body>
</html>
"""


def build_preview_html(pack_dir: Path, output_path: Path | None = None) -> Path:
    """Embed manifest & chapter JSONs into a self-contained Dual-Mode preview.html."""
    pack_dir = pack_dir.resolve()
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest.json not found in {pack_dir}")

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters_list: list[dict[str, Any]] = []
    for ch_meta in manifest.get("chapters", []):
        ch_rel = ch_meta.get("file", "")
        ch_file = pack_dir / ch_rel
        if not ch_file.is_file():
            raise FileNotFoundError(f"Chapter JSON missing: {ch_file}")
        chapters_list.append(json.loads(ch_file.read_text(encoding="utf-8")))

    # Record missing assets so partial quota-wait previews degrade gracefully
    missing_files: dict[str, bool] = {}
    cover = manifest.get("cover_assets", {})
    for rel in (cover.get("poster_2x3_rel"), cover.get("banner_16x9_rel")):
        if isinstance(rel, str) and rel and not (pack_dir / rel).is_file():
            missing_files[rel] = True
    for cg_meta in (manifest.get("cg_catalog") or {}).values():
        rel = cg_meta.get("file") if isinstance(cg_meta, dict) else None
        if isinstance(rel, str) and rel and not (pack_dir / rel).is_file():
            missing_files[rel] = True
    for bgm_meta in (manifest.get("bgm_catalog") or {}).values():
        rel = bgm_meta.get("file") if isinstance(bgm_meta, dict) else None
        if isinstance(rel, str) and rel and not (pack_dir / rel).is_file():
            missing_files[rel] = True

    embedded_payload = {
        "manifest": manifest,
        "chapters": chapters_list,
        "missing_files": missing_files,
    }
    safe_json = json.dumps(embedded_payload, ensure_ascii=True).replace(
        "</script>", "<\\/script>"
    )

    rgb = manifest.get("theme_color_rgb")
    if not (isinstance(rgb, list) and len(rgb) == 3):
        rgb = [220, 38, 68]

    default_mode = manifest.get("default_read_mode", "adv_cinema")
    if default_mode not in {"adv_cinema", "nvl_book"}:
        default_mode = "adv_cinema"

    title_escaped = html.escape(str(manifest.get("title", manifest.get("work_id", "EagleLit Pack"))))

    rendered_html = (
        HTML_TEMPLATE.replace("__PAGE_TITLE__", title_escaped)
        .replace("__ACCENT_R__", str(int(rgb[0])))
        .replace("__ACCENT_G__", str(int(rgb[1])))
        .replace("__ACCENT_B__", str(int(rgb[2])))
        .replace("__DEFAULT_MODE__", default_mode)
        .replace("__EMBEDDED_PACK_JSON__", safe_json)
    )

    out_file = output_path.resolve() if output_path else (pack_dir / "preview.html")
    out_file.write_text(rendered_html, encoding="utf-8")
    return out_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build standalone CORS-free Dual-Mode preview.html for an EagleLit Pack v1.0."
    )
    parser.add_argument(
        "pack_dir",
        help="Path to package directory (e.g. packages/rezero_ayamatsu_pride_if).",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Optional custom output path (defaults to <pack_dir>/preview.html).",
    )

    args = parser.parse_args(argv)
    pack_path = Path(args.pack_dir)
    out_opt = Path(args.output) if args.output else None

    try:
        built_path = build_preview_html(pack_path, output_path=out_opt)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Built Dual-Mode preview: {built_path} ({built_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
