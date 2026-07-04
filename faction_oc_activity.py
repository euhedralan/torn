"""
Faction OC Activity Report
Fetches the last 14 days of completed organized crimes and reports how many
hours each current faction member spent in the OC planning phase.

  Hours  = sum of (executed_at - joined_at) per crime slot per member
  Util%  = Hours / (HISTORY_DAYS × 24) × 100

A member who was in an OC every possible moment would score 100% utilization.

Generates: oc_activity.png in the same directory as the script.

Usage:
  python faction_oc_activity.py
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import requests

# ── Config ────────────────────────────────────────────────────────────────────

FACTION_ID    = 50888
API_KEY       = os.environ.get("TORN_API_KEY")
if not API_KEY:
    sys.exit("Set TORN_API_KEY environment variable before running.")

TORN_V2       = "https://api.torn.com/v2"
REQUEST_DELAY = 0.5
HISTORY_DAYS  = 14
PAGE_SIZE     = 100

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "oc_activity.png")

MAX_HOURS = HISTORY_DAYS * 24   # theoretical max hours in OC per member

# Color thresholds: (green_min, yellow_min)
THRESH = {
    "util":  (60.0, 30.0),
    "hours": (MAX_HOURS * 0.60, MAX_HOURS * 0.30),
}

# ── Image palette ─────────────────────────────────────────────────────────────

BG         = "#0d1117"
HDR_BG     = "#161b22"
COL_HDR_BG = "#1f2937"
ROW_EVEN   = "#0d1117"
ROW_ODD    = "#0f1923"
FOOT_BG    = "#161b22"
SEP        = "#30363d"
ROW_SEP    = "#21262d"
ACCENT     = "#a371f7"
WHITE      = "#e6edf3"
DIM        = "#8b949e"
GREEN      = "#3fb950"
YELLOW     = "#d29922"
RED        = "#f85149"
GRAY       = "#484f58"


# ── API ───────────────────────────────────────────────────────────────────────

def get_faction_members() -> dict:
    url = f"{TORN_V2}/faction/{FACTION_ID}?selections=members&key={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        sys.exit(f"API error {data['error'].get('code')}: {data['error'].get('error')}")
    members = data.get("members", [])
    if isinstance(members, dict):
        return {str(k): v.get("name", "?") for k, v in members.items()}
    return {str(m["id"]): m.get("name", "?") for m in members}


def get_completed_crimes(since_ts: int, until_ts: int) -> list:
    crimes = []
    offset = 0
    while True:
        url = (
            f"{TORN_V2}/faction/crimes"
            f"?cat=completed&filter=executed_at&sort=DESC"
            f"&from={since_ts}&to={until_ts}"
            f"&limit={PAGE_SIZE}&offset={offset}"
            f"&key={API_KEY}"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            sys.exit(f"Torn API error {data['error'].get('code')}: {data['error'].get('error')}")
        page = data.get("crimes", [])
        if not page:
            break
        crimes.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)
    return crimes


# ── Data helpers ──────────────────────────────────────────────────────────────

def get_slots(crime: dict) -> list:
    """Returns list of {id: str, hours: float} — one entry per filled slot."""
    executed = crime.get("executed_at") or 0
    result   = []
    for slot in (crime.get("slots") or []):
        user = slot.get("user")
        if not user:
            continue
        mid = user.get("id")
        if not mid:
            continue
        joined = user.get("joined_at") or executed
        hours  = max(0.0, (executed - joined) / 3600.0) if executed > joined else 0.0
        result.append({"id": str(mid), "hours": hours})
    return result


# ── Stats ─────────────────────────────────────────────────────────────────────

def compute_stats(crimes: list, member_names: dict) -> list:
    hours_map = {mid: 0.0 for mid in member_names}

    for crime in crimes:
        for slot in get_slots(crime):
            mid = slot["id"]
            if mid in hours_map:
                hours_map[mid] += slot["hours"]

    rows = []
    for mid, name in member_names.items():
        h = hours_map[mid]
        rows.append({
            "name":  name,
            "hours": h,
            "util":  (h / MAX_HOURS) * 100,
        })

    return sorted(rows, key=lambda r: r["hours"], reverse=True)


# ── Image ─────────────────────────────────────────────────────────────────────

_COLS = [
    ("#",     "rank",  0.08, lambda v: str(int(v))),
    ("Name",  "name",  0.52, lambda v: v[:36] + "…" if len(v) > 36 else v),
    ("Hours", "hours", 0.20, lambda v: f"{v:.1f}h"),
    ("Util%", "util",  0.20, lambda v: f"{v:.1f}%"),
]


def _stat_color(key: str, val) -> str:
    if key not in THRESH:
        return WHITE
    g, y = THRESH[key]
    fval = float(val)
    if fval >= g:
        return GREEN
    if fval >= y:
        return YELLOW
    if fval > 0:
        return RED
    return GRAY


def render_image(rows: list, period_label: str, output_path: str) -> None:
    n = len(rows)

    HDR_H  = 0.85
    COL_H  = 0.50
    ROW_H  = 0.48
    FOOT_H = 0.80
    FIG_W  = 9.5
    FIG_H  = HDR_H + COL_H + n * ROW_H + FOOT_H

    fig = plt.figure(figsize=(FIG_W, FIG_H))
    fig.patch.set_facecolor(BG)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, FIG_H)
    ax.axis("off")

    # title
    top = FIG_H
    ax.add_patch(patches.Rectangle((0, top - HDR_H), 1, HDR_H, fc=HDR_BG, zorder=1))
    ax.text(0.5, top - HDR_H * 0.38, "OC ACTIVITY SCOREBOARD",
            ha="center", va="center", fontsize=16, fontweight="bold",
            color=ACCENT, fontfamily="monospace", zorder=2)
    ax.text(0.5, top - HDR_H * 0.75, period_label,
            ha="center", va="center", fontsize=11,
            color=DIM, fontfamily="monospace", zorder=2)
    ax.plot([0, 1], [top - HDR_H, top - HDR_H], color=SEP, lw=1.0, zorder=3)

    # column headers
    top -= HDR_H
    ax.add_patch(patches.Rectangle((0, top - COL_H), 1, COL_H, fc=COL_HDR_BG, zorder=1))
    x = 0.0
    for label, key, w, _ in _COLS:
        ax.text(x + w / 2, top - COL_H / 2, label,
                ha="center", va="center", fontsize=12, fontweight="bold",
                color=WHITE, fontfamily="monospace", zorder=2)
        x += w
    ax.plot([0, 1], [top - COL_H, top - COL_H], color=SEP, lw=0.8, zorder=3)

    # data rows
    top -= COL_H
    for i, row in enumerate(rows):
        y  = top - (i + 1) * ROW_H
        bg = ROW_ODD if i % 2 else ROW_EVEN
        ax.add_patch(patches.Rectangle((0, y), 1, ROW_H, fc=bg, zorder=1))

        x = 0.0
        for label, key, w, fmt in _COLS:
            raw = row.get(key, 0)
            txt = fmt(raw)
            if key in THRESH:
                color = _stat_color(key, raw)
            elif key == "rank":
                color = DIM
            elif key == "name":
                color = WHITE
            else:
                color = DIM
            ax.text(x + w / 2, y + ROW_H / 2, txt,
                    ha="center", va="center", fontsize=12,
                    color=color, fontfamily="monospace", zorder=2)
            x += w

        ax.plot([0, 1], [y, y], color=ROW_SEP, lw=0.4, zorder=3)

    # footer
    ax.add_patch(patches.Rectangle((0, 0), 1, FOOT_H, fc=FOOT_BG, zorder=1))
    ax.plot([0, 1], [FOOT_H, FOOT_H], color=SEP, lw=0.8, zorder=3)
    ax.text(0.5, FOOT_H * 0.72,
            f"Hours = time in OC planning phase  |  Max = {MAX_HOURS}h ({HISTORY_DAYS} days)  |  Util% = Hours / Max",
            ha="center", va="center", fontsize=9,
            color=DIM, fontfamily="monospace", zorder=2)
    ax.text(0.5, FOOT_H * 0.28,
            "Hours counted from each member's join time to execution  |  Green ≥60%  Yellow ≥30%",
            ha="center", va="center", fontsize=9,
            color=DIM, fontfamily="monospace", zorder=2)

    plt.savefig(output_path, dpi=150, bbox_inches="tight",
                facecolor=BG, pad_inches=0.05)
    print(f"Image saved → {output_path}")
    plt.close(fig)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    now_ts   = int(datetime.now(timezone.utc).timestamp())
    since_ts = now_ts - HISTORY_DAYS * 86400
    since_dt = datetime.fromtimestamp(since_ts, tz=timezone.utc)
    now_dt   = datetime.fromtimestamp(now_ts,   tz=timezone.utc)

    period_label = (
        f"{HISTORY_DAYS}-day window  "
        f"({since_dt.strftime('%b %d')} – {now_dt.strftime('%b %d, %Y')})"
    )

    print("Fetching current faction roster...")
    member_names = get_faction_members()
    print(f"  {len(member_names)} members.")

    print(f"Fetching completed OCs  {since_dt.strftime('%b %d')} → {now_dt.strftime('%b %d, %Y')} ...")
    crimes = get_completed_crimes(since_ts, now_ts)
    print(f"  {len(crimes)} crime(s) returned.")

    if not crimes:
        print("No completed crimes found in the last 14 days.")
        return

    rows = compute_stats(crimes, member_names)
    for i, row in enumerate(rows, 1):
        row["rank"] = i

    now_str = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*54}")
    print(f"  OC Activity Report  |  {period_label}")
    print(f"  Max possible: {MAX_HOURS}h per member")
    print(f"{'='*54}")
    print(f"  {'#':<4} {'Name':<36} {'Hours':>7} {'Util%':>7}")
    print(f"  {'-'*50}")
    for row in rows:
        print(
            f"  {row['rank']:<4} {row['name'][:35]:<36}"
            f" {row['hours']:>6.1f}h {row['util']:>6.1f}%"
        )
    print(f"{'='*54}")
    print(f"  Generated: {now_str}")

    render_image(rows, period_label, OUTPUT_PATH)


if __name__ == "__main__":
    main()
