#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
  GLOBAL DOMINANCE TERMINAL HD   (gdthd.py)
  A fullscreen 3D-ASCII-globe idle tycoon for modern terminals.
==============================================================================

  Run it in a LARGE terminal (recommended 160-240 columns x 60-80 lines, a
  truecolor-capable terminal such as Windows Terminal, iTerm2, kitty, WezTerm,
  GNOME Terminal, Alacritty ...).  Minimum usable size: 100 x 36.

      python3 gdthd.py

  Only the standard library is used (math, time, os, sys, json, ...).

  HOW THE GLOBE IS RENDERED
  -------------------------
  * Terrain layer : every character cell inside the globe disc is ray-cast
    onto the unit sphere (inverse orthographic projection), un-rotated with
    Yaw + Pitch rotation matrices and sampled from a dense 720x360 procedural
    lat/lon world texture.  This gives a hole-free, smoothly curved surface.
  * Node layer    : a Fibonacci sphere lattice (thousands of points) is
    rotated with the same matrices and forward-projected.  Lattice points that
    sit on land light up as glowing nodes as you expand your empire.
  * Shading       : Lambert diffuse + specular + atmospheric limb glow +
    smooth day/night terminator -> 11-step ASCII ramp  " .:-=+*#%@$"  layered
    with 24-bit ANSI RGB colour.  The far side of the globe is culled.
  * Depth         : painter's algorithm with an orbit ring that is correctly
    hidden behind the sphere when it passes behind it.
  * Output        : the entire frame is built as ONE string and written with a
    single call after a cursor-home (no clear) -> flicker free.

  COMMANDS (type while the globe keeps spinning)
  ----------------------------------------------
      buy <#> [qty|max]     buy levels of a venture
      upgrade <#>           upgrade a venture's tier (x2 income)
      buyall                greedily buy the cheapest levels you can afford
      work [game]           work minigame for randomised cash; games:
                            timing code math memory reflex count
      repair                pay to end all active crises (storms, strikes ...)
      focus on|off          camera turns to the hub you just bought/upgraded
      status                detailed empire report
      spin <deg/s>          globe rotation speed (negative = reverse)
      tilt <deg>            globe axial tilt (-80 .. 80)
      fps <n>               target frame rate (5 .. 60)
      save / load / reset confirm / help / quit
"""

import json
import math
import os
import random
import shutil
import sys
import textwrap
import time
from collections import deque

IS_WIN = os.name == "nt"
if IS_WIN:
    import msvcrt
else:
    import select
    import termios
    import tty

# --------------------------------------------------------------------------
#  Constants
# --------------------------------------------------------------------------
PI = math.pi
TAU = 2.0 * PI
HALF_PI = PI / 2.0
RAMP = " .:-=+*#%@$"          # expanded grayscale / density ramp
NODE_RAMP = ".:+*#%@$"        # glowing node ramp (indices 0..7)
TW, TH = 720, 360             # world texture resolution (0.5 degree texels)
USC = TW / TAU
VSC = TH / PI
RING_R = 1.28
RING_TILT = 0.30
RING_ROLL = -0.38
SAVE_PATH = os.path.join(os.path.expanduser("~"), ".gdthd_save.json")
# ---- difficulty / balance ----
COST_GROWTH = 1.155           # price increase per level bought (was 1.15)
VENTURE_COST_STEP = 1.05      # each later venture costs this much more than the one before
UPGRADE_BASE = 55.0           # tier upgrade = cost * UPGRADE_BASE * UPGRADE_GROWTH ** tier
UPGRADE_GROWTH = 6.0
UNLOCK_EARNED = 1.5           # a venture unlocks once lifetime earnings reach 1.5x its price
OFFLINE_RATE = 0.35           # share of income earned while the game is closed
WORK_COOLDOWN = 9.0           # seconds between work shifts
WORK_RPS_SECONDS = 27.0       # a work shift pays about this many seconds of income
BAD_EVENT_CHANCE = 0.5        # share of world events that are crises
MAX_TIER = 10
LOG_MAX = 60                  # log entries kept (shown in the sidebar, as many as fit)
HIST_MAX = 50                 # command history entries (Up / Down arrows)
RESET = "\x1b[0m"
BAR_BG = "\x1b[48;2;12;20;38m"   # background of the header and prompt bars
RULE_COL = (50, 70, 110)
WIN_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "M": "\x1b[C", "K": "\x1b[D"}  # msvcrt arrows

SUFFIX = ["", "K", "M", "B", "T", "Qa", "Qi", "Sx", "Sp", "Oc", "No", "Dc"]

REGIONS = [   # lat/lon = where the zone's name is printed on the globe
    # colours: distinct from each other, from the ocean blue and from crisis red
    {"name": "N.America", "tag": "NAM", "col": (255, 150, 40), "lat": 53.0, "lon": -102.0},
    {"name": "S.America", "tag": "SAM", "col": (110, 230, 90), "lat": -9.0, "lon": -58.0},
    {"name": "Europe", "tag": "EUR", "col": (80, 170, 255), "lat": 56.0, "lon": 24.0},
    {"name": "Africa", "tag": "AFR", "col": (245, 215, 60), "lat": 2.0, "lon": 22.0},
    {"name": "Asia", "tag": "ASI", "col": (255, 90, 180), "lat": 50.0, "lon": 95.0},
    {"name": "Oceania", "tag": "OCE", "col": (60, 235, 200), "lat": -30.0, "lon": 140.0},
    {"name": "Space", "tag": "SPC", "col": (180, 135, 255)},
]
REG_COLS = [r["col"] for r in REGIONS]
NREG = len(REGIONS)
SPACE = NREG - 1              # the orbital "region"
NLAND = NREG - 1              # land regions are 0 .. NLAND-1
T_ICE = NLAND + 1             # texture codes: 0 sea, 1..NLAND land, T_ICE ice, T_GRID grid
T_GRID = NLAND + 2

VENTURES = [
    {"name": "Pacific Logistics", "region": 0, "cost": 15.0, "income": 1.0,
     "lat": 37.0, "lon": -120.0},
    {"name": "Andes Mining Co.", "region": 1, "cost": 110.0, "income": 6.5,
     "lat": -15.0, "lon": -70.0},
    {"name": "Lagos Mobile Money", "region": 3, "cost": 900.0, "income": 40.0,
     "lat": 6.5, "lon": 3.4},
    {"name": "Rhine Fintech", "region": 2, "cost": 7500.0, "income": 220.0,
     "lat": 50.0, "lon": 8.0},
    {"name": "Pilbara Lithium Co.", "region": 5, "cost": 6.5e4, "income": 1200.0,
     "lat": -21.0, "lon": 119.0},
    {"name": "Shenzhen Robotics", "region": 4, "cost": 5.5e5, "income": 6500.0,
     "lat": 23.0, "lon": 114.0},
    {"name": "Sahara Solar Grid", "region": 3, "cost": 4.8e6, "income": 3.6e4,
     "lat": 24.0, "lon": 12.0},
    {"name": "Sao Paulo Agritech", "region": 1, "cost": 4.2e7, "income": 2.0e5,
     "lat": -18.0, "lon": -48.0},
    {"name": "Mumbai Cloud Svcs", "region": 4, "cost": 3.8e8, "income": 1.1e6,
     "lat": 19.0, "lon": 73.0},
    {"name": "Toronto Quantum Lab", "region": 0, "cost": 3.5e9, "income": 6.2e6,
     "lat": 43.7, "lon": -79.4},
    {"name": "Nordic Fusion Power", "region": 2, "cost": 3.3e10, "income": 3.5e7,
     "lat": 62.0, "lon": 15.0},
    {"name": "Auckland Desal Co.", "region": 5, "cost": 3.2e11, "income": 2.0e8,
     "lat": -38.0, "lon": 175.0},
    {"name": "Orbital Solar Array", "region": SPACE, "cost": 3.2e12, "income": 1.15e9,
     "orbit": 0.4, "speed": 0.35},
    {"name": "Lunar Mining Base", "region": SPACE, "cost": 3.3e13, "income": 6.6e9,
     "orbit": 3.5, "speed": -0.22},
    {"name": "Mars Colony Corp", "region": SPACE, "cost": 3.5e14, "income": 3.8e10,
     "orbit": 2.0, "speed": 0.15},
]
NV = len(VENTURES)
for _i, _v in enumerate(VENTURES):
    _v["cost"] *= VENTURE_COST_STEP ** _i
LAND_HUBS = [i for i, v in enumerate(VENTURES) if "lat" in v]
DUMMY_HUB = len(LAND_HUBS)
HUB_OF = {vi: hb for hb, vi in enumerate(LAND_HUBS)}
# venture order of the old 8-venture saves (migrated by name on load)
OLD_NAMES = ["Pacific Logistics", "Andes Mining Co.", "Rhine Fintech", "Sahara Solar Grid",
             "Shenzhen Robotics", "Mumbai Cloud Svcs", "Orbital Solar Array", "Lunar Mining Base"]
MINI_NAMES = {"timing": "DEAL CLOSER", "code": "MAINFRAME BREACH", "math": "QUARTERLY AUDIT",
              "memory": "MARKET MEMORY", "reflex": "FLASH TRADE", "count": "INVENTORY COUNT"}
TIMING_SPEED = [0.55, 0.80, 1.10]
TIMING_ZONE = [0.24, 0.17, 0.11]
COUNT_SYMS = "$%&#@+"
COUNT_COLS = {"$": (120, 255, 150), "%": (255, 120, 210), "&": (255, 200, 90),
              "#": (90, 210, 255), "@": (235, 240, 250), "+": (180, 135, 255)}

# Stylised continents: (region, lat, lon, half-height deg, half-width deg)
# region 0..NLAND-1 = developable land, -1 = neutral (ice / Greenland)
BLOBS = [
    # North America
    (0, 47, -100, 22, 42), (0, 62, -112, 12, 38), (0, 66, -150, 7, 16),
    (0, 20, -100, 11, 14), (0, 16, -88, 6, 9), (0, 32, -80, 8, 12),
    # South America
    (1, -8, -62, 17, 17), (1, -27, -62, 20, 11), (1, -46, -70, 10, 6),
    (1, 5, -70, 8, 12), (1, -15, -45, 10, 8),
    # Europe
    (2, 50, 12, 9, 22), (2, 63, 16, 9, 11), (2, 41, -4, 6, 10),
    (2, 42, 14, 5, 5), (2, 55, -3, 5, 5), (2, 50, 38, 8, 12),
    # Africa
    (3, 12, 20, 24, 30), (3, -18, 24, 16, 16), (3, 6, -4, 9, 14),
    (3, -31, 25, 5, 8), (3, -19, 47, 6, 3),
    # Asia
    (4, 57, 85, 14, 65), (4, 61, 135, 10, 30), (4, 36, 85, 14, 35),
    (4, 26, 106, 14, 18), (4, 21, 78, 12, 10), (4, 25, 45, 9, 13),
    (4, 12, 102, 10, 6), (4, 36, 138, 5, 4), (4, -2, 115, 4, 20),
    (4, 40, 115, 8, 15),
    # Oceania
    (5, -25, 134, 12, 22), (5, -40, 174, 6, 5), (5, -6, 143, 4, 8),
    # Neutral
    (-1, 72, -40, 10, 18),
]

TERR = None   # bytearray: 0 sea, 1..NLAND region land, T_ICE ice/neutral, T_GRID graticule
ELEV = None   # bytearray: 0..255 pseudo elevation

# Pre-built 4096-entry truecolor escape table (16 levels per channel)
ESC = ["\x1b[38;2;%d;%d;%dm" % ((k >> 8) * 17, ((k >> 4) & 15) * 17, (k & 15) * 17)
       for k in range(4096)]


def qk(r, g, b):
    """Quantise an RGB triple to a 12-bit key into ESC."""
    r = int(r)
    g = int(g)
    b = int(b)
    if r > 255:
        r = 255
    elif r < 0:
        r = 0
    if g > 255:
        g = 255
    elif g < 0:
        g = 0
    if b > 255:
        b = 255
    elif b < 0:
        b = 0
    return ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)


def fg(rgb):
    return "\x1b[38;2;%d;%d;%dm" % (rgb[0], rgb[1], rgb[2])


def fmt(n):
    if n != n:
        return "NaN"
    if n == float("inf"):
        return "INF"
    if n < 0:
        return "-" + fmt(-n)
    if n < 1000:
        if n < 10:
            return "%.2f" % n
        if n < 100:
            return "%.1f" % n
        return "%.0f" % n
    e = int(math.log10(n) // 3)
    if e >= len(SUFFIX):
        return "%.2e" % n
    return ("%.2f" % (n / (1000.0 ** e))) + SUFFIX[e]


def money(n):
    return "$" + fmt(n)


def seg_line(segs, width, bg=""):
    """Render [(text, rgb), ...] into an exactly `width`-wide coloured string."""
    out = [bg]
    used = 0
    for text, rgb in segs:
        if used >= width:
            break
        if used + len(text) > width:
            text = text[:width - used]
        out.append(fg(rgb) + text)
        used += len(text)
    if used < width:
        out.append(" " * (width - used))
    return "".join(out) + RESET


def seg_len(segs):
    return sum(len(text) for text, _ in segs)


def rule_line(title, width, hint=""):
    """Section heading with an inline rule:  ' TITLE  hint ────────'."""
    segs = [(" " + title + " ", (255, 255, 255))]
    if hint:
        segs.append((" " + hint + " ", (110, 130, 170)))
    segs.append(("─" * max(0, width - seg_len(segs) - 1), RULE_COL))
    return seg_line(segs, width)


def latlon_vec(lat, lon):
    lat = math.radians(lat)
    lon = math.radians(lon)
    return (math.cos(lat) * math.sin(lon), math.sin(lat), math.cos(lat) * math.cos(lon))


def tangent_basis(v):
    """Two unit vectors spanning the sphere's tangent plane at v."""
    hx, hy, hz = v
    up = (0.0, 1.0, 0.0) if abs(hy) < 0.95 else (1.0, 0.0, 0.0)
    e1 = (up[1] * hz - up[2] * hy, up[2] * hx - up[0] * hz, up[0] * hy - up[1] * hx)
    en = math.sqrt(e1[0] ** 2 + e1[1] ** 2 + e1[2] ** 2)
    e1 = (e1[0] / en, e1[1] / en, e1[2] / en)
    e2 = (hy * e1[2] - hz * e1[1], hz * e1[0] - hx * e1[2], hx * e1[1] - hy * e1[0])
    return e1, e2


