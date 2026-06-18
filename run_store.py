"""
Run Store — lưu lịch sử chạy test cases và feedback cho AI responses.

Tables:
  tc_run_log    : mỗi lần mock-test chạy, log từng TC result + req_id liên quan
  ai_feedback   : thumbs up/down cho từng AI response trong chat
"""

import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional
from uuid import uuid4

DB_PATH = os.getenv("DB_PATH", "qa_copilot.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_run_store():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tc_run_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      TEXT NOT NULL,
                tc_id       TEXT NOT NULL,
                req_id      TEXT,
                status      TEXT NOT NULL,
                duration_ms INTEGER,
                endpoint    TEXT,
                diff        TEXT,
                run_at      TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_run_req ON tc_run_log(req_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_run_tc  ON tc_run_log(tc_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                cap        TEXT NOT NULL,
                rating     INTEGER NOT NULL,   -- 1=helpful, -1=not helpful
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_cap ON ai_feedback(cap)")


def log_tc_run(results: List[Dict], tc_lookup: Dict[str, Dict]) -> str:
    """Log một batch test run. tc_lookup: {tc_id -> tc row từ DB}."""
    run_id = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    rows = []
    for r in results:
        tc = tc_lookup.get(r.get("tc_id", ""), {})
        raw_reqs = tc.get("linked_reqs") or ""
        req_ids = [x.strip() for x in raw_reqs.split(",") if x.strip()] or [None]
        for req_id in req_ids:
            rows.append((
                run_id, r.get("tc_id",""), req_id,
                r.get("status","UNKNOWN"), r.get("duration_ms", 0),
                r.get("endpoint",""), r.get("diff",""), now,
            ))
    if rows:
        with _conn() as conn:
            conn.executemany("""
                INSERT INTO tc_run_log
                (run_id, tc_id, req_id, status, duration_ms, endpoint, diff, run_at)
                VALUES (?,?,?,?,?,?,?,?)
            """, rows)
    return run_id


def get_req_run_history(req_id: str, limit: int = 20) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM tc_run_log
            WHERE req_id=? ORDER BY run_at DESC LIMIT ?
        """, (req_id, limit)).fetchall()
    return [dict(r) for r in rows]


def get_req_run_summary(req_id: str) -> Dict:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM tc_run_log WHERE req_id=?
            GROUP BY status
        """, (req_id,)).fetchall()
        last = conn.execute("""
            SELECT run_at FROM tc_run_log WHERE req_id=?
            ORDER BY run_at DESC LIMIT 1
        """, (req_id,)).fetchone()
    counts = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    passed = counts.get("PASS", 0)
    return {
        "total": total,
        "passed": passed,
        "failed": counts.get("FAIL", 0),
        "blocked": counts.get("BLOCKED", 0),
        "pass_rate": round(passed / total * 100) if total else None,
        "last_run": last["run_at"] if last else None,
    }


def get_all_req_summaries() -> Dict[str, Dict]:
    """Returns {req_id: summary} for all reqs that have run history."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT req_id,
                   COUNT(*) as total,
                   SUM(status='PASS') as passed,
                   SUM(status='FAIL') as failed,
                   SUM(status='BLOCKED') as blocked,
                   MAX(run_at) as last_run
            FROM tc_run_log WHERE req_id IS NOT NULL
            GROUP BY req_id
        """).fetchall()
    result = {}
    for r in rows:
        total = r["total"]
        passed = r["passed"] or 0
        result[r["req_id"]] = {
            "total": total,
            "passed": passed,
            "failed": r["failed"] or 0,
            "blocked": r["blocked"] or 0,
            "pass_rate": round(passed / total * 100) if total else None,
            "last_run": r["last_run"],
        }
    return result


def save_feedback(cap: str, rating: int):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO ai_feedback (cap, rating, created_at) VALUES (?,?,?)",
            (cap, rating, datetime.utcnow().isoformat()),
        )


def get_feedback_stats() -> Dict:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT cap,
                   COUNT(*) as total,
                   SUM(rating=1) as helpful,
                   SUM(rating=-1) as not_helpful
            FROM ai_feedback GROUP BY cap ORDER BY total DESC
        """).fetchall()
        total_all = conn.execute("SELECT COUNT(*) FROM ai_feedback").fetchone()[0]
    return {
        "total": total_all,
        "by_cap": [{
            "cap": r["cap"],
            "total": r["total"],
            "helpful": r["helpful"] or 0,
            "not_helpful": r["not_helpful"] or 0,
            "helpful_rate": round((r["helpful"] or 0) / r["total"] * 100) if r["total"] else 0,
        } for r in rows],
    }


init_run_store()
