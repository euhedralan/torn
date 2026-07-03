"""
Faction Mercenary Scoreboard
Reads the same snapshot DB as faction_stat_score.py and generates a ranked
PNG scoreboard image focused on daily mercenary combat activity.

Score = XAN/d×40 + REF/d×20 + LSD/d×15 + CAN/d×8 + ATK/d×3

All metrics are expressed as daily averages over the actual snapshot window
so members with shorter history are still comparable.

Thresholds (green / yellow / red):
  XAN/d  : ≥ 2.0  /  1.0–2.0  /  < 1.0
  REF/d  : ≥ 1.0  /  0.5–1.0  /  < 0.5
  LSD/d  : ≥ 0.5  /  0.1–0.5  /  = 0
  CAN/d  : ≥ 5.0  /  2.0–5.0  /  < 2.0
  ATK/d  : ≥ 20   /  5–20     /  < 5

Generates: merc_score.png in the same directory as the script.
Shares the snapshot DB (torn_tracker_50888.db) with faction_stat_score.py —
no duplicate API calls when both run on the same day.

Usage:
  python faction_merc_score.py
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")   # headless — must be before pyplot import
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import requests

# ── Config ────────────────────────────────────────────────────────────────────

FACTION_ID   = 50888
API_KEY      = os.environ.get("TORN_API_KEY", "HGJzKIi0YS2qLWsD")
TORN_V1      = "https://api.torn.com"
TORN_V2      = "https://api.torn.com/v2"
REQUEST_DELAY = 0.7
HISTORY_DAYS  = 60
MIN_HOURS_BETWEEN_SNAPSHOTS = 20

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(SCRIPT_DIR, f"torn_tracker_{FACTION_ID}.db")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "merc_score.png")

# Score weights (applied to daily rates)
W_XAN  = 40
W_REF  = 20
W_LSD  = 15
W_CANS =  8
W_ATK  =  3

# Color thresholds: (green_min, yellow_min) — below yellow_min is red
THRESH = {
    "xan_d":  (2.0, 1.0),
    "ref_d":  (1.0, 0.5),
    "lsd_d":  (0.5, 0.1),
    "cans_d": (5.0, 2.0),
    "atk_d":  (20.0, 5.0),
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
ACCENT     = "#58a6ff"
WHITE      = "#e6edf3"
DIM        = "#8b949e"
GREEN      = "#3fb950"
YELLOW     = "#d29922"
RED        = "#f85149"
GRAY       = "#484f58"


# ── Database (shared schema with faction_stat_score.py) ──────────────────────

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            faction_id  INTEGER NOT NULL,
            captured_at TEXT    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS member_snapshots (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id        INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
            member_id          INTEGER NOT NULL,
            name               TEXT    NOT NULL,
            profile_json       TEXT    NOT NULL,
            personalstats_json TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ms_snapshot ON member_snapshots(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_snap_faction ON snapshots(faction_id, captured_at);
    """)
    conn.commit()
    return conn


def should_take_snapshot(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT captured_at FROM snapshots WHERE faction_id=? ORDER BY captured_at DESC LIMIT 1",
        (FACTION_ID,)
    ).fetchone()
    if not row:
        return True
    age_hours = (datetime.now(timezone.utc) -
                 datetime.fromisoformat(row["captured_at"])).total_seconds() / 3600
    return age_hours >= MIN_HOURS_BETWEEN_SNAPSHOTS


def purge_old_snapshots(conn: sqlite3.Connection) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
    cur = conn.execute(
        "DELETE FROM snapshots WHERE faction_id=? AND captured_at < ?",
        (FACTION_ID, cutoff)
    )
    conn.commit()
    return cur.rowcount


def get_snapshot_pair(conn: sqlite3.Connection):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
    recent = conn.execute(
        "SELECT * FROM snapshots WHERE faction_id=? ORDER BY captured_at DESC LIMIT 1",
        (FACTION_ID,)
    ).fetchone()
    oldest = conn.execute(
        "SELECT * FROM snapshots WHERE faction_id=? AND captured_at >= ?"
        " ORDER BY captured_at ASC LIMIT 1",
        (FACTION_ID, cutoff)
    ).fetchone()
    if not recent or not oldest or recent["id"] == oldest["id"]:
        return None, None
    return recent, oldest


