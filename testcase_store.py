"""
Test Case Store

WHAT: Parse và lưu trữ test cases từ AI response vào SQLite
WHY:  Test cases không bị mất sau mỗi session, có thể filter/export
HOW:  Parse markdown table hoặc TC_FEATURE_NNN pattern từ text AI trả về
"""

import sqlite3
import re
import os
from datetime import datetime
from typing import List, Dict, Optional, Tuple

DB_PATH = os.getenv("DB_PATH", "qa_copilot.db")


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_testcase_store():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_cases (
                tc_id        TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                feature      TEXT NOT NULL DEFAULT 'General',
                priority     TEXT NOT NULL DEFAULT 'Medium',
                type         TEXT NOT NULL DEFAULT 'Happy Path',
                preconditions TEXT,
                steps        TEXT,
                expected     TEXT,
                linked_reqs  TEXT,
                status       TEXT NOT NULL DEFAULT 'active',
                version      INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                source_cap   TEXT NOT NULL DEFAULT 'CAP-4'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tc_versions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tc_id        TEXT NOT NULL,
                version      INTEGER NOT NULL,
                title        TEXT,
                steps        TEXT,
                expected     TEXT,
                changed_at   TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_feature ON test_cases(feature)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_status  ON test_cases(status)")


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------

_PRIORITY_MAP = {
    "p0": "P0-Critical", "critical": "P0-Critical", "blocker": "P0-Critical",
    "p1": "P1-High",     "high": "P1-High",
    "p2": "P2-Medium",   "medium": "P2-Medium",     "med": "P2-Medium",
    "p3": "P3-Low",      "low": "P3-Low",
}

_TYPE_PATTERNS = [
    (r"happy\s*path|positive|thành\s*công|success",  "Happy Path"),
    (r"negative|invalid|fail|sai|lỗi|không\s*hợp",  "Negative"),
    (r"boundary|biên|giới\s*hạn|edge|min|max",       "Boundary"),
    (r"edge\s*case|corner|unusual|bất\s*thường",     "Edge Case"),
    (r"performance|perf|load|stress",                 "Performance"),
    (r"security|auth|xss|injection|bảo\s*mật",       "Security"),
]

_FEATURE_PATTERNS = [
    (r"auth|login|logout|đăng\s*(nhập|xuất)|password|mật\s*khẩu|otp|pin",  "Authentication"),
    (r"register|signup|đăng\s*ký|tài\s*khoản",                              "Registration"),
    (r"payment|thanh\s*toán|vnpay|momo|wallet|ví|điểm\s*thưởng",            "Payment"),
    (r"order|đơn\s*hàng|cart|giỏ\s*hàng|checkout",                          "Order"),
    (r"product|sản\s*phẩm|item|catalog|danh\s*mục",                         "Product"),
    (r"search|tìm\s*kiếm|filter|lọc",                                        "Search"),
    (r"user|người\s*dùng|profile|account|hồ\s*sơ",                           "User Management"),
    (r"notif|thông\s*báo|email|sms|push",                                    "Notification"),
    (r"report|báo\s*cáo|export|xuất",                                        "Report"),
    (r"api|endpoint|http|rest",                                               "API"),
]


def _detect_priority(text: str) -> str:
    low = text.lower()
    for k, v in _PRIORITY_MAP.items():
        if k in low:
            return v
    return "P2-Medium"


def _detect_type(text: str) -> str:
    low = text.lower()
    for pattern, t in _TYPE_PATTERNS:
        if re.search(pattern, low):
            return t
    return "Happy Path"


def _detect_feature(text: str) -> str:
    low = text.lower()
    for pattern, f in _FEATURE_PATTERNS:
        if re.search(pattern, low):
            return f
    return "General"


def _extract_linked_reqs(text: str) -> str:
    return ",".join(sorted(set(re.findall(r"REQ-\d+", text))))


def parse_testcases(raw_text: str) -> List[Dict]:
    """
    Parse test cases từ AI response.
    Hỗ trợ 2 format:
    1. Markdown table với cột TC_ID
    2. Heading/block với TC_FEATURE_NNN
    """
    results = []

    # --- Format 1: Markdown table ---
    # | TC_ID | Title | Priority | ... |
    table_rows = re.findall(
        r"\|\s*(TC_[A-Z0-9_]+)\s*\|([^|\n]+)\|([^|\n]*)\|([^|\n]*)\|([^|\n]*)\|([^|\n]*)\|",
        raw_text
    )
    for row in table_rows:
        tc_id, title, priority, precond, steps, expected = [c.strip() for c in row]
        title = re.sub(r"\*+", "", title).strip()
        if not title:
            continue
        results.append({
            "tc_id":        tc_id,
            "title":        title,
            "feature":      _detect_feature(tc_id + " " + title),
            "priority":     _detect_priority(priority) if priority else "P2-Medium",
            "type":         _detect_type(tc_id + " " + title),
            "preconditions": precond or "",
            "steps":        steps or "",
            "expected":     expected or "",
            "linked_reqs":  _extract_linked_reqs(raw_text),
        })

    # --- Format 2: Block với TC_FEATURE_NNN ---
    # Tìm các block bắt đầu bằng TC_xxx hoặc **TC_xxx**
    if not results:
        blocks = re.split(r"\n(?=\*{0,2}TC_[A-Z0-9_]+)", raw_text)
        for block in blocks:
            m = re.search(r"\*{0,2}(TC_[A-Z0-9_]+)\*{0,2}", block)
            if not m:
                continue
            tc_id = m.group(1)

            # Title: dòng sau TC_ID hoặc cùng dòng sau dấu :
            title_m = re.search(r"TC_[A-Z0-9_]+[:\s\|*]+([^\n|]{5,80})", block)
            title   = re.sub(r"\*+", "", title_m.group(1)).strip() if title_m else tc_id

            # Steps
            steps_m = re.search(
                r"(?:Steps?|Bước|Thực\s*hiện)[:\s*]*\n?((?:.|\n)*?)(?=\n\s*\*{0,2}(?:Expected|Kết\s*quả|Precond|Priority)|$)",
                block, re.IGNORECASE
            )
            steps = steps_m.group(1).strip() if steps_m else ""

            # Expected
            exp_m = re.search(
                r"(?:Expected|Kết\s*quả\s*mong\s*đợi)[:\s*]*\n?((?:.|\n)*?)(?=\n\s*\*{0,2}(?:Steps?|Precond|Priority|TC_)|$)",
                block, re.IGNORECASE
            )
            expected = exp_m.group(1).strip() if exp_m else ""

            # Priority
            prio_m = re.search(r"Priority[:\s*]*(P\d|High|Medium|Low|Critical)", block, re.IGNORECASE)
            priority = _detect_priority(prio_m.group(1)) if prio_m else _detect_priority(block)

            results.append({
                "tc_id":         tc_id,
                "title":         title[:200],
                "feature":       _detect_feature(tc_id + " " + title),
                "priority":      priority,
                "type":          _detect_type(tc_id + " " + title + " " + block[:200]),
                "preconditions": "",
                "steps":         steps[:2000],
                "expected":      expected[:1000],
                "linked_reqs":   _extract_linked_reqs(block),
            })

    # Dedup by tc_id
    seen = set()
    unique = []
    for tc in results:
        if tc["tc_id"] not in seen:
            seen.add(tc["tc_id"])
            unique.append(tc)
    return unique


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def save_testcases(raw_text: str, source_cap: str = "CAP-4") -> Dict:
    tcs = parse_testcases(raw_text)
    if not tcs:
        return {"added": [], "updated": [], "total_new": 0, "total_updated": 0}

    added, updated = [], []
    now = datetime.utcnow().isoformat()

    with _conn() as conn:
        for tc in tcs:
            existing = conn.execute(
                "SELECT * FROM test_cases WHERE tc_id = ?", (tc["tc_id"],)
            ).fetchone()

            if not existing:
                conn.execute("""
                    INSERT INTO test_cases
                    (tc_id, title, feature, priority, type, preconditions, steps,
                     expected, linked_reqs, status, version, created_at, updated_at, source_cap)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    tc["tc_id"], tc["title"], tc["feature"], tc["priority"], tc["type"],
                    tc["preconditions"], tc["steps"], tc["expected"], tc["linked_reqs"],
                    "active", 1, now, now, source_cap,
                ))
                added.append({"tc_id": tc["tc_id"], "title": tc["title"], "feature": tc["feature"]})
            else:
                # Chỉ update nếu có thay đổi thực sự
                changed = (
                    existing["title"]    != tc["title"]    or
                    existing["steps"]    != tc["steps"]    or
                    existing["expected"] != tc["expected"]
                )
                if changed:
                    new_ver = existing["version"] + 1
                    conn.execute("""
                        INSERT INTO tc_versions (tc_id, version, title, steps, expected, changed_at)
                        VALUES (?,?,?,?,?,?)
                    """, (existing["tc_id"], existing["version"],
                          existing["title"], existing["steps"], existing["expected"], now))
                    conn.execute("""
                        UPDATE test_cases
                        SET title=?, steps=?, expected=?, linked_reqs=?,
                            version=?, updated_at=?
                        WHERE tc_id=?
                    """, (tc["title"], tc["steps"], tc["expected"], tc["linked_reqs"],
                          new_ver, now, tc["tc_id"]))
                    updated.append({"tc_id": tc["tc_id"], "title": tc["title"]})

    return {
        "added":         added,
        "updated":       updated,
        "total_new":     len(added),
        "total_updated": len(updated),
    }


def get_all_testcases(status: str = "active") -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM test_cases WHERE status=? ORDER BY feature, tc_id",
            (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_grouped_by_feature() -> Dict[str, List[Dict]]:
    tcs = get_all_testcases()
    grouped: Dict[str, List] = {}
    for tc in tcs:
        grouped.setdefault(tc["feature"], []).append(tc)
    return grouped


def get_testcase_stats() -> Dict:
    with _conn() as conn:
        total  = conn.execute("SELECT COUNT(*) FROM test_cases WHERE status='active'").fetchone()[0]
        by_f   = conn.execute("""
            SELECT feature, COUNT(*) as cnt FROM test_cases
            WHERE status='active' GROUP BY feature ORDER BY cnt DESC
        """).fetchall()
        by_t   = conn.execute("""
            SELECT type, COUNT(*) as cnt FROM test_cases
            WHERE status='active' GROUP BY type ORDER BY cnt DESC
        """).fetchall()
        by_p   = conn.execute("""
            SELECT priority, COUNT(*) as cnt FROM test_cases
            WHERE status='active' GROUP BY priority ORDER BY cnt DESC
        """).fetchall()
    return {
        "total_active": total,
        "by_feature":   {r["feature"]: r["cnt"] for r in by_f},
        "by_type":      {r["type"]:    r["cnt"] for r in by_t},
        "by_priority":  {r["priority"]:r["cnt"] for r in by_p},
    }


def get_tc_history(tc_id: str) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tc_versions WHERE tc_id=? ORDER BY version DESC",
            (tc_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def deprecate_testcase(tc_id: str) -> bool:
    with _conn() as conn:
        n = conn.execute(
            "UPDATE test_cases SET status='deprecated', updated_at=? WHERE tc_id=?",
            (datetime.utcnow().isoformat(), tc_id)
        ).rowcount
    return n > 0


def save_tc_direct(tc: Dict, source_cap: str = "IMPORT") -> bool:
    """Lưu trực tiếp một TC từ dict (dùng cho import Excel / gen AI)."""
    now = datetime.utcnow().isoformat()
    steps = tc.get("steps") or []
    if isinstance(steps, list):
        steps = "\n".join(str(s) for s in steps)
    with _conn() as conn:
        existing = conn.execute(
            "SELECT version FROM test_cases WHERE tc_id=?", (tc["tc_id"],)
        ).fetchone()
        if existing:
            conn.execute("""
                INSERT INTO tc_versions (tc_id, version, title, steps, expected, changed_at)
                VALUES (?,?,?,?,?,?)
            """, (tc["tc_id"], existing["version"],
                  tc.get("title",""), steps, tc.get("expected",""), now))
            conn.execute("""
                UPDATE test_cases
                SET title=?, feature=?, type=?, steps=?, expected=?,
                    version=version+1, updated_at=?, source_cap=?
                WHERE tc_id=?
            """, (tc.get("title",""), tc.get("feature","Imported"),
                  tc.get("type","functional"), steps, tc.get("expected",""),
                  now, source_cap, tc["tc_id"]))
        else:
            conn.execute("""
                INSERT INTO test_cases
                (tc_id, title, feature, priority, type, preconditions, steps,
                 expected, linked_reqs, status, version, created_at, updated_at, source_cap)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tc["tc_id"], tc.get("title",""), tc.get("feature","Imported"),
                tc.get("priority","medium"), tc.get("type","functional"),
                tc.get("preconditions",""), steps,
                tc.get("expected",""), tc.get("linked_reqs",""),
                "active", 1, now, now, source_cap,
            ))
    return True


# Init on import
init_testcase_store()