def lerp(a, b, t):
    return a + (b - a) * t


# --------------------------------------------------------------------------
#  Procedural world texture
# --------------------------------------------------------------------------
def _noise(lat, lon):
    return (0.16 * math.sin(0.31 * lat + 0.17 * lon)
            + 0.12 * math.sin(0.43 * lon - 0.29 * lat + 1.3)
            + 0.07 * math.sin(0.9 * lat + 0.8 * lon + 0.5))


def build_texture():
    """Build the 720x360 terrain / elevation world map (stylised continents)."""
    global TERR, ELEV
    best = [9.0] * (TW * TH)
    owner = bytearray(TW * TH)
    dv = 180.0 / TH
    du = 360.0 / TW
    for rg, lat0, lon0, rlat, rlon in BLOBS:
        v0 = max(0, int((90.0 - (lat0 + rlat * 1.25)) / dv))
        v1 = min(TH - 1, int((90.0 - (lat0 - rlat * 1.25)) / dv))
        span = rlon * 1.25
        u_lo = int((lon0 - span + 180.0) / du)
        u_hi = int((lon0 + span + 180.0) / du)
        if u_hi - u_lo >= TW:
            u_lo, u_hi = 0, TW - 1
        for v in range(v0, v1 + 1):
            lat = 90.0 - (v + 0.5) * dv
            dl = (lat - lat0) / rlat
            dl2 = dl * dl
            if dl2 > 1.6:
                continue
            base = v * TW
            for uu in range(u_lo, u_hi + 1):
                u = uu % TW
                lon = (u + 0.5) * du - 180.0
                dlon = (lon - lon0 + 180.0) % 360.0 - 180.0
                d2 = dl2 + (dlon / rlon) ** 2
                if d2 > 1.6:
                    continue
                sc = d2 - _noise(lat, lon)
                if sc < best[base + u]:
                    best[base + u] = sc
                    owner[base + u] = rg + 1 if rg >= 0 else T_ICE

    terr = bytearray(TW * TH)
    elev = bytearray(TW * TH)
    sin = math.sin
    cos = math.cos
    for v in range(TH):
        lat = 90.0 - (v + 0.5) * dv
        slat = sin(lat * 0.53 + 1.0)
        base = v * TW
        polar = abs(lat) > 68.0
        for u in range(TW):
            i = base + u
            lon = (u + 0.5) * du - 180.0
            b = best[i]
            if b < 1.0:
                depth = 1.0 - b
                if depth > 1.0:
                    depth = 1.0
                e = 0.5 * depth + 0.5 * (0.5 + 0.5 * slat * cos(lon * 0.71))
                elev[i] = int(max(0.0, min(1.0, e)) * 255)
                terr[i] = owner[i]          # 1..NLAND regions, T_ICE neutral
                continue
            if polar:
                if lat < -71.0 + 3.0 * sin(lon * 0.07) + 2.0 * sin(lon * 0.19):
                    terr[i] = T_ICE
                    elev[i] = 110
                    continue
                if lat > 83.0 + 1.5 * sin(lon * 0.11):
                    terr[i] = T_ICE
                    elev[i] = 90
                    continue
            if (v % 30) < 2 or (u % 30) < 2:
                terr[i] = T_GRID            # sea graticule (15 degree grid)
    TERR = terr
    ELEV = elev