def fetch_snap(conn: sqlite3.Connection, snapshot_id: int) -> dict:
    rows = conn.execute(
        "SELECT member_id, name, personalstats_json FROM member_snapshots WHERE snapshot_id=?",
        (snapshot_id,)
    ).fetchall()
    return {
        str(r["member_id"]): (r["name"], json.loads(r["personalstats_json"]))
        for r in rows
    }


def _oldest_snap_for_member(conn, member_id: str, exclude_id: int):
    return conn.execute(
        "SELECT s.id, s.captured_at FROM snapshots s "
        "JOIN member_snapshots ms ON ms.snapshot_id = s.id "
        "WHERE s.faction_id = ? AND ms.member_id = ? AND s.id != ? "
        "ORDER BY s.captured_at ASC LIMIT 1",
        (FACTION_ID, int(member_id), exclude_id),
    ).fetchone()


# ── API ───────────────────────────────────────────────────────────────────────

def get_faction_members() -> list:
    url = f"{TORN_V2}/faction/{FACTION_ID}?selections=members&key={API_KEY}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        sys.exit(f"API error {data['error'].get('code')}: {data['error'].get('error')}")
    members = data.get("members", [])
    if isinstance(members, dict):
        return [{"id": str(k), "name": v.get("name", "?")} for k, v in members.items()]
    return [{"id": str(m["id"]), "name": m.get("name", "?")} for m in members]


def take_snapshot(conn: sqlite3.Connection, members: list) -> None:
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        "INSERT INTO snapshots (faction_id, captured_at) VALUES (?, ?)",
        (FACTION_ID, now.isoformat())
    )
    conn.commit()
    snapshot_id = cur.lastrowid
    print(f"Taking snapshot #{snapshot_id} for {len(members)} members...")

    for i, m in enumerate(members, 1):
        mid, name = m["id"], m["name"]
        print(f"  [{i}/{len(members)}] {name}...", end=" ", flush=True)
        url = f"{TORN_V1}/user/{mid}?selections=profile,personalstats&key={API_KEY}"
        try:
            r    = requests.get(url, timeout=10)
            data = r.json()
            ps      = data.pop("personalstats", {}) or {}
            profile = data
        except Exception as e:
            print(f"ERROR ({e})")
            if i < len(members):
                time.sleep(REQUEST_DELAY)
            continue
        if not ps:
            print("SKIP")
            if i < len(members):
                time.sleep(REQUEST_DELAY)
            continue
        conn.execute(
            "INSERT INTO member_snapshots "
            "(snapshot_id, member_id, name, profile_json, personalstats_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (snapshot_id, int(mid), name, json.dumps(profile), json.dumps(ps)),
        )
        print("OK")
        if i < len(members):
            time.sleep(REQUEST_DELAY)

    conn.commit()
    snap_count = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE faction_id=?", (FACTION_ID,)
    ).fetchone()[0]
    print(f"Snapshot complete. DB now has {snap_count} snapshot(s).")


# ── Stats ─────────────────────────────────────────────────────────────────────

def calc_merc_score(xan_d, ref_d, lsd_d, cans_d, atk_d) -> float:
    return xan_d * W_XAN + ref_d * W_REF + lsd_d * W_LSD + cans_d * W_CANS + atk_d * W_ATK


