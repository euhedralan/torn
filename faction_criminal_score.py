"""
Faction Criminal Scoreboard
Reads the same snapshot DB as faction_stat_score.py and generates a ranked
PNG scoreboard focused on daily criminal activity.

Score = (crime_nerve_per_day + nerve_refill_per_day×150 + cannabis_per_day×50
         + alcohol_per_day×5) × consistency_multiplier

Crime nerve weights (avg nerve cost per crime category):
  Theft      × 3.9   Cybercrime × 6.0   Fraud × 3.6
  Vandalism  × 3.0   Other      × 6.0

Thresholds (green / yellow / red):
  Nrv/d   : ≥ 400  /  200–400  /  < 200
  NRfl/d  : ≥ 1.0  /  0.5–1.0  /  < 0.5   (baseline: 1/day)
  Can/d   : ≥ 3.0  /  1.0–3.0  /  < 1.0   (baseline: 3/day)
  Alco/d  : ≥ 24   /  10–24    /  < 10    (baseline: 24/day)
  Consist : ≥ 80%  /  50–80%   /  < 50%

Generates: criminal_score.png in the same directory as the script.

Usage:
  python faction_criminal_score.py
"""

import json
import os
import sqlite3
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
API_KEY       = os.environ.get("TORN_API_KEY", "HGJzKIi0YS2qLWsD")
TORN_V1       = "https://api.torn.com"
TORN_V2       = "https://api.torn.com/v2"
REQUEST_DELAY = 0.7
HISTORY_DAYS  = 60
MIN_HOURS_BETWEEN_SNAPSHOTS = 20

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH     = os.path.join(SCRIPT_DIR, f"torn_tracker_{FACTION_ID}.db")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "criminal_score.png")

# Nerve cost weights per crime category (avg per attempt)
NERVE_WEIGHTS = {
    "theft":      3.9,
    "cybercrime": 6.0,
    "fraud":      3.6,
    "vandalism":  3.0,
    "other":      6.0,
}
NERVE_PER_REFILL  = 150   # approximate nerve bar size
NERVE_PER_CANNABIS =  50  # crime-effectiveness value per cannabis
NERVE_PER_ALCOHOL  =   5  # stat-boost value per alcohol drink
NERVE_PER_BUST     =   5  # nerve cost of a bust

MIN_INTERVALS_FOR_CONSISTENCY = 6
CONSISTENCY_BONUS = 0.30

