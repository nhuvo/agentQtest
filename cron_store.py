"""
Cron Store — Lập lịch tự động chạy test skill

Table: cron_jobs
Scheduler: APScheduler (BackgroundScheduler)
"""

import json
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

DB_PATH = "qa_copilot.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_cron_db():
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cron_jobs (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                skill_id     TEXT NOT NULL,
                schedule     TEXT NOT NULL,
                cron_expr    TEXT NOT NULL,
                enabled      INTEGER NOT NULL DEFAULT 1,
                last_run_at  TEXT,
                last_status  TEXT,
                last_summary TEXT NOT NULL DEFAULT '',
                run_count    INTEGER NOT NULL DEFAULT 0,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cron_run_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cron_id     TEXT NOT NULL,
                ran_at      TEXT NOT NULL,
                status      TEXT NOT NULL,
                summary     TEXT NOT NULL DEFAULT '',
                passed      INTEGER NOT NULL DEFAULT 0,
                failed      INTEGER NOT NULL DEFAULT 0,
                blocked     INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (cron_id) REFERENCES cron_jobs(id)
            );
        """)


def _parse_natural_schedule(schedule: str) -> str:
    """
    Parse ngôn ngữ tự nhiên → cron expression.
    VD: "every day 9am" → "0 9 * * *"
        "mỗi thứ hai 8 giờ" → "0 8 * * 1"
        "mỗi giờ" → "0 * * * *"
    """
    s = schedule.lower().strip()

    # Hourly
    if re.search(r"m[oỗ]i gi[oờ]|every hour|hourly", s):
        return "0 * * * *"

    # Extract hour
    hour_m = re.search(r"(\d{1,2})\s*(?:h|giờ|am|pm|:00)?", s)
    hour = int(hour_m.group(1)) if hour_m else 9
    if "pm" in s and hour < 12:
        hour += 12

    # Day of week
    days = {
        "hai|monday|mon|thứ 2|thu 2":     "1",
        "ba|tuesday|tue|thứ 3|thu 3":     "2",
        "tư|wednesday|wed|thứ 4|thu 4":   "3",
        "năm|thursday|thu|thứ 5|thu 5":   "4",
        "sáu|friday|fri|thứ 6|thu 6":     "5",
        "bảy|saturday|sat|thứ 7|thu 7":   "6",
        "chủ nhật|sunday|sun|cn":          "0",
    }
    for pattern, dow in days.items():
        if re.search(pattern, s):
            return f"0 {hour} * * {dow}"

    # Daily
    if re.search(r"daily|every day|m[oỗ]i ng[aà]y|h[aà]ng ng[aà]y", s):
        return f"0 {hour} * * *"

    # Weekly (default Monday)
    if re.search(r"weekly|every week|m[oỗ]i tu[aầ]n", s):
        return f"0 {hour} * * 1"

    # Default: daily at extracted hour
    return f"0 {hour} * * *"


def save_cron(name: str, skill_id: str, schedule: str) -> Dict:
    init_cron_db()
    cron_expr = _parse_natural_schedule(schedule)
    now = datetime.now().isoformat()
    cron_id = re.sub(r"[^a-z0-9]", "_", name.lower())[:24] + "_" + now[-6:].replace(":", "")

    with _get_conn() as conn:
        existing = conn.execute("SELECT id FROM cron_jobs WHERE name=?", (name,)).fetchone()
        if existing:
            conn.execute("""
                UPDATE cron_jobs SET skill_id=?, schedule=?, cron_expr=?, enabled=1 WHERE name=?
            """, (skill_id, schedule, cron_expr, name))
            cron_id = existing["id"]
            action = "updated"
        else:
            conn.execute("""
                INSERT INTO cron_jobs (id, name, skill_id, schedule, cron_expr, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (cron_id, name, skill_id, schedule, cron_expr, now))
            action = "created"

    return {"action": action, "id": cron_id, "name": name,
            "schedule": schedule, "cron_expr": cron_expr}


def get_all_crons() -> List[Dict]:
    init_cron_db()
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM cron_jobs ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_cron(cron_id: str) -> Optional[Dict]:
    init_cron_db()
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM cron_jobs WHERE id=?", (cron_id,)).fetchone()
        return dict(row) if row else None


def toggle_cron(cron_id: str, enabled: bool) -> bool:
    init_cron_db()
    with _get_conn() as conn:
        return conn.execute(
            "UPDATE cron_jobs SET enabled=? WHERE id=?", (1 if enabled else 0, cron_id)
        ).rowcount > 0


def delete_cron(cron_id: str) -> bool:
    init_cron_db()
    with _get_conn() as conn:
        conn.execute("DELETE FROM cron_run_logs WHERE cron_id=?", (cron_id,))
        return conn.execute("DELETE FROM cron_jobs WHERE id=?", (cron_id,)).rowcount > 0


def log_cron_run(cron_id: str, status: str, summary: str,
                 passed: int, failed: int, blocked: int, duration_ms: int):
    init_cron_db()
    now = datetime.now().isoformat()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO cron_run_logs (cron_id, ran_at, status, summary, passed, failed, blocked, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (cron_id, now, status, summary, passed, failed, blocked, duration_ms))
        conn.execute("""
            UPDATE cron_jobs
            SET last_run_at=?, last_status=?, last_summary=?, run_count=run_count+1
            WHERE id=?
        """, (now, status, summary, cron_id))


def get_cron_logs(cron_id: str, limit: int = 20) -> List[Dict]:
    init_cron_db()
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM cron_run_logs WHERE cron_id=? ORDER BY ran_at DESC LIMIT ?
        """, (cron_id, limit)).fetchall()
        return [dict(r) for r in rows]