def compute_merc_stats(conn: sqlite3.Connection) -> tuple[dict, str] | tuple[None, None]:
    recent, oldest = get_snapshot_pair(conn)
    if not recent:
        return None, None

    recent_data = fetch_snap(conn, recent["id"])
    oldest_data = fetch_snap(conn, oldest["id"])
    recent_ts   = datetime.fromisoformat(recent["captured_at"])
    oldest_ts   = datetime.fromisoformat(oldest["captured_at"])
    pair_days   = (recent_ts - oldest_ts).total_seconds() / 86400

    period_label = (
        f"{int(pair_days)}-day window  "
        f"({oldest_ts.strftime('%b %d')} – {recent_ts.strftime('%b %d, %Y')})"
    )

    result = {}
    for mid, (name, r_ps) in recent_data.items():
        if mid in oldest_data:
            _, o_ps = oldest_data[mid]
            days = pair_days
        else:
            row = _oldest_snap_for_member(conn, mid, recent["id"])
            if not row:
                continue
            snap = fetch_snap(conn, row["id"])
            if mid not in snap:
                continue
            _, o_ps = snap[mid]
            days = (recent_ts - datetime.fromisoformat(row["captured_at"])
                    ).total_seconds() / 86400

        if days <= 0:
            continue

        def d(field: str) -> int:
            return max(0, (r_ps.get(field) or 0) - (o_ps.get(field) or 0))

        xan_d  = d("xantaken")        / days
        ref_d  = d("refills")         / days
        lsd_d  = d("lsdtaken")        / days
        cans_d = d("energydrinkused") / days
        atk_d  = (d("attackswon") + d("attackslost") + d("attacksdraw")) / days

        result[mid] = {
            "name":   name,
            "score":  calc_merc_score(xan_d, ref_d, lsd_d, cans_d, atk_d),
            "xan_d":  xan_d,
            "ref_d":  ref_d,
            "lsd_d":  lsd_d,
            "cans_d": cans_d,
            "atk_d":  atk_d,
            "days":   days,
        }

    return result, period_label


# ── Image ─────────────────────────────────────────────────────────────────────

_COLS = [
    # (header, key, width_frac, fmt_fn)
    ("#",      "rank",   0.05, lambda v: str(int(v))),
    ("Name",   "name",   0.24, lambda v: v[:20] + "…" if len(v) > 20 else v),
    ("Score",  "score",  0.10, lambda v: f"{v:.1f}"),
    ("XAN/d",  "xan_d",  0.10, lambda v: f"{v:.2f}"),
    ("REF/d",  "ref_d",  0.10, lambda v: f"{v:.2f}"),
    ("LSD/d",  "lsd_d",  0.09, lambda v: f"{v:.2f}"),
    ("CAN/d",  "cans_d", 0.09, lambda v: f"{v:.1f}"),
    ("ATK/d",  "atk_d",  0.10, lambda v: f"{v:.1f}"),
    ("Window", "days",   0.13, lambda v: f"{v:.0f}d"),
]


def _stat_color(key: str, val: float) -> str:
    if key not in THRESH:
        return WHITE
    g, y = THRESH[key]
    if val >= g:
        return GREEN
    if val >= y:
        return YELLOW
    if val > 0:
        return RED
    return GRAY