# Thresholds: (green_min, yellow_min)
THRESH = {
    "nrv_d":   (400.0, 200.0),
    "nrfl_d":  (1.0,   0.5),
    "can_d":   (3.0,   1.0),
    "alco_d":  (24.0,  10.0),
    "bust_d":  (3.0,   1.0),
    "consist": (80.0,  50.0),
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
ACCENT     = "#f0883e"   # orange accent to distinguish from merc board
WHITE      = "#e6edf3"
DIM        = "#8b949e"
GREEN      = "#3fb950"
YELLOW     = "#d29922"
RED        = "#f85149"
GRAY       = "#484f58"


# ── Database ──────────────────────────────────────────────────────────────────

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

def _get_consistency(conn: sqlite3.Connection, member_id: str) -> tuple:
    """Returns (active_intervals, total_intervals) or (None, None)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT ms.personalstats_json "
        "FROM snapshots s "
        "JOIN member_snapshots ms ON ms.snapshot_id = s.id "
        "WHERE s.faction_id = ? AND ms.member_id = ? AND s.captured_at >= ? "
        "ORDER BY s.captured_at ASC",
        (FACTION_ID, int(member_id), cutoff),
    ).fetchall()

    if len(rows) < 2:
        return None, None

    active = 0
    total  = len(rows) - 1
    for i in range(total):
        old = json.loads(rows[i]["personalstats_json"]).get("criminaloffenses") or 0
        new = json.loads(rows[i + 1]["personalstats_json"]).get("criminaloffenses") or 0
        if new > old:
            active += 1
    return active, total


def _crime_nerve_daily(r_ps: dict, o_ps: dict, days: float) -> float:
    """Weighted nerve spent on crimes per day."""
    def d(f):
        return max(0, (r_ps.get(f) or 0) - (o_ps.get(f) or 0)) / days

    theft_d = d("theft")
    cyber_d = d("cybercrime")
    fraud_d = d("fraud")
    vand_d  = d("vandalism")
    total_d = d("criminaloffenses")
    other_d = max(0.0, total_d - theft_d - cyber_d - fraud_d - vand_d)

    return (
        theft_d * NERVE_WEIGHTS["theft"]      +
        cyber_d * NERVE_WEIGHTS["cybercrime"] +
        fraud_d * NERVE_WEIGHTS["fraud"]      +
        vand_d  * NERVE_WEIGHTS["vandalism"]  +
        other_d * NERVE_WEIGHTS["other"]
    )


def compute_criminal_stats(conn: sqlite3.Connection) -> tuple:
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

        def d(field: str) -> float:
            return max(0, (r_ps.get(field) or 0) - (o_ps.get(field) or 0)) / days

        nrv_d  = _crime_nerve_daily(r_ps, o_ps, days)
        nrfl_d = d("nerverefills")
        can_d  = d("cantaken")
        alco_d = d("alcoholused")
        bust_d = d("peoplebusted")

        raw = (
            nrv_d +
            nrfl_d * NERVE_PER_REFILL   +
            can_d  * NERVE_PER_CANNABIS +
            alco_d * NERVE_PER_ALCOHOL  +
            bust_d * NERVE_PER_BUST
        )

        active_i, total_i = _get_consistency(conn, mid)
        if total_i is not None and total_i >= MIN_INTERVALS_FOR_CONSISTENCY:
            ratio       = active_i / total_i
            consist_pct = round(ratio * 100)
            multiplier  = 1.0 + CONSISTENCY_BONUS * ratio
        else:
            consist_pct = None
            multiplier  = 1.0

        result[mid] = {
            "name":    name,
            "score":   raw * multiplier,
            "nrv_d":   nrv_d,
            "nrfl_d":  nrfl_d,
            "can_d":   can_d,
            "alco_d":  alco_d,
            "bust_d":  bust_d,
            "consist": consist_pct,
            "days":    days,
        }

    return result, period_label


# ── Image ─────────────────────────────────────────────────────────────────────

_COLS = [
    ("#",      "rank",    0.05, lambda v: str(int(v))),
    ("Name",   "name",    0.22, lambda v: v[:18] + "…" if len(v) > 18 else v),
    ("Score",  "score",   0.10, lambda v: f"{v:.0f}"),
    ("Nrv/d",  "nrv_d",   0.10, lambda v: f"{v:.0f}"),
    ("NRfl/d", "nrfl_d",  0.09, lambda v: f"{v:.2f}"),
    ("Can/d",  "can_d",   0.08, lambda v: f"{v:.2f}"),
    ("Alco/d", "alco_d",  0.08, lambda v: f"{v:.1f}"),
    ("Bust/d", "bust_d",  0.08, lambda v: f"{v:.2f}"),
    ("Consist","consist", 0.09, lambda v: f"{v:.0f}%" if v is not None else "—"),
    ("Window", "days",    0.11, lambda v: f"{v:.0f}d"),
]


def _stat_color(key: str, val) -> str:
    if key not in THRESH or val is None:
        return GRAY if val is None else WHITE
    g, y = THRESH[key]
    fval = float(val)
    if fval >= g:
        return GREEN
    if fval >= y:
        return YELLOW
    if fval > 0:
        return RED
    return GRAY


def render_image(rows: list[dict], period_label: str, output_path: str) -> None:
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
    ax.text(0.5, top - HDR_H * 0.38,
            "CRIMINAL SCOREBOARD",
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
            raw  = row.get(key)
            text = fmt(raw if raw is not None else 0)

            if key in THRESH:
                color = _stat_color(key, raw)
            elif key == "score":
                color = ACCENT
            elif key == "rank":
                color = DIM
            elif key == "name":
                color = WHITE
            else:
                color = DIM

            ax.text(x + w / 2, y + ROW_H / 2, text,
                    ha="center", va="center", fontsize=12,
                    color=color, fontfamily="monospace", zorder=2)
            x += w

        ax.plot([0, 1], [y, y], color=ROW_SEP, lw=0.4, zorder=3)

    # footer
    ax.add_patch(patches.Rectangle((0, 0), 1, FOOT_H, fc=FOOT_BG, zorder=1))
    ax.plot([0, 1], [FOOT_H, FOOT_H], color=SEP, lw=0.8, zorder=3)
    ax.text(0.5, FOOT_H * 0.72,
            f"Score = (CrimeNrv/d + NRfl/d×{NERVE_PER_REFILL} + Can/d×{NERVE_PER_CANNABIS}"
            f" + Alco/d×{NERVE_PER_ALCOHOL} + Bust/d×{NERVE_PER_BUST}) × consistency",
            ha="center", va="center", fontsize=9,
            color=DIM, fontfamily="monospace", zorder=2)
    ax.text(0.5, FOOT_H * 0.28,
            "Nrv=Nerve  NRfl=Nerve Refill  Can=Cannabis  Alco=Alcohol  Bust=Busts  Consist=Consistency",
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

    stats, period_label = compute_criminal_stats(conn)
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
    print(f"\n{'='*76}")
    print(f"  Criminal Scoreboard  |  {period_label}")
    print(f"  Score = (CrimeNrv + NRfl×{NERVE_PER_REFILL} + Can×{NERVE_PER_CANNABIS}"
          f" + Alco×{NERVE_PER_ALCOHOL} + Bust×{NERVE_PER_BUST}) × consistency  (all /day)")
    print(f"{'='*84}")
    print(f"  {'#':<4} {'Name':<22} {'Score':>7}  {'Nrv/d':>6} {'NRfl/d':>7}"
          f" {'Can/d':>6} {'Alco/d':>7} {'Bust/d':>7} {'Consist':>8}  {'Win':>5}")
    print(f"  {'-'*80}")
    for row in ranked:
        consist_str = f"{row['consist']:.0f}%" if row["consist"] is not None else "—"
        print(
            f"  {row['rank']:<4} {row['name'][:21]:<22} {row['score']:>7.0f}"
            f"  {row['nrv_d']:>6.0f} {row['nrfl_d']:>7.2f}"
            f" {row['can_d']:>6.2f} {row['alco_d']:>7.1f} {row['bust_d']:>7.2f} {consist_str:>8}"
            f"  {row['days']:>4.0f}d"
        )
    print(f"{'='*76}")
    print(f"  Generated: {now}")

    render_image(ranked, period_label, OUTPUT_PATH)


if __name__ == "__main__":
    main()
