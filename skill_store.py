"""
Skill Store — Test skill templates có thể tái dùng

A skill = named collection of mock API test cases (queue snapshot)
Lưu vào SQLite, load lại với 1 click trong UI.
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


def init_skills_db():
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS test_skills (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                category    TEXT NOT NULL DEFAULT 'General',
                test_cases  TEXT NOT NULL DEFAULT '[]',
                tags        TEXT NOT NULL DEFAULT '[]',
                created_at  TEXT NOT NULL,
                used_count  INTEGER NOT NULL DEFAULT 0
            );
        """)


def _make_id(name: str) -> str:
    base = re.sub(r"[^a-z0-9]", "_", name.lower())[:32].strip("_")
    return base or "skill"


def save_skill(name: str, test_cases: List[Dict],
               description: str = "", category: str = "General",
               tags: Optional[List[str]] = None) -> Dict:
    init_skills_db()
    tags = tags or []
    now = datetime.now().isoformat()
    skill_id = _make_id(name)

    with _get_conn() as conn:
        existing = conn.execute("SELECT id FROM test_skills WHERE name=?", (name,)).fetchone()
        if existing:
            conn.execute("""
                UPDATE test_skills
                SET description=?, category=?, test_cases=?, tags=?
                WHERE name=?
            """, (description, category, json.dumps(test_cases), json.dumps(tags), name))
            action = "updated"
            skill_id = existing["id"]
        else:
            conn.execute("""
                INSERT INTO test_skills (id, name, description, category, test_cases, tags, created_at, used_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """, (skill_id, name, description, category,
                  json.dumps(test_cases), json.dumps(tags), now))
            action = "created"

    return {"action": action, "id": skill_id, "name": name, "tc_count": len(test_cases)}


def get_all_skills() -> List[Dict]:
    init_skills_db()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM test_skills ORDER BY used_count DESC, created_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_skill(skill_id: str) -> Optional[Dict]:
    init_skills_db()
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM test_skills WHERE id=?", (skill_id,)).fetchone()
        if not row:
            return None
        d = _row_to_dict(row)
        conn.execute("UPDATE test_skills SET used_count = used_count + 1 WHERE id=?", (skill_id,))
        return d


def delete_skill(skill_id: str) -> bool:
    init_skills_db()
    with _get_conn() as conn:
        return conn.execute("DELETE FROM test_skills WHERE id=?", (skill_id,)).rowcount > 0


def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    d["test_cases"] = json.loads(d.get("test_cases", "[]"))
    d["tags"] = json.loads(d.get("tags", "[]"))
    return d