def render_image(rows: list[dict], period_label: str, output_path: str) -> None:
    n = len(rows)

    HDR_H  = 0.85   # title bar
    COL_H  = 0.50   # column header row
    ROW_H  = 0.48   # data row
    FOOT_H = 0.80   # formula footer (two lines)
    FIG_W  = 9.5
    FIG_H  = HDR_H + COL_H + n * ROW_H + FOOT_H

    fig = plt.figure(figsize=(FIG_W, FIG_H))
    fig.patch.set_facecolor(BG)
    ax  = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, FIG_H)
    ax.axis("off")

    # title header ─────────────────────────────────────────────────────────────
    top = FIG_H
    ax.add_patch(patches.Rectangle((0, top - HDR_H), 1, HDR_H, fc=HDR_BG, zorder=1))
    ax.text(0.5, top - HDR_H * 0.38,
            "MERCENARY SCOREBOARD",
            ha="center", va="center", fontsize=16, fontweight="bold",
            color=ACCENT, fontfamily="monospace", zorder=2)
    ax.text(0.5, top - HDR_H * 0.75, period_label,
            ha="center", va="center", fontsize=11,
            color=DIM, fontfamily="monospace", zorder=2)
    ax.plot([0, 1], [top - HDR_H, top - HDR_H], color=SEP, lw=1.0, zorder=3)

    # column headers ───────────────────────────────────────────────────────────
    top -= HDR_H
    ax.add_patch(patches.Rectangle((0, top - COL_H), 1, COL_H, fc=COL_HDR_BG, zorder=1))
    x = 0.0
    for label, key, w, _ in _COLS:
        ax.text(x + w / 2, top - COL_H / 2, label,
                ha="center", va="center", fontsize=12, fontweight="bold",
                color=WHITE, fontfamily="monospace", zorder=2)
        x += w
    ax.plot([0, 1], [top - COL_H, top - COL_H], color=SEP, lw=0.8, zorder=3)

    # data rows ────────────────────────────────────────────────────────────────
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
                color = _stat_color(key, float(raw))
            elif key == "score":
                color = ACCENT
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

    # footer ───────────────────────────────────────────────────────────────────
    ax.add_patch(patches.Rectangle((0, 0), 1, FOOT_H, fc=FOOT_BG, zorder=1))
    ax.plot([0, 1], [FOOT_H, FOOT_H], color=SEP, lw=0.8, zorder=3)
    ax.text(0.5, FOOT_H * 0.72,
            f"Score = XAN/d×{W_XAN} + REF/d×{W_REF} + LSD/d×{W_LSD} + CAN/d×{W_CANS} + ATK/d×{W_ATK}",
            ha="center", va="center", fontsize=9,
            color=DIM, fontfamily="monospace", zorder=2)
    ax.text(0.5, FOOT_H * 0.28,
            "XAN=Xanax  REF=Energy Refill  LSD=LSD  CAN=Cans  ATK=Attacks",
            ha="center", va="center", fontsize=9,
            color=DIM, fontfamily="monospace", zorder=2)

    plt.savefig(output_path, dpi=150, bbox_inches="tight",
                facecolor=BG, pad_inches=0.05)
    print(f"Image saved → {output_path}")
    plt.close(fig)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    conn = open_db()

    print("Fetching current faction roster...")
    members     = get_faction_members()
    current_ids = {m["id"] for m in members}
    print(f"  {len(members)} members in faction.")

    if should_take_snapshot(conn):
        take_snapshot(conn, members)
        purged = purge_old_snapshots(conn)
        if purged:
            print(f"  Purged {purged} snapshot(s) older than {HISTORY_DAYS} days.")
    else:
        print("  Snapshot taken recently — skipping fetch.")

    stats, period_label = compute_merc_stats(conn)
    conn.close()

    if stats is None:
        print("\nOnly 1 snapshot in DB — run again tomorrow for delta scores.")
        return

    stats  = {mid: s for mid, s in stats.items() if mid in current_ids}
    ranked = sorted(stats.values(), key=lambda s: s["score"], reverse=True)
    for i, row in enumerate(ranked, 1):
        row["rank"] = i

    # Terminal summary
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*72}")
    print(f"  Mercenary Scoreboard  |  {period_label}")
    print(f"  Score = XAN×{W_XAN} + REF×{W_REF} + LSD×{W_LSD} + CAN×{W_CANS} + ATK×{W_ATK}  (all /day)")
    print(f"{'='*72}")
    print(f"  {'#':<4} {'Name':<22} {'Score':>7}  {'XAN/d':>6} {'REF/d':>6}"
          f" {'LSD/d':>6} {'CAN/d':>6} {'ATK/d':>6}  {'Win':>5}")
    print(f"  {'-'*68}")
    for row in ranked:
        print(
            f"  {row['rank']:<4} {row['name'][:21]:<22} {row['score']:>7.1f}"
            f"  {row['xan_d']:>6.2f} {row['ref_d']:>6.2f}"
            f" {row['lsd_d']:>6.2f} {row['cans_d']:>6.1f} {row['atk_d']:>6.1f}"
            f"  {row['days']:>4.0f}d"
        )
    print(f"{'='*72}")
    print(f"  Generated: {now}")

    render_image(ranked, period_label, OUTPUT_PATH)


if __name__ == "__main__":
    main()