# --------------------------------------------------------------------------
#  The 3D globe engine
# --------------------------------------------------------------------------
class Globe:
    LIGHT = (-0.55, 0.50, 0.67)

    def __init__(self, gw, gh, pitch):
        self.gw = gw
        self.gh = gh
        self.pitch = pitch
        self.cx = (gw - 1) / 2.0
        self.cy = (gh - 1) / 2.0
        # The tilted orbit ring reaches ~0.6 R vertically but ~1.2 R horizontally,
        # so only the width needs a ring margin; height just needs room for the halo.
        self.ry = min((gh - 1) / 2.0 / 1.06, gw / 4.0 / 1.22)
        self.rx = self.ry * 2.0            # terminal cells are ~2x taller than wide
        self.lit_count = 0
        self.arc_cache = {}
        self._build_cells()
        self.set_pitch(pitch)
        self._build_nodes()
        self._build_ring()
        self._build_background()

    # ---- static geometry -------------------------------------------------
    def _build_cells(self):
        gw, gh, cx, cy, rx, ry = self.gw, self.gh, self.cx, self.cy, self.rx, self.ry
        lx, ly, lz = self.LIGHT
        n = math.sqrt(lx * lx + ly * ly + lz * lz)
        lx, ly, lz = lx / n, ly / n, lz / n
        self.light = (lx, ly, lz)
        hx, hy, hz = lx, ly, lz + 1.0
        hn = math.sqrt(hx * hx + hy * hy + hz * hz)
        hx, hy, hz = hx / hn, hy / hn, hz / hn
        self.c_idx = []
        self.c_vec = []
        self.c_sh = []
        self.c_limb = []
        self.c_spec = []
        for row in range(gh):
            y = (cy - row) / ry
            for col in range(gw):
                x = (col - cx) / rx
                r2 = x * x + y * y
                if r2 >= 1.0:
                    continue
                z = math.sqrt(1.0 - r2)
                lum = x * lx + y * ly + z * lz
                day = (lum + 0.12) / 1.12
                day = 0.0 if day < 0 else (1.0 if day > 1 else day)
                day = day * day * (3.0 - 2.0 * day)          # smooth terminator
                sh = (0.08 + 0.92 * day) * (0.62 + 0.38 * z)  # limb darkening
                limb = (1.0 - z) ** 2.2
                sp = x * hx + y * hy + z * hz
                sp = (sp ** 48) * day if sp > 0 else 0.0
                self.c_idx.append(row * gw + col)
                self.c_vec.append((x, y, z))
                self.c_sh.append(sh)
                self.c_limb.append(limb)
                self.c_spec.append(sp)
        self.ncell = len(self.c_idx)

    def set_pitch(self, pitch):
        """Cache pitch-inverse-rotated cell vectors (rotation matrix about X)."""
        self.pitch = pitch
        cp = math.cos(pitch)
        sp = math.sin(pitch)
        self.cp, self.sp = cp, sp
        xs, ys, zs = [], [], []
        for x, y, z in self.c_vec:
            y1 = y * cp + z * sp
            z1 = -y * sp + z * cp
            xs.append(x)
            ys.append(max(-0.9999999, min(0.9999999, y1)))
            zs.append(z1)
        self.c_xp, self.c_yp, self.c_zp = xs, ys, zs

    def _build_nodes(self):
        """Fibonacci sphere lattice; keep the points that fall on developable land."""
        n = int(max(3000, min(16000, self.rx * self.ry * 5.5)))
        rng = random.Random(7)
        golden = PI * (3.0 - math.sqrt(5.0))
        self.hub_vecs = []
        for i in LAND_HUBS:
            lat = math.radians(VENTURES[i]["lat"])
            lon = math.radians(VENTURES[i]["lon"])
            self.hub_vecs.append((math.cos(lat) * math.sin(lon), math.sin(lat),
                                  math.cos(lat) * math.cos(lon)))
        self.hub_basis = [tangent_basis(v) for v in self.hub_vecs]
        self.reg_vecs = [latlon_vec(REGIONS[r]["lat"], REGIONS[r]["lon"]) for r in range(NLAND)]
        self.reg_basis = [tangent_basis(v) for v in self.reg_vecs]
        cos_lim = math.cos(0.5)
        NX, NY, NZ, NREG, NHUB, NDIST, NH1, NH2 = [], [], [], [], [], [], [], []
        for i in range(n):
            y = 1.0 - 2.0 * (i + 0.5) / n
            rr = math.sqrt(max(0.0, 1.0 - y * y))
            th = i * golden
            x = rr * math.cos(th)
            z = rr * math.sin(th)
            lon = math.atan2(x, z)
            lat = math.asin(y)
            u = int((lon + PI) * USC)
            if u >= TW:
                u = 0
            v = int((HALF_PI - lat) * VSC)
            if v >= TH:
                v = TH - 1
            tc = TERR[v * TW + u]
            if tc < 1 or tc > NLAND:
                continue
            best, bd = DUMMY_HUB, cos_lim
            for hi, (hx, hy, hz) in enumerate(self.hub_vecs):
                dd = x * hx + y * hy + z * hz
                if dd > bd:
                    bd, best = dd, hi
            dist = math.acos(min(1.0, bd)) if best != DUMMY_HUB else 9.0
            NX.append(x)
            NY.append(y)
            NZ.append(z)
            NREG.append(tc - 1)
            NHUB.append(best)
            NDIST.append(dist)
            NH1.append(rng.random())
            NH2.append(rng.random())
        self.nn = len(NX)
        self.NX, self.NY, self.NZ = NX, NY, NZ
        self.NREG, self.NHUB, self.NDIST, self.NH1, self.NH2 = NREG, NHUB, NDIST, NH1, NH2

    def ring_pos(self, a):
        px = RING_R * math.cos(a)
        pz = RING_R * math.sin(a)
        y = -pz * math.sin(RING_TILT)
        z = pz * math.cos(RING_TILT)
        cr, sr = math.cos(RING_ROLL), math.sin(RING_ROLL)
        return px * cr - y * sr, px * sr + y * cr, z

    def _build_ring(self):
        gw, gh, cx, cy, rx, ry = self.gw, self.gh, self.cx, self.cy, self.rx, self.ry
        self.ring_m = 520
        self.ring_front, self.ring_back = [], []
        seen = set()
        for j in range(self.ring_m):
            x, y, z = self.ring_pos(j * TAU / self.ring_m)
            col = int(cx + x * rx + 0.5)
            row = int(cy - y * ry + 0.5)
            if not (0 <= col < gw and 0 <= row < gh):
                continue
            idx = row * gw + col
            if idx in seen:
                continue
            seen.add(idx)
            if z >= 0:
                self.ring_front.append((j, idx, z))
            elif x * x + y * y >= 0.99:          # not hidden behind the planet
                self.ring_back.append((j, idx, z))

    def _build_background(self):
        gw, gh, cx, cy, rx, ry = self.gw, self.gh, self.cx, self.cy, self.rx, self.ry
        lx, ly, _ = self.light
        self.bg_chars = [" "] * (gw * gh)
        self.bg_keys = [0] * (gw * gh)
        # atmospheric halo just outside the limb
        for row in range(gh):
            y = (cy - row) / ry
            for col in range(gw):
                x = (col - cx) / rx
                r = math.sqrt(x * x + y * y)
                if 1.0 <= r < 1.07:
                    ld = (x * lx + y * ly) / r
                    inten = (1.0 - (r - 1.0) / 0.07) * max(0.0, 0.25 + 0.75 * ld)
                    if inten > 0.12:
                        i = row * gw + col
                        self.bg_chars[i] = ":" if inten > 0.5 else "."
                        self.bg_keys[i] = qk(60 + 60 * inten, 120 + 90 * inten, 255 * inten + 40)
        # star field
        rng = random.Random(99)
        self.stars = []
        for _ in range(gw * gh // 55):
            col = rng.randrange(gw)
            row = rng.randrange(gh)
            x = (col - cx) / rx
            y = (cy - row) / ry
            if x * x + y * y < 1.2:
                continue
            idx = row * gw + col
            if self.bg_chars[idx] != " ":
                continue
            self.stars.append((idx, rng.random() * TAU, 0.35 + 0.65 * rng.random(),
                               rng.choice(".....:+*")))

    # ---- helpers ---------------------------------------------------------
    def arc(self, a, b):
        key = (a, b)
        pts = self.arc_cache.get(key)
        if pts is None:
            va, vb = self.hub_vecs[a], self.hub_vecs[b]
            dot = max(-1.0, min(1.0, va[0] * vb[0] + va[1] * vb[1] + va[2] * vb[2]))
            om = math.acos(dot)
            so = math.sin(om) or 1e-6
            n = 44
            pts = []
            for k in range(n):
                t = k / (n - 1.0)
                w1 = math.sin((1 - t) * om) / so
                w2 = math.sin(t * om) / so
                lift = 1.0 + 0.16 * math.sin(PI * t) * min(1.0, om)
                pts.append(((va[0] * w1 + vb[0] * w2) * lift,
                            (va[1] * w1 + vb[1] * w2) * lift,
                            (va[2] * w1 + vb[2] * w2) * lift))
            self.arc_cache[key] = pts
        return pts

    # ---- purchase / reward effects --------------------------------------
    def _draw_fx(self, gs, t, put, put_text):
        cx, cy, rx, ry = self.cx, self.cy, self.rx, self.ry
        c = math.cos(gs.yaw)
        s = math.sin(gs.yaw)
        cp, sp = self.cp, self.sp
        sin, cos = math.sin, math.cos
        GOLD = (255, 215, 90)
        RED = (255, 80, 60)

        def proj(wx, wy, wz):
            z1 = -wx * s + wz * c
            return (wy * sp + z1 * cp,
                    int(cx + (wx * c + wz * s) * rx + 0.5),
                    int(cy - (wy * cp - z1 * sp) * ry + 0.5))

        def tint(base, fade):
            w = fade * 0.6
            f = 0.35 + 0.65 * fade
            return qk((base[0] * (1 - w) + 255 * w) * f,
                      (base[1] * (1 - w) + 255 * w) * f,
                      (base[2] * (1 - w) + 255 * w) * f)

        for fx in gs.fx:
            age = t - fx["t0"]
            dur = fx["dur"]
            if age < 0 or age >= dur:
                continue
            p = age / dur
            vi = fx["vi"]
            text = fx["text"]
            seed = fx["seed"]
            bad = fx["kind"] in ("disaster", "alert")
            gold = fx["kind"] in ("upgrade", "cash", "boom")
            tc = RED if bad else GOLD                       # floating text colour
            if vi is None:                                  # floating reward / alert text
                rise = int(min(age, 1.5) * 3)
                k = 1.0 - p * 0.6
                put_text(int(cx) - len(text) // 2, max(1, int(cy - ry * 1.12)) + 3 - rise,
                         text, qk(tc[0] * k, tc[1] * k, tc[2] * k))
                continue
            rc = REG_COLS[VENTURES[vi]["region"]]
            base = RED if bad else (GOLD if gold else rc)
            if vi in HUB_OF:                                # ---- land hub
                hb = HUB_OF[vi]
                hx, hy, hz = self.hub_vecs[hb]
                e1, e2 = self.hub_basis[hb]
                hz_, hcol, hrow = proj(hx, hy, hz)
                for rn in range(3 if (gold or bad) else 2):          # expanding shockwaves
                    pr = (age - rn * 0.28) / (dur * 0.8)
                    if pr <= 0 or pr >= 1:
                        continue
                    rho = 0.62 * (1 - (1 - pr) ** 2)
                    n = 28 + int(rho * 90)
                    sr, cr = sin(rho), cos(rho)
                    fade = 1.0 - pr
                    ch = "*" if fade > 0.6 else ("o" if fade > 0.3 else ".")
                    k = tint(base, fade)
                    for q in range(n):
                        ph = q * TAU / n
                        cph, sph = cos(ph), sin(ph)
                        z, col, row = proj(hx * cr + (e1[0] * cph + e2[0] * sph) * sr,
                                           hy * cr + (e1[1] * cph + e2[1] * sph) * sr,
                                           hz * cr + (e1[2] * cph + e2[2] * sph) * sr)
                        if z > 0.02:
                            put(col, row, ch, k)
                for q in range(16):                         # spark fountain
                    a = q * 2.399963 + seed
                    spd = 0.6 + 0.8 * ((q * 0.618 + seed) % 1.0)
                    rho = 0.30 * age * spd
                    lift = max(0.0, 0.9 * age * spd - 0.55 * age * age * spd) * 0.25 + 1.0
                    ca, sa = cos(a), sin(a)
                    sr, cr = sin(rho), cos(rho)
                    z, col, row = proj((hx * cr + (e1[0] * ca + e2[0] * sa) * sr) * lift,
                                       (hy * cr + (e1[1] * ca + e2[1] * sa) * sr) * lift,
                                       (hz * cr + (e1[2] * ca + e2[2] * sa) * sr) * lift)
                    if z > 0.02:
                        put(col, row, "*" if p < 0.3 else ("+" if p < 0.6 else "."),
                            tint(tc if q % 2 else base, 1.0 - p))
                if hz_ > 0.05:
                    if age < 0.7:                           # impact flash
                        wh = qk(255, 255, 230)
                        put(hcol - 2, hrow, "-", wh)
                        put(hcol + 2, hrow, "-", wh)
                        put(hcol - 1, hrow, "*", wh)
                        put(hcol + 1, hrow, "*", wh)
                        put(hcol, hrow - 1, "|", wh)
                        put(hcol, hrow + 1, "|", wh)
                    k = 1.0 - p * 0.7
                    put_text(hcol - len(text) // 2, hrow - 2 - int(min(age, 1.2) * 4), text,
                             qk(tc[0] * k, tc[1] * k, tc[2] * k))
            else:                                           # ---- satellite
                vd = VENTURES[vi]
                x, y, z = self.ring_pos(vd["orbit"] + t * vd["speed"])
                if z < 0 and x * x + y * y < 0.99:
                    continue
                col = int(cx + x * rx + 0.5)
                row = int(cy - y * ry + 0.5)
                for rn in range(3 if (gold or bad) else 2):
                    pr = (age - rn * 0.28) / (dur * 0.8)
                    if pr <= 0 or pr >= 1:
                        continue
                    rr = 14.0 * (1 - (1 - pr) ** 2)
                    fade = 1.0 - pr
                    ch = "*" if fade > 0.6 else ("o" if fade > 0.3 else ".")
                    k = tint(base, fade)
                    for q in range(40):
                        ph = q * TAU / 40
                        put(col + int(cos(ph) * rr * 2), row + int(sin(ph) * rr), ch, k)
                for q in range(14):
                    a = q * TAU / 14 + seed
                    dist = 2.0 + 9.0 * age * (0.6 + 0.8 * ((q * 0.618 + seed) % 1.0))
                    put(col + int(cos(a) * dist * 2), row + int(sin(a) * dist),
                        "*" if p < 0.3 else ("+" if p < 0.6 else "."),
                        tint(tc if q % 2 else base, 1.0 - p))
                if age < 0.7:
                    put(col, row, "@", qk(255, 255, 230))
                k = 1.0 - p * 0.7
                put_text(col - len(text) // 2, row - 2 - int(min(age, 1.2) * 3), text,
                         qk(tc[0] * k, tc[1] * k, tc[2] * k))

    # ---- per-frame render ------------------------------------------------
    def render(self, gs, t):
        gw, gh = self.gw, self.gh
        chars = self.bg_chars[:]
        keys = self.bg_keys[:]
        cx, cy, rx, ry = self.cx, self.cy, self.rx, self.ry
        c = math.cos(gs.yaw)
        s = math.sin(gs.yaw)
        cp, sp = self.cp, self.sp
        sin = math.sin
        DEV = gs.dev
        RC = REG_COLS

        def put(col, row, ch, k):
            if 0 <= col < gw and 0 <= row < gh:
                i = row * gw + col
                chars[i] = ch
                keys[i] = k

        def put_text(col, row, text, k):
            for n, ch in enumerate(text):
                put(col + n, row, ch, k)

        # --- stars (twinkling) ---
        for idx, ph, b, ch in self.stars:
            f = b * (0.55 + 0.45 * sin(t * 1.7 + ph))
            chars[idx] = ch
            keys[idx] = qk(190 * f, 205 * f, 255 * f)

        # --- orbit ring: far side ---
        spacedev = DEV[SPACE]
        lit_frac = spacedev * 45.0
        pulse = (t * 0.35) % 1.0
        M = self.ring_m

        def draw_ring(lst):
            for j, idx, z in lst:
                zf = 0.5 + 0.5 * z / RING_R
                d = (j / M - pulse) % 1.0
                if d < 0.045:
                    f = 1.0 - d / 0.045
                    chars[idx] = "@" if f > 0.7 else "*"
                    keys[idx] = qk(150 + 105 * f, 110 + 145 * f, 230 + 25 * f)
                elif (j * 37) % 100 < lit_frac:
                    chars[idx] = "+"
                    keys[idx] = qk(40 + 190 * zf, 30 + 150 * zf, 255)
                else:
                    chars[idx] = "."
                    keys[idx] = qk(80 + 60 * zf, 60 + 50 * zf, 140 + 80 * zf)

        draw_ring(self.ring_back)

        # --- TERRAIN: inverse-mapped sphere sampling of the world texture ---
        TERRL, ELEVL = TERR, ELEV
        IDX, XP, YP, ZP = self.c_idx, self.c_xp, self.c_yp, self.c_zp
        SH, LIMB, SPEC = self.c_sh, self.c_limb, self.c_spec
        atan2, asin = math.atan2, math.asin
        tick = int(t * 5)
        CRISIS = [0.0] * NLAND
        for r in gs.crisis_regions:
            if r < NLAND:
                CRISIS[r] = 0.75 + 0.25 * sin(t * 6.0)
        for k in range(self.ncell):
            xp = XP[k]
            zp = ZP[k]
            x0 = xp * c - zp * s                      # undo Yaw
            z0 = xp * s + zp * c
            u = int((atan2(x0, z0) + PI) * USC)       # longitude -> texel
            if u >= TW:
                u = 0
            v = int((HALF_PI - asin(YP[k])) * VSC)    # latitude  -> texel
            tex = v * TW + u
            tc = TERRL[tex]
            sh = SH[k]
            if tc == 0 or tc == T_GRID:
                spc = SPEC[k]
                if tc == 0:
                    br = 0.15 + 0.98 * sh
                    r_ = 16 * br + 255 * spc
                    g_ = 60 * br + 255 * spc
                    b_ = 150 * br + 255 * spc
                    if spc > 0.6:
                        ch = "*"
                    elif spc > 0.28:
                        ch = "+"
                    elif sh > 0.62:
                        ch = ":"
                    elif sh > 0.1:
                        ch = "."
                    else:
                        ch = " "
                    if ((u + 3 * v + tick) & 15) == 0 and sh > 0.3 and spc < 0.28:
                        ch = "~"
                else:
                    br = 0.25 + 0.95 * sh
                    r_ = 50 * br + 255 * spc
                    g_ = 135 * br + 255 * spc
                    b_ = 235 * br + 255 * spc
                    ch = "+" if sh > 0.3 else ":"
            elif tc <= NLAND:
                ri = tc - 1
                d = DEV[ri]
                e = ELEVL[tex] * 0.0039216
                val = sh * 0.45 + e * 0.20 + d * 0.35
                ci = int(val * 10.0)
                if ci < 1:
                    ci = 1
                elif ci > 10:
                    ci = 10
                ch = RAMP[ci]
                br = 0.13 + 0.87 * sh
                if CRISIS[ri]:                      # disaster: the zone flickers red
                    br *= 0.75
                    r_ = 200 * br * CRISIS[ri]
                    g_ = 40 * br
                    b_ = 30 * br
                    i = IDX[k]
                    chars[i] = ch
                    keys[i] = qk(r_ + 60 * br, g_ + 60 * br, b_ + 40 * br)
                    continue
                rc = RC[ri]
                # land always wears its zone colour; development makes it brighter
                lvl = br * (0.50 + 0.30 * e + 0.35 * d)
                glow = d * 0.28 * (1.0 - sh)
                r_ = rc[0] * (lvl + glow)
                g_ = rc[1] * (lvl + glow)
                b_ = rc[2] * (lvl + glow)
            else:                                     # polar ice / Greenland
                e = ELEVL[tex] * 0.0039216
                ci = int((sh * 0.5 + e * 0.2) * 10.0)
                if ci < 1:
                    ci = 1
                elif ci > 9:
                    ci = 9
                ch = RAMP[ci]
                br = 0.15 + 0.9 * sh
                r_, g_, b_ = 205 * br, 225 * br, 240 * br
            lm = LIMB[k]                              # atmospheric limb glow
            if lm > 0.03:
                a = lm * 0.7
                w = (0.3 + 0.7 * sh) * a
                r_ = r_ * (1 - a) + 90 * w
                g_ = g_ * (1 - a) + 160 * w
                b_ = b_ * (1 - a) + 255 * w
            i = IDX[k]
            chars[i] = ch
            keys[i] = qk(r_, g_, b_)

        # --- Fibonacci lattice NODES (corporate presence) ---
        NX, NY, NZ = self.NX, self.NY, self.NZ
        NREG, NHUB, NDIST, NH1, NH2 = self.NREG, self.NHUB, self.NDIST, self.NH1, self.NH2
        ZR, ZD, RD, FXH = gs.zr, gs.zd, gs.rd, gs.fxh
        lit_count = 0
        for i in range(self.nn):
            hb = NHUB[i]
            zr = ZR[hb]
            if zr > 0.0 and NDIST[i] < zr and NH2[i] < ZD[hb]:
                lit = 2
            elif NH1[i] < RD[NREG[i]]:
                lit = 1
            else:
                continue
            wx = NX[i]
            wz = NZ[i]
            z1 = -wx * s + wz * c
            z = NY[i] * sp + z1 * cp
            if z < 0.03:
                continue
            x = wx * c + wz * s
            y = NY[i] * cp - z1 * sp
            col = int(cx + x * rx + 0.5)
            row = int(cy - y * ry + 0.5)
            tw = 0.5 + 0.5 * sin(t * 2.7 + NH1[i] * 40.0)
            if lit == 2:
                tier = 1.0 - NDIST[i] / zr
                fh = FXH[hb]                         # purchase flare-up
                gi = int(2 + tier * 3.6 + tw * 1.8 + fh * 2.5)
                hot = tier * tier * 0.8 * tw + fh * 0.9
                rc = RC[VENTURES[LAND_HUBS[hb]]["region"]]
            else:
                gi = 1 + int(tw * 2.2)
                hot = 0.0
                rc = RC[NREG[i]]
            if gi > 7:
                gi = 7
            f = (0.5 + 0.5 * z) * (0.65 + 0.35 * tw)
            idx = row * gw + col
            if 0 <= idx < gw * gh:
                chars[idx] = NODE_RAMP[gi]
                keys[idx] = qk(rc[0] * f + 255 * hot, rc[1] * f + 255 * hot,
                               rc[2] * f + 255 * hot)
                lit_count += 1
        self.lit_count = lit_count

        # --- trade-route arcs between owned land hubs ---
        owned = [h for h, vi in enumerate(LAND_HUBS) if gs.level[vi] > 0]
        pairs = [(owned[n], owned[n + 1]) for n in range(len(owned) - 1)]
        if len(owned) >= 3:
            pairs.append((owned[0], owned[-1]))
        for a, b in pairs:
            pts = self.arc(a, b)
            npt = len(pts)
            pp = (t * 0.30 + (a * 3 + b) * 0.17) % 1.0
            for k, (wx, wy, wz) in enumerate(pts):
                z1 = -wx * s + wz * c
                z = wy * sp + z1 * cp
                if z < 0.02:
                    continue
                dd = abs(k / (npt - 1.0) - pp)
                if dd < 0.03:
                    x = wx * c + wz * s
                    y = wy * cp - z1 * sp
                    put(int(cx + x * rx + 0.5), int(cy - y * ry + 0.5), "$",
                        qk(255, 255, 190))
                elif k % 2 == 0:
                    x = wx * c + wz * s
                    y = wy * cp - z1 * sp
                    f = 0.5 + 0.5 * z
                    put(int(cx + x * rx + 0.5), int(cy - y * ry + 0.5), ":",
                        qk(90 * f, 235 * f, 205 * f))

        # --- active world events: storms spin over hit zones, gold swirls on booms ---
        for m in gs.mods:
            if m["scope"] != "region" or m["target"] >= NLAND:
                continue
            hx, hy, hz = self.reg_vecs[m["target"]]
            e1, e2 = self.reg_basis[m["target"]]
            bad = m["bad"]
            spin = t * (2.6 if bad else 1.2)
            for q in range(56):
                ang = q * 0.52 + spin
                rho = 0.03 + q * 0.0042
                ca, sa = math.cos(ang), sin(ang)
                sr, cr = sin(rho), math.cos(rho)
                wx = hx * cr + (e1[0] * ca + e2[0] * sa) * sr
                wy = hy * cr + (e1[1] * ca + e2[1] * sa) * sr
                wz = hz * cr + (e1[2] * ca + e2[2] * sa) * sr
                z1 = -wx * s + wz * c
                z = wy * sp + z1 * cp
                if z < 0.03:
                    continue
                f = (1.0 - q / 70.0) * (0.55 + 0.45 * z)
                if bad:
                    ch = "@" if q < 8 else ("%" if q < 20 else ("*" if q < 36 else "~"))
                    k = qk(150 + 105 * f, 150 * f, 140 * f)
                else:
                    ch = "$" if q % 3 == 0 else "+"
                    k = qk(255 * f, 215 * f, 90 * f)
                put(int(cx + (wx * c + wz * s) * rx + 0.5),
                    int(cy - (wy * cp - z1 * sp) * ry + 0.5), ch, k)

        # --- zone names printed on the surface ---
        for r in range(NLAND):
            wx, wy, wz = self.reg_vecs[r]
            z1 = -wx * s + wz * c
            z = wy * sp + z1 * cp
            if z < 0.35:
                continue
            x = wx * c + wz * s
            y = wy * cp - z1 * sp
            name = REGIONS[r]["name"].upper()
            rc = (255, 90, 70) if r in gs.crisis_regions else RC[r]
            f = (z - 0.35) / 0.65
            f = 0.35 + 0.65 * f * f
            put_text(int(cx + x * rx + 0.5) - len(name) // 2, int(cy - y * ry + 0.5), name,
                     qk(rc[0] * f, rc[1] * f, rc[2] * f))

        # --- orbit ring: near side ---
        draw_ring(self.ring_front)

        # --- hub markers (land) ---
        glyphs = "@$%&"
        blink = int(t * 3) % 2 == 0
        red = qk(255, 70, 60)
        for hb, vi in enumerate(LAND_HUBS):
            wx, wy, wz = self.hub_vecs[hb]
            z1 = -wx * s + wz * c
            z = wy * sp + z1 * cp
            if z < 0.06:
                continue
            x = wx * c + wz * s
            y = wy * cp - z1 * sp
            col = int(cx + x * rx + 0.5)
            row = int(cy - y * ry + 0.5)
            lv = gs.level[vi]
            rc = RC[VENTURES[vi]["region"]]
            label = str(vi + 1)
            if lv <= 0:
                put(col, row, "o", qk(rc[0] * 0.7, rc[1] * 0.7, rc[2] * 0.7))
                put_text(col + 1, row, label, qk(170, 180, 200))
                continue
            pulse_c = 0.65 + 0.35 * sin(t * 4.0 + vi)
            core = qk(255, 245 * pulse_c, 160 * pulse_c)
            if vi in gs.halted and blink:                  # strike / freeze: blinking X
                put(col, row, "X", red)
            else:
                put(col, row, glyphs[int(t * 5 + vi) % 4], core)
            off = 1
            if lv >= 10:
                put(col - 1, row, "(", qk(*rc))
                put(col + 1, row, ")", qk(*rc))
                off = 2
            if lv >= 25:
                put(col - 2, row, "<", qk(*rc))
                put(col + 2, row, ">", qk(*rc))
                off = 3
            put_text(col + off, row, label, red if vi in gs.halted else qk(255, 255, 255))

        # --- satellites on the orbit ring (Space hubs) ---
        space_hit = SPACE in gs.crisis_regions
        for vi in range(NV):
            vd = VENTURES[vi]
            if "orbit" not in vd:
                continue
            a = vd["orbit"] + t * vd["speed"]
            x, y, z = self.ring_pos(a)
            if z < 0 and x * x + y * y < 0.99:
                continue                                  # behind the planet
            col = int(cx + x * rx + 0.5)
            row = int(cy - y * ry + 0.5)
            lv = gs.level[vi]
            rc = RC[SPACE]
            if lv <= 0:
                put(col, row, "o", qk(rc[0] * 0.7, rc[1] * 0.7, rc[2] * 0.7))
            else:
                for tr in (1, 2, 3):                      # comet tail
                    tx, ty, _ = self.ring_pos(a - math.copysign(tr * 0.025, vd["speed"]))
                    put(int(cx + tx * rx + 0.5), int(cy - ty * ry + 0.5), ".",
                        qk(rc[0] / (tr + 1), rc[1] / (tr + 1), rc[2] / (tr + 1)))
                hit = (space_hit or vi in gs.halted) and blink
                put(col, row, "X" if hit else glyphs[int(t * 5 + vi) % 4],
                    red if hit else qk(255, 240, 255))
                if lv >= 10:
                    put(col - 1, row, "=", qk(*rc))
                    put(col + 1, row, "=", qk(*rc))
            put_text(col, row - 1, str(vi + 1), qk(255, 255, 255))

        # --- purchase / reward effects ---
        if gs.fx:
            self._draw_fx(gs, t, put, put_text)

        # --- HUD text overlay ---
        cyan = qk(90, 210, 255)
        dim = qk(110, 130, 170)
        put_text(1, 0, "ORBITAL VIEW // LIVE", cyan)
        put_text(1, 1, "YAW %5.1f  TILT %4.1f  SPIN %+.1f/s" % (
            math.degrees(gs.yaw) % 360.0, math.degrees(gs.pitch), gs.spin_deg), dim)
        put_text(1, 2, "NODES %d/%d" % (lit_count, self.nn), dim)
        put_text(1, gh - 1, "o idle  @ hub  (@) L10  <(@)> L25  =@= orbital  $ cargo", dim)
        fps_txt = "%d FPS" % round(gs.fps_real)
        put_text(gw - len(fps_txt) - 1, 0, fps_txt, cyan)

        # --- convert buffers to strings (colour escape only when it changes) ---
        rows_out = []
        for r in range(gh):
            base = r * gw
            parts = []
            last = -1
            for i in range(base, base + gw):
                ch = chars[i]
                if ch == " ":
                    parts.append(" ")
                else:
                    kk = keys[i]
                    if kk != last:
                        parts.append(ESC[kk])
                        last = kk
                    parts.append(ch)
            parts.append(RESET)
            rows_out.append("".join(parts))
        return rows_out


# --------------------------------------------------------------------------
#  Terminal I/O (cross-platform, non-blocking)
# --------------------------------------------------------------------------
class Term:
    def __enter__(self):
        if IS_WIN:
            try:
                import ctypes
                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-11)
                mode = ctypes.c_ulong()
                k32.GetConsoleMode(h, ctypes.byref(mode))
                k32.SetConsoleMode(h, mode.value | 0x0004)
            except Exception:
                pass
            os.system("")
        else:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J\x1b[H")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write(RESET + "\x1b[?7h\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if not IS_WIN:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        return False

    @staticmethod
    def size():
        sz = shutil.get_terminal_size((120, 40))
        return sz.columns, sz.lines

    def read(self):
        """Return all pending characters without blocking."""
        out = []
        if IS_WIN:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    # special key: translate arrows into ANSI sequences like POSIX
                    out.append(WIN_KEYS.get(msvcrt.getwch(), ""))
                    continue
                out.append(ch)
            return "".join(out)
        r, _, _ = select.select([self.fd], [], [], 0)
        if r:
            try:
                return os.read(self.fd, 4096).decode("utf-8", "ignore")
            except OSError:
                return ""
        return ""

    def wait(self, timeout):
        """Sleep up to `timeout` seconds; wake early if a key is pressed."""
        if timeout <= 0:
            return
        if IS_WIN:
            end = time.perf_counter() + timeout
            while time.perf_counter() < end:
                if msvcrt.kbhit():
                    return
                time.sleep(0.004)
            return
        try:
            select.select([self.fd], [], [], timeout)
        except (OSError, ValueError):
            time.sleep(timeout)

    @staticmethod
    def write(s):
        sys.stdout.write(s)
        sys.stdout.flush()


# --------------------------------------------------------------------------
#  Game state, economy & UI
# --------------------------------------------------------------------------
class Game:
    def __init__(self):
        self.cash = 25.0
        self.earned = 25.0
        self.level = [0] * NV
        self.tier = [0] * NV
        self.playtime = 0.0
        self.yaw = 0.6
        self.pitch = math.radians(22.0)
        self.spin_deg = 14.0
        self.fps = 20
        self.fps_real = 20.0
        self.running = True
        self.buf = ""
        self.hist = []
        self.hpos = 0
        self.draft = ""              # unsent input saved while browsing history
        self.esc = 0
        self.log = deque(maxlen=LOG_MAX)
        self.overlay = None
        self.globe = None
        self.last_size = None
        self.next_event = 60.0 + random.random() * 60.0
        self.next_save = 30.0
        self.t = 0.0                 # animation clock (seconds since start)
        self.fx = []                 # active purchase / reward effects
        self.fxh = [0.0] * (DUMMY_HUB + 1)
        self.focus = True
        self.focus_lon = None
        self.focus_until = 0.0
        self.mini = None             # active work minigame
        self.work_ready = 0.0
        self.streak = 0
        self.boost_until = 0.0
        self.boost_mult = 1.0
        self.rps_cache = 0.0
        self.dev = [0.0] * NREG
        self.zr = [0.0] * (DUMMY_HUB + 1)
        self.zd = [0.0] * (DUMMY_HUB + 1)
        self.rd = [0.0] * NLAND
        self.mods = []               # timed world events: booms, disasters, strikes ...
        self.crisis_regions = set()  # regions under a bad event (globe turns red there)
        self.halted = set()          # ventures stopped by a strike / freeze
        self.n_unlocked = 0
        self.seen_unlocked = set()
        self.update_visual()
        self.check_unlocks()
        self.say("Welcome, CEO. Type 'help' for orders.  First move:  buy 1", (130, 230, 255))

    # ---- logging ----------------------------------------------------------
    def say(self, text, rgb=(200, 210, 228)):
        m, s = divmod(int(self.playtime), 60)
        self.log.append(("%02d:%02d " % (m, s), text, rgb))

    # ---- economy ----------------------------------------------------------
    def unit_cost(self, i, k=1):
        g = COST_GROWTH
        return VENTURES[i]["cost"] * (g ** self.level[i]) * (g ** k - 1.0) / (g - 1.0)

    def max_affordable(self, i):
        c0 = VENTURES[i]["cost"] * (COST_GROWTH ** self.level[i])
        if self.cash < c0:
            return 0
        k = int(math.log(self.cash * (COST_GROWTH - 1.0) / c0 + 1.0) / math.log(COST_GROWTH))
        while k > 0 and self.unit_cost(i, k) > self.cash:
            k -= 1
        return k

    def upgrade_cost(self, i):
        return VENTURES[i]["cost"] * UPGRADE_BASE * (UPGRADE_GROWTH ** self.tier[i])

    def upgrade_need(self, i):
        return 5 * (self.tier[i] + 1)

    def base_income(self, i):
        lv = self.level[i]
        if lv <= 0:
            return 0.0
        return (VENTURES[i]["income"] * lv * (2.0 ** self.tier[i]) * (2.0 ** (lv // 25)))

    def region_mult(self, r):
        members = [i for i in range(NV) if VENTURES[i]["region"] == r]
        return 1.5 if all(self.level[i] > 0 for i in members) else 1.0

    def global_mult(self):
        return 2.0 if all(l > 0 for l in self.level) else 1.0

    # ---- progression: ventures unlock one by one ---------------------------
    def every_land_zone(self):
        return all(any(self.level[i] > 0 for i in range(NV) if VENTURES[i]["region"] == r)
                   for r in range(NLAND))

    def unlock_need(self, i):
        return VENTURES[i]["cost"] * UNLOCK_EARNED

    def is_unlocked(self, i):
        if self.level[i] > 0:
            return True
        if VENTURES[i]["region"] == SPACE and not self.every_land_zone():
            return False                     # space needs a foothold on every continent
        return self.earned >= self.unlock_need(i)

    def lock_reason(self, i):
        if VENTURES[i]["region"] == SPACE and not self.every_land_zone():
            return "own a venture in every land zone"
        return "earn %s in total" % money(self.unlock_need(i))

    def check_unlocks(self):
        n = sum(1 for i in range(NV) if self.is_unlocked(i))
        if n > self.n_unlocked:
            for i in range(NV):
                if self.is_unlocked(i) and i not in self.seen_unlocked:
                    self.seen_unlocked.add(i)
                    if self.n_unlocked:          # stay quiet about the starting set
                        self.say("NEW VENTURE AVAILABLE: [%d] %s (%s)" %
                                 (i + 1, VENTURES[i]["name"],
                                  REGIONS[VENTURES[i]["region"]]["name"]), (130, 230, 255))
            self.n_unlocked = n

    def event_mult(self, i, ignore_bad=False):
        """Combined multiplier of all active world events touching venture i."""
        mult = 1.0
        reg = VENTURES[i]["region"]
        for m in self.mods:
            if ignore_bad and m["bad"]:
                continue
            if (m["scope"] == "global" or (m["scope"] == "region" and m["target"] == reg)
                    or (m["scope"] == "venture" and m["target"] == i)):
                mult *= m["mult"]
        return mult

    def venture_income(self, i, ignore_bad=False):
        return (self.base_income(i) * self.region_mult(VENTURES[i]["region"])
                * self.global_mult() * self.event_mult(i, ignore_bad))

    def rps(self, ignore_bad=False):
        total = sum(self.venture_income(i, ignore_bad) for i in range(NV))
        if self.playtime < self.boost_until:
            total *= self.boost_mult
        return total

    def mults(self):
        return [self.region_mult(r) for r in range(NREG)] + [self.global_mult()]

    def check_bonuses(self, before):
        after = self.mults()
        for r in range(NREG):
            if after[r] > before[r]:
                self.say("SYNERGY UNLOCKED: all %s ventures online -> x1.5 income there!"
                         % REGIONS[r]["name"], (255, 220, 110))
        if after[NREG] > before[NREG]:
            self.say("GLOBAL DOMINANCE: all %d ventures owned -> x2 income everywhere!" % NV,
                     (255, 120, 210))

    def update_visual(self):
        """Derive globe development values from the current empire."""
        rl = [0] * NREG
        for i in range(NV):
            rl[VENTURES[i]["region"]] += self.level[i]
        self.dev = [1.0 - math.exp(-rl[r] / 25.0) for r in range(NREG)]
        self.rd = [0.5 * self.dev[r] for r in range(NLAND)]
        for hb, vi in enumerate(LAND_HUBS):
            lv = self.level[vi]
            if lv <= 0:
                self.zr[hb] = 0.0
                self.zd[hb] = 0.0
            else:
                self.zr[hb] = 0.07 + 0.011 * min(lv, 28) + 0.010 * self.tier[vi]
                self.zd[hb] = min(1.0, 0.35 + 0.03 * lv + 0.05 * self.tier[vi])
        self.zr[DUMMY_HUB] = 0.0
        self.zd[DUMMY_HUB] = 0.0

    # ---- world events -----------------------------------------------------
    def add_mod(self, scope, target, mult, dur, name, bad):
        self.mods.append({"scope": scope, "target": target, "mult": mult, "name": name,
                          "bad": bad, "until": self.playtime + dur, "dur": dur})
        self.update_mods()

    def update_mods(self):
        self.mods = [m for m in self.mods if m["until"] > self.playtime]
        self.crisis_regions = {m["target"] for m in self.mods
                               if m["bad"] and m["scope"] == "region"}
        self.halted = {m["target"] for m in self.mods if m["bad"] and m["scope"] == "venture"}

    def mod_target(self, m):
        if m["scope"] == "region":
            return REGIONS[m["target"]]["name"]
        if m["scope"] == "venture":
            return VENTURES[m["target"]]["name"]
        return "Worldwide"

    def random_event(self):
        owned = [i for i in range(NV) if self.level[i] > 0]
        # crises only once the empire is up and running, and never stacked endlessly
        n_bad = sum(1 for m in self.mods if m["bad"])
        if owned and self.playtime > 90 and n_bad < 2 and random.random() < BAD_EVENT_CHANCE:
            self.bad_event(owned)
        else:
            self.good_event(owned)

    def good_event(self, owned):
        GOLD = (255, 220, 110)
        r = random.random()
        if owned and r < 0.25:
            vi = random.choice(owned)
            reg = VENTURES[vi]["region"]
            dur = random.randint(40, 70)
            self.add_mod("region", reg, 2.0, dur, "BOOM", False)
            self.add_fx(vi, "boom", "BOOM x2!")
            self.say("Economic boom in %s! Income there x2 for %ds." %
                     (REGIONS[reg]["name"], dur), GOLD)
        elif owned and r < 0.40:
            dur = random.randint(30, 50)
            self.add_mod("global", 0, 1.5, dur, "BULL MARKET", False)
            self.add_fx(None, "cash", "BULL MARKET  x1.5")
            self.say("Bull market! All income x1.5 for %ds." % dur, GOLD)
        else:
            bonus = max(25.0, self.rps_cache * random.uniform(20.0, 60.0))
            msg = random.choice([
                "Market boom! Investors pour in:",
                "A viral campaign lands a mega-contract:",
                "Government subsidy approved:",
                "Rival corporation acquired in a hostile takeover:",
                "Tax loophole discovered by your lawyers:",
            ])
            self.cash += bonus
            self.earned += bonus
            self.add_fx(None, "cash", "+" + money(bonus))
            self.say("%s +%s" % (msg, money(bonus)), GOLD)

    def bad_event(self, owned):
        RED = (255, 110, 110)
        r = random.random()
        # never hit something that is already in trouble
        free = [i for i in owned if i not in self.halted
                and VENTURES[i]["region"] not in self.crisis_regions]
        if not free:
            r = 0.9                                          # fall through to a cash loss
        vi = random.choice(free or owned)
        reg = VENTURES[vi]["region"]
        if 0.60 <= r < 0.75 and any(m["scope"] == "global" and m["bad"] for m in self.mods):
            r = 0.9                                          # no double recession
        if r < 0.38:                                         # regional disaster
            if reg == SPACE:
                name = random.choice(["Solar storm", "Debris cascade", "Reactor fault"])
            else:
                name = random.choice(["Hurricane", "Earthquake", "Flooding", "Wildfires",
                                      "Grid blackout", "Pandemic outbreak", "Civil unrest"])
            mult = random.choice([0.5, 0.4, 0.3])
            dur = random.randint(35, 65)
            self.add_mod("region", reg, mult, dur, name.upper(), True)
            self.add_fx(vi, "disaster", name.upper() + "!", 3.0)
            self.say("%s hits %s! Income there x%.1f for %ds.  ('repair' to fix)" %
                     (name, REGIONS[reg]["name"], mult, dur), RED)
        elif r < 0.60:                                       # one venture stops
            name = random.choice(["Workers strike at", "Supply-chain collapse at",
                                  "Regulators freeze", "Factory fire at"])
            dur = random.randint(30, 50)
            self.add_mod("venture", vi, 0.0, dur, "HALTED", True)
            self.add_fx(vi, "disaster", "HALTED!", 3.0)
            self.say("%s %s! It produces nothing for %ds.  ('repair' to fix)" %
                     (name, VENTURES[vi]["name"], dur), RED)
        elif r < 0.75:                                       # global slump
            dur = random.randint(30, 50)
            self.add_mod("global", 0, 0.7, dur, "RECESSION", True)
            self.add_fx(None, "alert", "RECESSION  x0.7")
            self.say("Global recession! All income x0.7 for %ds.  ('repair' to fix)" % dur, RED)
        else:                                                # straight cash loss
            loss = min(self.cash * 0.25,
                       max(self.cash * random.uniform(0.05, 0.15),
                           self.rps_cache * random.uniform(15.0, 40.0)))
            if loss < 1.0:
                return self.good_event(owned)
            self.cash -= loss
            msg = random.choice([
                "Market crash wipes out",
                "Lawsuit settlement costs you",
                "Hackers drain your accounts:",
                "Tax audit fine:",
                "Rogue executive embezzles",
            ])
            self.add_fx(None, "alert", "-" + money(loss))
            self.say("%s -%s" % (msg, money(loss)), RED)

    def cmd_repair(self):
        bad = [m for m in self.mods if m["bad"]]
        if not bad:
            self.say("No active crises. Everything is running smoothly.")
            return
        lost = max(0.0, self.rps(ignore_bad=True) - self.rps())
        left = max(m["until"] for m in bad) - self.playtime
        cost = max(25.0, lost * left * 0.75)
        if cost > self.cash:
            self.say("Emergency response costs %s (you have %s). Or ride it out: %ds left." %
                     (money(cost), money(self.cash), math.ceil(left)), (255, 150, 110))
            return
        self.cash -= cost
        self.mods = [m for m in self.mods if not m["bad"]]
        self.update_mods()
        self.add_fx(None, "cash", "CRISIS RESOLVED")
        self.say("Emergency crews deployed for %s: all crises resolved." % money(cost),
                 (130, 255, 160))

    # ---- simulation tick --------------------------------------------------
    def update(self, dt):
        self.update_mods()
        self.rps_cache = self.rps()
        gain = self.rps_cache * dt
        self.cash += gain
        self.earned += gain
        self.playtime += dt
        if self.focus_lon is not None and self.t < self.focus_until:
            diff = (self.focus_lon - self.yaw + PI) % TAU - PI      # shortest turn
            self.yaw = (self.yaw + diff * min(1.0, dt * 3.5)) % TAU
        else:
            self.yaw = (self.yaw + math.radians(self.spin_deg) * dt) % TAU
        self.update_visual()
        self.check_unlocks()
        self.update_fx()
        self.mini_update(dt)
        if self.playtime >= self.next_event:
            self.next_event = self.playtime + 50.0 + random.random() * 80.0
            self.random_event()
        if self.playtime >= self.next_save:
            self.next_save = self.playtime + 30.0
            self.save(quiet=True)

    # ---- persistence --------------------------------------------------------
    def save(self, quiet=False):
        data = {"v": 1, "cash": self.cash, "earned": self.earned,
                "level": self.level, "tier": self.tier, "playtime": self.playtime,
                "yaw": self.yaw, "pitch": self.pitch, "spin": self.spin_deg,
                "fps": self.fps, "hist": self.hist[-HIST_MAX:], "saved_at": time.time()}
        try:
            tmp = SAVE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, SAVE_PATH)
            if not quiet:
                self.say("Empire saved to " + SAVE_PATH, (130, 255, 160))
            return True
        except OSError as e:
            self.say("Save failed: %s" % e, (255, 110, 110))
            return False

    def load(self, startup=False):
        try:
            with open(SAVE_PATH) as f:
                d = json.load(f)
            lv = [int(x) for x in d["level"]]
            tr = [int(x) for x in d["tier"]]
            if len(lv) == len(tr) == len(OLD_NAMES) != NV:      # pre-expansion save
                names = [v["name"] for v in VENTURES]
                lv2, tr2 = [0] * NV, [0] * NV
                for old, name in enumerate(OLD_NAMES):
                    lv2[names.index(name)] = lv[old]
                    tr2[names.index(name)] = tr[old]
                lv, tr = lv2, tr2
            if len(lv) != NV or len(tr) != NV:
                raise ValueError("incompatible save")
            self.cash = float(d["cash"])
            self.earned = float(d["earned"])
            self.level, self.tier = lv, tr
            self.playtime = float(d.get("playtime", 0.0))
            self.yaw = float(d.get("yaw", 0.6))
            self.pitch = float(d.get("pitch", self.pitch))
            self.spin_deg = float(d.get("spin", 14.0))
            self.fps = int(d.get("fps", 20))
            hist = d.get("hist", [])
            if isinstance(hist, list):
                self.hist = [str(x)[:80] for x in hist if str(x).strip()][-HIST_MAX:]
                self.hpos = len(self.hist)
            self.next_event = self.playtime + 60.0
            self.next_save = self.playtime + 30.0
            self.update_visual()
            self.n_unlocked = 0
            self.seen_unlocked = set()
            self.check_unlocks()
            away = max(0.0, min(12 * 3600.0, time.time() - float(d.get("saved_at", time.time()))))
            gain = self.rps() * away * OFFLINE_RATE
            if gain > 0 and away > 30:
                self.cash += gain
                self.earned += gain
                h, rem = divmod(int(away), 3600)
                self.say("Welcome back! Away %dh%02dm: empire earned %s (%d%% offline rate)"
                         % (h, rem // 60, money(gain), OFFLINE_RATE * 100), (255, 220, 110))
            else:
                self.say("Save loaded.", (130, 255, 160))
            return True
        except FileNotFoundError:
            if not startup:
                self.say("No save file found.", (255, 150, 110))
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.say("Could not load save: %s" % e, (255, 110, 110))
        return False

    # ---- commands -----------------------------------------------------------
    def parse_idx(self, args):
        if not args:
            self.say("Specify a venture number 1-%d." % NV, (255, 150, 110))
            return None
        try:
            i = int(args[0]) - 1
        except ValueError:
            self.say("'%s' is not a venture number." % args[0], (255, 150, 110))
            return None
        if not 0 <= i < NV:
            self.say("Venture number must be 1-%d." % NV, (255, 150, 110))
            return None
        return i

    def cmd_buy(self, args):
        i = self.parse_idx(args)
        if i is None:
            return
        if not self.is_unlocked(i):
            self.say("Venture %d is still locked: %s first." % (i + 1, self.lock_reason(i)),
                     (255, 150, 110))
            return
        qty = 1
        if len(args) > 1:
            if args[1] == "max":
                qty = self.max_affordable(i)
                if qty <= 0:
                    self.say("Cannot afford even one level (%s needed)." %
                             money(self.unit_cost(i)), (255, 150, 110))
                    return
            else:
                try:
                    qty = int(args[1])
                except ValueError:
                    self.say("Quantity must be a number or 'max'.", (255, 150, 110))
                    return
                if qty < 1 or qty > 100000:
                    self.say("Quantity must be between 1 and 100000.", (255, 150, 110))
                    return
        cost = self.unit_cost(i, qty)
        if cost > self.cash:
            self.say("Need %s for %dx %s (you have %s)." %
                     (money(cost), qty, VENTURES[i]["name"], money(self.cash)), (255, 150, 110))
            return
        before = self.mults()
        was_new = self.level[i] == 0
        self.cash -= cost
        self.level[i] += qty
        self.add_fx(i, "buy", "NEW HUB!" if was_new else "+%d LV" % qty)
        self.check_bonuses(before)
        self.say("Bought %dx %s for %s  ->  Lv %d" %
                 (qty, VENTURES[i]["name"], money(cost), self.level[i]), (130, 255, 160))
        self.update_visual()

    def cmd_upgrade(self, args):
        i = self.parse_idx(args)
        if i is None:
            return
        if self.tier[i] >= MAX_TIER:
            self.say("%s is already at maximum tier." % VENTURES[i]["name"], (255, 150, 110))
            return
        if self.level[i] < self.upgrade_need(i):
            self.say("%s needs Lv %d to upgrade (now Lv %d)." %
                     (VENTURES[i]["name"], self.upgrade_need(i), self.level[i]), (255, 150, 110))
            return
        cost = self.upgrade_cost(i)
        if cost > self.cash:
            self.say("Upgrade costs %s (you have %s)." % (money(cost), money(self.cash)),
                     (255, 150, 110))
            return
        self.cash -= cost
        self.tier[i] += 1
        self.add_fx(i, "upgrade", "TIER %d!" % self.tier[i])
        self.say("Upgraded %s to Tier %d for %s  (income x2)" %
                 (VENTURES[i]["name"], self.tier[i], money(cost)), (255, 220, 110))
        self.update_visual()

    def cmd_buyall(self):
        bought = 0
        spent = 0.0
        before = self.mults()
        per = [0] * NV
        for _ in range(5000):
            best, bc = -1, None
            for i in range(NV):
                if not self.is_unlocked(i):
                    continue
                c = self.unit_cost(i)
                if c <= self.cash and (bc is None or c < bc):
                    best, bc = i, c
            if best < 0:
                break
            self.cash -= bc
            spent += bc
            self.level[best] += 1
            per[best] += 1
            bought += 1
        for i in range(NV):
            if per[i]:
                self.add_fx(i, "buy", "+%d LV" % per[i])
        self.check_bonuses(before)
        if bought:
            self.say("Bought %d levels across your empire for %s." % (bought, money(spent)),
                     (130, 255, 160))
        else:
            self.say("You cannot afford anything right now.", (255, 150, 110))
        self.update_visual()

    def show_help(self):
        H = (180, 235, 255)
        D = (200, 210, 228)
        self.overlay = {"title": "HELP", "until": time.time() + 40, "lines": [
            ("buy <#> [qty|max]   buy levels of venture #", D),
            ("buyall              buy cheapest levels you can afford", D),
            ("upgrade <#>         raise tier (income x2); needs Lv 5,10..", D),
            ("work [game]         minigame -> randomised cash", D),
            ("repair              pay to end all active crises now", D),
            ("status              detailed empire report", D),
            ("focus on|off        camera follows purchases & events", D),
            ("spin <deg/s>  tilt <deg>  fps <n>   globe / display", D),
            ("save | load | reset confirm | quit", D),
            ("Up/Down arrows recall earlier commands (kept in save).", D),
            ("", D),
            ("BONUSES", H),
            ("Own every venture in a zone -> x1.5 income there", D),
            ("Own all %d ventures         -> x2 income globally" % NV, D),
            ("Every 25 levels of a venture -> x2 income", D),
            ("", D),
            ("UNLOCKING", H),
            ("New ventures appear as your total earnings grow.", D),
            ("Space needs a venture in every land zone first.", D),
            ("", D),
            ("WORLD EVENTS", H),
            ("Booms & bull markets raise income for a while; storms,", D),
            ("strikes, recessions and crashes hurt. Hit zones turn red", D),
            ("on the globe. Ride them out or 'repair' them.", D),
            ("", D),
            ("WORK GAMES  (TAB aborts)", H),
            ("timing  code  math  memory  reflex  count", D),
            ("Better score = bigger payout; streaks & jackpots stack.", D),
        ]}

    def show_status(self):
        H = (180, 235, 255)
        D = (200, 210, 228)
        lines = [
            ("Bank %s   Revenue %s/s" % (money(self.cash), money(self.rps())), (130, 255, 160)),
            ("Earned %s   Played %dh %02dm   Global x%.1f" % (
                money(self.earned), self.playtime // 3600, (self.playtime % 3600) // 60,
                self.global_mult()), D),
            ("", D), ("INCOME BREAKDOWN", H)]
        hidden = 0
        for i in range(NV):
            if not self.is_unlocked(i):              # undiscovered ventures stay secret
                hidden += 1
                continue
            em = self.event_mult(i)
            lines.append(("%2d %-19s %12s/s%s" % (i + 1, VENTURES[i]["name"],
                                                   money(self.venture_income(i)),
                                                   "" if em == 1.0 else "  x%.1f" % em),
                          REGIONS[VENTURES[i]["region"]]["col"]))
        if hidden:
            lines.append(("   + %d undiscovered venture%s" % (hidden, "" if hidden == 1 else "s"),
                          (75, 90, 120)))
        lines.append(("", D))
        lines.append(("ZONES", H))
        for r in range(NREG):
            lines.append(("%-9s synergy x%.1f   development %3d%%" %
                          (REGIONS[r]["name"], self.region_mult(r), int(self.dev[r] * 100)),
                          REGIONS[r]["col"]))
        lines.append(("", D))
        lines.append(("Save file: " + SAVE_PATH, (110, 130, 170)))
        self.overlay = {"title": "EMPIRE STATUS", "until": time.time() + 40, "lines": lines}

    def execute(self, raw):
        line = raw.strip()
        if not line:
            return
        self.overlay = None
        self.say("> " + line, (110, 130, 170))
        parts = line.lower().split()
        cmd, args = parts[0], parts[1:]
        if cmd in ("q", "quit", "exit"):
            self.running = False
        elif cmd in ("h", "help", "?"):
            self.show_help()
        elif cmd in ("s", "status", "stats"):
            self.show_status()
        elif cmd in ("b", "buy"):
            self.cmd_buy(args)
        elif cmd in ("u", "up", "upgrade"):
            self.cmd_upgrade(args)
        elif cmd == "buyall":
            self.cmd_buyall()
        elif cmd in ("r", "repair", "fix"):
            self.cmd_repair()
        elif cmd in ("w", "work"):
            self.start_work(args[0] if args else None)
        elif cmd == "focus":
            if args and args[0] in ("on", "off"):
                self.focus = args[0] == "on"
                self.say("Camera focus on purchases: %s." % args[0].upper())
            else:
                self.say("Usage: focus on|off", (255, 150, 110))
        elif cmd == "save":
            self.save()
        elif cmd == "load":
            self.load()
        elif cmd == "spin":
            try:
                self.spin_deg = max(-720.0, min(720.0, float(args[0])))
                self.say("Spin set to %+.1f deg/s." % self.spin_deg)
            except (IndexError, ValueError):
                self.say("Usage: spin <degrees per second>", (255, 150, 110))
        elif cmd == "tilt":
            try:
                deg = max(-80.0, min(80.0, float(args[0])))
                self.pitch = math.radians(deg)
                self.say("Tilt set to %.1f deg." % deg)
            except (IndexError, ValueError):
                self.say("Usage: tilt <degrees -80..80>", (255, 150, 110))
        elif cmd == "fps":
            try:
                self.fps = max(5, min(60, int(args[0])))
                self.say("Target frame rate: %d FPS." % self.fps)
            except (IndexError, ValueError):
                self.say("Usage: fps <5..60>", (255, 150, 110))
        elif cmd == "reset":
            if args and args[0] == "confirm":
                self.cash = 25.0
                self.earned = 25.0
                self.level = [0] * NV
                self.tier = [0] * NV
                self.mods = []
                self.playtime = 0.0
                self.update_visual()
                self.n_unlocked = 0
                self.seen_unlocked = set()
                self.check_unlocks()
                self.say("Empire liquidated. A fresh start awaits.", (255, 220, 110))
            else:
                self.say("This erases everything. Type 'reset confirm' to proceed.",
                         (255, 150, 110))
        elif cmd in ("clear", "cls"):
            pass
        else:
            self.say("Unknown command '%s'. Type 'help'." % cmd, (255, 150, 110))

    # ---- effects -------------------------------------------------------------
    def add_fx(self, vi, kind, text, dur=None):
        dur = dur or (2.4 if kind == "upgrade" else 1.9)
        self.fx.append({"vi": vi, "kind": kind, "text": text, "t0": self.t,
                        "dur": dur, "seed": random.random() * TAU})
        if len(self.fx) > 30:
            self.fx = self.fx[-30:]
        if vi is not None and self.focus and vi in HUB_OF:
            self.focus_lon = -math.radians(VENTURES[vi]["lon"])
            self.focus_until = self.t + 2.6

    def update_fx(self):
        t = self.t
        self.fx = [f for f in self.fx if 0 <= t - f["t0"] < f["dur"]]
        h = [0.0] * (DUMMY_HUB + 1)
        for f in self.fx:
            vi = f["vi"]
            if vi is not None and vi in HUB_OF:
                v = 1.0 - (t - f["t0"]) / f["dur"]
                if v > h[HUB_OF[vi]]:
                    h[HUB_OF[vi]] = v
        self.fxh = h

    # ---- work minigames ------------------------------------------------------
    def start_work(self, arg=None):
        if self.mini is not None:
            return
        if self.playtime < self.work_ready:
            self.say("Your team is catching its breath: work again in %ds." %
                     math.ceil(self.work_ready - self.playtime), (255, 150, 110))
            return
        kinds = list(MINI_NAMES)
        kind = arg if arg in kinds else random.choice(kinds)
        m = {"kind": kind, "t0": self.t}
        if kind == "timing":
            m.update(round=0, results=[], ph=random.random() * 2.0, hold=None, last_pos=0.0)
            self.new_round(m)
        elif kind == "code":
            alpha = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            m.update(codes=["".join(random.choice(alpha) for _ in range(6)) for _ in range(3)],
                     ci=0, pos=0, errors=0, limit=16.0, deadline=self.t + 16.0, flash=0.0)
        elif kind in ("memory", "reflex", "count"):    # 3-round games
            m.update(round=0, results=[], hold=None, note="")
            self.new_round(m)
        else:
            probs = []
            for _ in range(4):
                op = random.choice("+-*")
                if op == "+":
                    a, b = random.randint(10, 99), random.randint(10, 99)
                    ans = a + b
                elif op == "-":
                    a = random.randint(25, 99)
                    b = random.randint(10, a - 1)
                    ans = a - b
                else:
                    a, b = random.randint(3, 19), random.randint(3, 12)
                    ans = a * b
                probs.append(("%d %s %d" % (a, "x" if op == "*" else op, b), ans))
            m.update(probs=probs, qi=0, buf="", marks=[], limit=20.0, deadline=self.t + 20.0)
        self.mini = m
        self.overlay = None
        self.say("%s - go!  (TAB aborts)" % MINI_NAMES[kind], (255, 230, 120))

    def new_round(self, m):
        r = m["round"]
        m["hold"] = None
        k = m["kind"]
        if k == "timing":
            w = TIMING_ZONE[r]
            m["w"] = w
            m["center"] = random.uniform(w / 2 + 0.04, 1.0 - w / 2 - 0.04)
            m["speed"] = TIMING_SPEED[r]
        elif k == "memory":                    # memorise a digit string, then type it
            n = 5 + r
            m["seq"] = "".join(random.choice("0123456789") for _ in range(n))
            m["buf"] = ""
            m["show_until"] = self.t + 1.0 + 0.4 * n
            m["deadline"] = m["show_until"] + 4.0 + n
        elif k == "reflex":                    # hit SPACE the instant BUY appears
            m["go_at"] = self.t + random.uniform(1.2, 3.6)
            m["seen"] = None
        elif k == "count":                     # count one symbol in a grid
            target = random.choice(COUNT_SYMS)
            others = [c for c in COUNT_SYMS if c != target]
            n = random.randint(6, 14)
            cells = [target] * n + [random.choice(others) for _ in range(5 * 12 - n)]
            random.shuffle(cells)
            m.update(target=target, cells=cells, answer=n, buf="",
                     deadline=self.t + 11.0 - r)

    def end_round(self, m, score, note):
        m["results"].append(max(0.0, min(1.0, score)))
        m["note"] = note
        m["hold"] = self.t + 1.2

    def submit_memory(self, m):
        n = len(m["seq"])
        good = sum(1 for a, b in zip(m["buf"], m["seq"]) if a == b)
        self.end_round(m, good / n, "%d/%d digits" % (good, n))

    def submit_count(self, m):
        try:
            diff = abs(int(m["buf"]) - m["answer"])
        except ValueError:
            diff = 99
        self.end_round(m, {0: 1.0, 1: 0.5, 2: 0.2}.get(diff, 0.0),
                       "there were %d" % m["answer"])

    @staticmethod
    def tri(ph):
        p = ph % 2.0
        return p if p <= 1.0 else 2.0 - p

    def mini_update(self, dt):
        m = self.mini
        if m is None:
            return
        k = m["kind"]
        if k == "timing":
            if m["hold"] is None:
                m["ph"] += m["speed"] * dt
            elif self.t >= m["hold"]:
                m["round"] += 1
                if m["round"] >= 3:
                    self.finish_work(sum(m["results"]) / 3.0)
                else:
                    self.new_round(m)
        elif k in ("memory", "reflex", "count"):
            if m["hold"] is not None:
                if self.t >= m["hold"]:
                    m["round"] += 1
                    if m["round"] >= 3:
                        self.finish_work(sum(m["results"]) / 3.0)
                    else:
                        self.new_round(m)
            elif k == "memory" and self.t >= m["deadline"]:
                self.submit_memory(m)
            elif k == "count" and self.t >= m["deadline"]:
                self.submit_count(m)
            elif k == "reflex" and m["seen"] is not None and time.perf_counter() - m["seen"] > 1.2:
                self.end_round(m, 0.0, "TOO SLOW")
        elif self.t >= m["deadline"]:
            if k == "code":
                self.finish_code()
            else:
                self.finish_math()

    def mini_key(self, ch):
        m = self.mini
        k = m["kind"]
        if ch == "\t":
            self.mini = None
            self.work_ready = self.playtime + 3.0
            self.say("Work aborted.", (255, 150, 110))
            return
        if k == "timing":
            if ch in (" ", "\r", "\n") and m["hold"] is None:
                pos = self.tri(m["ph"])
                dist = abs(pos - m["center"])
                half = m["w"] / 2.0
                m["results"].append(1.0 - 0.5 * dist / half if dist <= half else 0.0)
                m["last_pos"] = pos
                m["hold"] = self.t + 0.8
        elif k == "code":
            if not (ch.isascii() and ch.isalnum()):
                return
            target = m["codes"][m["ci"]][m["pos"]]
            if ch.upper() == target:
                m["pos"] += 1
                if m["pos"] >= 6:
                    m["ci"] += 1
                    m["pos"] = 0
                    if m["ci"] >= 3:
                        self.finish_code()
            else:
                m["errors"] += 1
                m["flash"] = self.t + 0.25
        elif k in ("memory", "reflex", "count"):
            if m["hold"] is not None:
                return
            if k == "reflex":
                if ch in (" ", "\r", "\n"):
                    if m["seen"] is None:
                        self.end_round(m, 0.0, "TOO EARLY!")
                    else:
                        rt = time.perf_counter() - m["seen"]
                        self.end_round(m, (0.9 - rt) / 0.6, "%d ms" % (rt * 1000))
                return
            if k == "memory" and self.t < m["show_until"]:
                return                                     # still memorising
            limit = len(m["seq"]) if k == "memory" else 3
            if ch.isdigit() and len(m["buf"]) < limit:
                m["buf"] += ch
                if k == "memory" and len(m["buf"]) == limit:
                    self.submit_memory(m)
            elif ch in ("\x7f", "\b"):
                m["buf"] = m["buf"][:-1]
            elif ch in ("\r", "\n") and m["buf"]:
                if k == "memory":
                    self.submit_memory(m)
                else:
                    self.submit_count(m)
        else:
            if ch.isdigit() or (ch == "-" and not m["buf"]):
                if len(m["buf"]) < 6:
                    m["buf"] += ch
            elif ch in ("\x7f", "\b"):
                m["buf"] = m["buf"][:-1]
            elif ch in ("\r", "\n") and m["buf"] not in ("", "-"):
                ok = int(m["buf"]) == m["probs"][m["qi"]][1]
                m["marks"].append(ok)
                m["qi"] += 1
                m["buf"] = ""
                if m["qi"] >= len(m["probs"]):
                    self.finish_math()

    def finish_code(self):
        m = self.mini
        typed = m["ci"] * 6 + m["pos"]
        acc = max(0.35, 1.0 - 0.07 * m["errors"])
        remaining = max(0.0, m["deadline"] - self.t) / m["limit"]
        bonus = 0.15 * remaining if m["ci"] >= 3 else 0.0
        self.finish_work(min(1.0, typed / 18.0 * acc + bonus))

    def finish_math(self):
        m = self.mini
        remaining = max(0.0, m["deadline"] - self.t) / m["limit"]
        good = sum(1 for x in m["marks"] if x)
        self.finish_work(good / 4.0 * (0.75 + 0.25 * remaining))

    def finish_work(self, score):
        name = MINI_NAMES[self.mini["kind"]]
        self.mini = None
        score = max(0.0, min(1.0, score))
        rating = ("S" if score >= 0.92 else "A" if score >= 0.75 else
                  "B" if score >= 0.5 else "C" if score >= 0.25 else "F")
        perf = 0.15 + 1.85 * score
        roll = random.uniform(0.75, 1.35)
        crit, tag = 1.0, ""
        r = random.random()
        if score >= 0.5:
            if r < 0.04:
                crit, tag = 6.0, "JACKPOT!!"
                self.boost_mult = 2.0
                self.boost_until = self.playtime + 30.0
            elif r < 0.16:
                crit, tag = 2.5, "BIG DEAL!"
        self.streak = self.streak + 1 if score >= 0.5 else 0
        smult = 1.0 + 0.1 * min(self.streak, 10)
        base = 40.0 + self.rps() * WORK_RPS_SECONDS
        reward = base * perf * roll * crit * smult
        self.cash += reward
        self.earned += reward
        self.work_ready = self.playtime + WORK_COOLDOWN
        self.add_fx(None, "cash", "+" + money(reward) + ("  " + tag if tag else ""))
        col = {"S": (255, 220, 110), "A": (120, 255, 150), "B": (150, 235, 255),
               "C": (255, 190, 120), "F": (255, 110, 110)}[rating]
        D = (200, 210, 228)
        lines = [("%s  -  RATING %s  (%d%%)" % (name, rating, int(score * 100)), col),
                 ("", D),
                 ("Base payout        %s" % money(base), D),
                 ("Performance        x%.2f" % perf, D),
                 ("Market roll        x%.2f" % roll, D),
                 ("Streak (%d)         x%.1f" % (self.streak, smult), D)]
        if tag:
            lines.append(("%-18s  x%.1f" % (tag, crit), (255, 220, 110)))
        if crit >= 6.0:
            lines.append(("REVENUE BOOST x2 for 30 seconds!", (255, 120, 210)))
        lines += [("", D), ("EARNED  %s" % money(reward), (120, 255, 150)),
                  ("", D), ("Next shift available in %d seconds." % WORK_COOLDOWN, (110, 130, 170))]
        self.overlay = {"title": "WORK RESULT", "until": time.time() + 9, "lines": lines}
        self.say("%s: rating %s -> +%s%s" % (name, rating, money(reward),
                                            "  " + tag if tag else ""), col)

    def mini_lines(self, SW):
        m = self.mini
        k = m["kind"]
        DIM = (110, 130, 170)
        WHITE = (235, 240, 250)
        GREEN = (120, 255, 150)
        GOLD = (255, 220, 110)
        RED = (255, 110, 110)
        L = [rule_line("WORK: " + MINI_NAMES[k], SW, "TAB aborts"), seg_line([], SW)]

        def label(sc):
            return (("PERFECT", GOLD) if sc >= 0.9 else ("GOOD", GREEN) if sc >= 0.7
                    else ("OK", (150, 235, 255)) if sc > 0 else ("MISS", RED))

        if k == "timing":
            L.append(seg_line([(" Stop the cursor inside the green zone!", WHITE)], SW))
            L.append(seg_line([(" Press SPACE (or ENTER).  3 rounds, each faster.", DIM)], SW))
            L.append(seg_line([], SW))
            BW = min(SW - 6, 46)
            pos = self.tri(m["ph"]) if m["hold"] is None else m["last_pos"]
            cur = int(pos * (BW - 1))
            lo = int((m["center"] - m["w"] / 2) * (BW - 1))
            hi = int((m["center"] + m["w"] / 2) * (BW - 1))
            segs = [(" [", DIM)]
            for i in range(BW):
                if i == cur:
                    segs.append(("\u2588", (255, 255, 255)))
                elif lo <= i <= hi:
                    segs.append(("#", GREEN))
                else:
                    segs.append(("-", (70, 85, 120)))
            segs.append(("]", DIM))
            L.append(seg_line(segs, SW))
            L.append(seg_line([], SW))
            segs = [(" Round %d/3   " % min(3, m["round"] + 1), WHITE)]
            for r, sc in enumerate(m["results"]):
                txt, rgb = label(sc)
                segs.append(("R%d %s  " % (r + 1, txt), rgb))
            L.append(seg_line(segs, SW))
        elif k == "code":
            left = max(0.0, m["deadline"] - self.t)
            L.append(seg_line([(" Type the three access codes.", WHITE)], SW))
            L.append(seg_line([(" Time left: ", DIM),
                               ("%4.1fs" % left, RED if left < 5 else GOLD),
                               ("   Errors: %d" % m["errors"], DIM)], SW))
            L.append(seg_line([], SW))
            for i, code in enumerate(m["codes"]):
                if i < m["ci"]:
                    L.append(seg_line([("   [%d] " % (i + 1), DIM), (code, GREEN), ("  OK", GREEN)], SW))
                elif i == m["ci"]:
                    bad = self.t < m["flash"]
                    L.append(seg_line([("  >[%d] " % (i + 1), GOLD),
                                       (code[:m["pos"]], GREEN),
                                       (code[m["pos"]:m["pos"] + 1], RED if bad else (255, 255, 255)),
                                       (code[m["pos"] + 1:], WHITE)], SW))
                else:
                    L.append(seg_line([("   [%d] " % (i + 1), DIM), (code, (150, 160, 180))], SW))
        elif k in ("memory", "reflex", "count"):
            held = m["hold"] is not None
            if k == "memory":
                L.append(seg_line([(" Memorise the account number, then type it.", WHITE)], SW))
                seq = m["seq"]
                if held:
                    L.append(seg_line([(" Result: ", DIM), (m["note"], GOLD)], SW))
                    L.append(seg_line([], SW))
                    L.append(seg_line([("   was:  ", DIM), ("  ".join(seq), WHITE)], SW))
                    L.append(seg_line([("   you:  ", DIM)] + [
                        (c + "  ", GREEN if i < len(seq) and c == seq[i] else RED)
                        for i, c in enumerate(m["buf"])], SW))
                elif self.t < m["show_until"]:
                    left = m["show_until"] - self.t
                    L.append(seg_line([(" Memorise!  ", GOLD),
                                       ("█" * int(left * 6), (255, 200, 90))], SW))
                    L.append(seg_line([], SW))
                    L.append(seg_line([("   ", DIM), ("  ".join(seq), (255, 255, 255))], SW))
                    L.append(seg_line([], SW))
                else:
                    left = max(0.0, m["deadline"] - self.t)
                    cur = "█" if int(self.t * 2) % 2 == 0 else "_"
                    shown = [c for c in m["buf"]] + [cur] + ["_"] * (len(seq) - len(m["buf"]) - 1)
                    L.append(seg_line([(" Type it!  time ", DIM),
                                       ("%4.1fs" % left, RED if left < 3 else GOLD)], SW))
                    L.append(seg_line([], SW))
                    L.append(seg_line([("   ", DIM), ("  ".join(shown[:len(seq)]), GREEN)], SW))
                    L.append(seg_line([], SW))
            elif k == "reflex":
                L.append(seg_line([(" Hit SPACE the instant the BUY signal flashes.", WHITE)], SW))
                L.append(seg_line([(" Too early = miss.  Under 300 ms = perfect.", DIM)], SW))
                BW = min(SW - 4, 40)
                if held:
                    txt, rgb, bg = m["note"], (255, 255, 255), (60, 70, 100)
                elif self.t >= m["go_at"]:
                    if m["seen"] is None:
                        m["seen"] = time.perf_counter()     # timer starts when it is drawn
                    txt, rgb, bg = ">>>  BUY NOW!  <<<", (10, 40, 10), (90, 255, 120)
                else:
                    txt, rgb, bg = "hold ... watch the ticker", (255, 220, 220), (150, 30, 30)
                bar = "\x1b[48;2;%d;%d;%dm" % bg
                L.append(seg_line([], SW))
                for row in range(3):
                    inner = txt.center(BW) if row == 1 else " " * BW
                    L.append("  " + bar + fg(rgb) + inner + RESET + " " * (SW - BW - 2))
                L.append(seg_line([], SW))
            else:
                tgt = m["target"]
                left = max(0.0, m["deadline"] - self.t)
                L.append(seg_line([(" Count the ", WHITE), (" " + tgt + " ", COUNT_COLS[tgt]),
                                   (" symbols.  ENTER submits.  ", WHITE),
                                   ("%4.1fs" % left, RED if left < 3 else GOLD)], SW))
                L.append(seg_line([], SW))
                cells = m["cells"]
                for row in range(5):
                    segs = [("   ", DIM)]
                    for c in cells[row * 12:(row + 1) * 12]:
                        hl = held and c == tgt
                        col = COUNT_COLS[c] if (not held or hl) else (60, 70, 95)
                        segs.append((c + " ", col))
                    L.append(seg_line(segs, SW))
                cur = "█" if int(self.t * 2) % 2 == 0 else " "
                L.append(seg_line([(" Answer: ", DIM), (m["buf"] + ("" if held else cur), GREEN)]
                                  + ([("   " + m["note"], GOLD)] if held else []), SW))
            L.append(seg_line([], SW))
            segs = [(" Round %d/3   " % min(3, m["round"] + 1), WHITE)]
            for r, sc in enumerate(m["results"]):
                txt, rgb = label(sc)
                segs.append(("R%d %s  " % (r + 1, txt), rgb))
            L.append(seg_line(segs, SW))
        else:
            left = max(0.0, m["deadline"] - self.t)
            L.append(seg_line([(" Balance the books. Type the answer, ENTER submits.", WHITE)], SW))
            L.append(seg_line([(" Time left: ", DIM), ("%4.1fs" % left, RED if left < 6 else GOLD)], SW))
            L.append(seg_line([], SW))
            qi = m["qi"]
            if qi < len(m["probs"]):
                cur = "\u2588" if int(self.t * 2) % 2 == 0 else " "
                L.append(seg_line([(" Q%d/4:  " % (qi + 1), GOLD),
                                   (m["probs"][qi][0] + " = ", (255, 255, 255)),
                                   (m["buf"] + cur, GREEN)], SW))
            L.append(seg_line([], SW))
            L.append(seg_line([(" Answers: ", DIM)] +
                              [("[OK] " if x else "[X] ", GREEN if x else RED) for x in m["marks"]], SW))
        L.append(seg_line([], SW))
        L.append(seg_line([(" Streak: %d" % self.streak, GOLD),
                           ("   (win a B or better to keep it)", DIM)], SW))
        return L

    # ---- keyboard -----------------------------------------------------------
    def hist_move(self, d):
        """Up (d=-1) / Down (d=+1) through earlier commands; past the newest
        entry you get back whatever you were typing before."""
        if self.mini is not None or not self.hist:
            return
        if self.hpos >= len(self.hist):
            self.draft = self.buf
        self.hpos = max(0, min(len(self.hist), self.hpos + d))
        self.buf = self.hist[self.hpos] if self.hpos < len(self.hist) else self.draft

    def key(self, ch):
        if self.esc == 1:
            self.esc = 2 if ch in "[O" else 0
            return
        if self.esc == 2:
            if ch.isalpha() or ch == "~":
                self.esc = 0
                if ch == "A":
                    self.hist_move(-1)
                elif ch == "B":
                    self.hist_move(1)
            return
        if ch == "\x03":
            self.running = False
            return
        if ch == "\x1b":
            self.esc = 1
            return
        if self.mini is not None:
            self.mini_key(ch)
            return
        if ch in ("\r", "\n"):
            cmd = self.buf
            self.buf = ""
            if cmd.strip() and (not self.hist or self.hist[-1] != cmd):
                self.hist.append(cmd)
                self.hist = self.hist[-HIST_MAX:]
            self.hpos = len(self.hist)
            self.draft = ""
            self.execute(cmd)
        elif ch in ("\x7f", "\b"):
            self.buf = self.buf[:-1]
        elif ch == "\x15":
            self.buf = ""
        elif ch.isprintable() and len(self.buf) < 80:
            self.buf += ch

    # ---- sidebar ------------------------------------------------------------
    def log_lines(self, SW, n):
        """The newest log entries, word-wrapped to the sidebar, bottom-aligned."""
        out = []
        for stamp, text, rgb in reversed(self.log):
            if len(out) >= n:
                break
            pad = " " * (len(stamp) + 1)
            chunks = textwrap.wrap(text, max(10, SW - len(pad) - 1)) or [""]
            rows = [seg_line([(" " + stamp, (80, 95, 130)), (chunks[0], rgb)], SW)]
            rows += [seg_line([(pad + c, rgb)], SW) for c in chunks[1:]]
            if out and len(out) + len(rows) > n:
                break                                # don't show half a message
            out = rows + out
        out = out[-n:] if n > 0 else []
        return [" " * SW] * (n - len(out)) + out

    def sidebar(self, SW, GH):
        L = []
        DIM = (110, 130, 170)
        FAINT = (75, 90, 120)
        WHITE = (235, 240, 250)
        GREEN = (120, 255, 150)
        RED = (255, 110, 110)
        GOLD = (255, 220, 110)
        blank = " " * SW
        if self.mini is not None:
            L = self.mini_lines(SW)
        elif self.overlay and time.time() < self.overlay["until"]:
            L.append(rule_line(self.overlay["title"], SW, "any command closes"))
            for text, rgb in self.overlay["lines"]:
                L.append(seg_line([(" " + text, rgb)], SW))
        else:
            self.overlay = None
            # only unlocked ventures are listed, plus a teaser for the next one
            shown = [i for i in range(NV) if self.is_unlocked(i)]
            locked = [i for i in range(NV) if not self.is_unlocked(i)]
            teaser = locked[0] if locked else None
            zone_rows = (NREG + 1) // 2
            ev_rows = (1 + len(self.mods)) if self.mods else 0
            other = (1 + (2 if teaser is not None else 0) + 1 + zone_rows + ev_rows
                     + 1 + 5 + 3)                       # +log header, 5 log rows, 3 spacers
            wide = GH - other >= 2 * len(shown)
            spacer = GH - other - (2 if wide else 1) * len(shown) >= 0

            owned = sum(1 for l in self.level if l > 0)
            L.append(rule_line("VENTURES", SW, "%d/%d owned" % (owned, NV)))
            for i in shown:
                v = VENTURES[i]
                rc = REGIONS[v["region"]]["col"]
                lv, tr = self.level[i], self.tier[i]
                inc = self.venture_income(i)
                if lv <= 0:
                    inc_s, inc_c = "not owned", FAINT
                elif self.event_mult(i) < 1.0:
                    inc_s, inc_c = "+" + money(inc) + "/s", RED
                else:
                    inc_s, inc_c = "+" + money(inc) + "/s", GOLD
                c1 = self.unit_cost(i)
                can1 = c1 <= self.cash
                uc = self.upgrade_cost(i)
                up_ok = tr < MAX_TIER and lv >= self.upgrade_need(i) and uc <= self.cash
                segs = [("%3d " % (i + 1), rc), ("■ ", rc),
                        (v["name"], WHITE if lv > 0 else (165, 175, 195))]
                lvl = [(" Lv%d" % lv, (235, 235, 160))] if lv else []
                if tr:
                    lvl.append((" T%d" % tr, (255, 200, 90)))
                if wide:
                    room = SW - seg_len(segs) - seg_len(lvl) - 1
                    segs += lvl + [(inc_s.rjust(max(1, room)), inc_c)]
                    L.append(seg_line(segs, SW))
                    left = [("        buy ", FAINT), (money(c1), GREEN if can1 else RED)]
                    if can1:
                        left.append((" (x%d)" % self.max_affordable(i), FAINT))
                    if tr >= MAX_TIER:
                        right = [("max tier ", (255, 200, 90))]
                    elif lv < self.upgrade_need(i):
                        right = [("upgrade at Lv%d " % self.upgrade_need(i), FAINT)]
                    else:
                        right = [("upgrade ", FAINT), (money(uc) + " ", GREEN if up_ok else RED)]
                    gap = SW - seg_len(left) - seg_len(right)
                    L.append(seg_line(left + [(" " * max(1, gap), FAINT)] + right, SW))
                else:                                   # compact: one row per venture
                    cost = money(c1) + ("^" if up_ok else " ")
                    segs[2] = (v["name"][:17].ljust(17), segs[2][1])
                    segs.append((" Lv%-4d" % lv if lv else "       ", (235, 235, 160)))
                    room = SW - seg_len(segs) - len(cost) - 2
                    segs += [(inc_s.rjust(max(1, room)), inc_c),
                             (" " + cost, GREEN if can1 else RED)]
                    L.append(seg_line(segs, SW))
            if teaser is not None:
                L.append(seg_line([("%3d " % (teaser + 1), FAINT), ("□ ", FAINT),
                                   ("??? locked: " + self.lock_reason(teaser), FAINT)], SW))
                if len(locked) > 1:
                    L.append(seg_line([("      + %d more to discover" % (len(locked) - 1),
                                        (60, 72, 100))], SW))

            # zones in two columns:  name  bar  pct  flag
            if spacer:
                L.append(blank)
            L.append(rule_line("ZONES", SW, "* full zone = x1.5   ! crisis"))
            cw = SW // 2
            nbar = max(4, cw - 18)
            for row in range(zone_rows):
                segs = []
                for r in (row, row + zone_rows):
                    if r >= NREG:
                        continue
                    rc = REGIONS[r]["col"]
                    d = self.dev[r]
                    filled = int(round(d * nbar))
                    hit = r in self.crisis_regions
                    flag = ("!", RED) if hit else (("*", GOLD) if self.region_mult(r) > 1
                                                   else (" ", DIM))
                    cell = [(" " + REGIONS[r]["name"].ljust(10), RED if hit else rc),
                            ("█" * filled, rc), ("░" * (nbar - filled), (50, 60, 85)),
                            ("%4d%%" % int(d * 100), WHITE), flag]
                    segs += cell + [(" " * max(0, cw - seg_len(cell)), DIM)]
                L.append(seg_line(segs, SW))

            if self.mods:
                if spacer:
                    L.append(blank)
                L.append(rule_line("WORLD EVENTS", SW, "repair = end crises"))
                for m in sorted(self.mods, key=lambda m: m["until"]):
                    left = math.ceil(m["until"] - self.playtime)
                    col = RED if m["bad"] else GOLD
                    head = (" ▼ " if m["bad"] else " ▲ ") + m["name"]
                    tail = " x%.1f %3ds" % (m["mult"], left)
                    mid = " " + self.mod_target(m)
                    room = SW - len(head) - len(tail) - 1
                    L.append(seg_line([(head, col), (mid[:max(0, room)].ljust(max(0, room)), WHITE),
                                       (tail, col)], SW))
            if spacer:
                L.append(blank)
        L = L[:max(0, GH - 4)]                # always keep room for a few log lines
        L.append(rule_line("LOG", SW, "help  status"))
        return L + self.log_lines(SW, GH - len(L))

    # ---- full frame ---------------------------------------------------------
    def render(self, cols, rows, t):
        W = cols - 1
        H = rows
        clear = ""
        if (cols, rows) != self.last_size:
            self.last_size = (cols, rows)
            clear = "\x1b[2J"
        if W < 99 or H < 32:
            msg = ["Global Dominance Terminal HD",
                   "",
                   "Terminal too small: %dx%d" % (cols, rows),
                   "Please enlarge to at least 100 x 32",
                   "(160-240 x 50-80 recommended).",
                   "", "Ctrl+C to quit."]
            lines = []
            top = max(0, (H - len(msg)) // 2)
            for r in range(H):
                m = msg[r - top] if 0 <= r - top < len(msg) else ""
                lines.append(fg((255, 200, 100)) + m.center(W)[:W] + RESET)
            return "\x1b[H" + clear + "\r\n".join(lines)

        SW = max(54, min(64, W // 3))
        GW = W - SW - 1
        GH = H - 2                            # one header bar + one prompt bar
        if (self.globe is None or (self.globe.gw, self.globe.gh) != (GW, GH)
                or abs(self.globe.pitch - self.pitch) > 1e-9):
            if self.globe is not None and (self.globe.gw, self.globe.gh) == (GW, GH):
                self.globe.set_pitch(self.pitch)
            else:
                self.globe = Globe(GW, GH, self.pitch)
        lines = []
        DIM = (110, 130, 170)

        # header bar: title + key stats + clock ------------------------------------
        title = " GLOBAL DOMINANCE "
        segs = []
        for n, ch in enumerate(title):
            f = n / (len(title) - 1.0)
            segs.append((ch, (int(lerp(80, 255, f)), int(lerp(220, 100, f)),
                              int(lerp(255, 220, f)))))
        dom = int(sum(self.dev) / NREG * 100)
        segs += [("│ BANK ", DIM), (money(self.cash), (120, 255, 150)),
                 ("  REV ", DIM), (money(self.rps_cache) + "/s", (255, 220, 110)),
                 ("  EARNED ", DIM), (money(self.earned), (200, 210, 230)),
                 ("  LV ", DIM), (str(sum(self.level)), (235, 235, 160)),
                 ("  DOM ", DIM), ("%d%%" % dom, (255, 120, 210)),
                 ("  BONUS ", DIM), ("x%.1f" % self.global_mult(), (180, 135, 255))]
        if self.playtime < self.boost_until:
            segs.append(("  BOOST x%.0f %ds" % (self.boost_mult, self.boost_until - self.playtime),
                         (255, 120, 210)))
        if self.streak > 0:
            segs.append(("  STREAK %d" % self.streak, (255, 220, 110)))
        clock = time.strftime("%H:%M:%S ")
        gap = W - seg_len(segs) - len(clock)
        if gap > 0:
            segs += [(" " * gap, DIM), (clock, DIM)]
        lines.append(seg_line(segs, W, BAR_BG))

        # globe + sidebar (sidebar holds ventures, regions and the log) -----------
        grows = self.globe.render(self, t)
        side = self.sidebar(SW, GH)
        div = fg((50, 75, 120)) + "│" + RESET
        for r in range(GH):
            lines.append(grows[r] + div + side[r])

        # prompt bar ----------------------------------------------------------------
        cursor = "█" if int(t * 2) % 2 == 0 else " "
        if self.mini is not None:
            hint = {"timing": "SPACE = stop the cursor", "code": "type the highlighted code",
                    "math": "type the answer, ENTER = submit",
                    "memory": "memorise the digits, then type them",
                    "reflex": "SPACE the instant BUY appears",
                    "count": "count the symbol, ENTER = submit"}[self.mini["kind"]]
            segs = [(" WORKING> ", (255, 230, 120)), (hint, (255, 255, 255)),
                    ("   (TAB aborts)", DIM)]
        else:
            wait = self.work_ready - self.playtime
            work = ("work ready in %ds " % math.ceil(wait)) if wait > 0 else "work ready "
            right = [("↑↓ history   ", DIM),
                     (work, (255, 150, 110) if wait > 0 else (120, 255, 150))]
            segs = [(" CEO@gdthd> ", (90, 210, 255)), (self.buf, (255, 255, 255)),
                    (cursor, (255, 255, 255))]
            gap = W - seg_len(segs) - seg_len(right)
            if gap > 0:
                segs += [(" " * gap, DIM)] + right
        lines.append(seg_line(segs, W, BAR_BG))
        return "\x1b[H" + clear + "\r\n".join(lines)

    # ---- main loop ------------------------------------------------------------
    def run(self, term):
        t0 = time.perf_counter()
        last = t0
        while self.running:
            now = time.perf_counter()
            dt = min(5.0, now - last)
            last = now
            if dt > 0:
                self.fps_real = 0.9 * self.fps_real + 0.1 * (1.0 / dt)
            self.t = now - t0
            data = term.read()
            for ch in data:
                self.key(ch)
            cols, rows = term.size()
            self.update(dt)
            frame = self.render(cols, rows, now - t0)
            term.write(frame)
            spent = time.perf_counter() - now
            term.wait(1.0 / self.fps - spent)


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("gdthd.py must be run in an interactive terminal.")
        return
    print("Global Dominance Terminal HD - generating world ...")
    build_texture()
    game = Game()
    game.load(startup=True)
    try:
        with Term() as term:
            game.run(term)
    except KeyboardInterrupt:
        pass
    game.save(quiet=True)
    print("Empire saved. Thanks for playing Global Dominance Terminal HD!")


if __name__ == "__main__":
    main()
