"""
Requirement Store

WHAT: Persistent storage for requirements using SQLite
WHY:  Chat history is volatile; requirements need to survive session resets
HOW:  SQLite DB with versioning, feature grouping, and diff support
"""

import re
import json
import sqlite3
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict


DB_PATH = "qa_copilot.db"

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Requirement:
    req_id: str           # REQ-001
    title: str            # Short title
    description: str      # Full text
    feature: str          # Grouped feature/module name
    source: str           # Raw input text (first 300 chars)
    version: int          # Version number (increments on update)
    ambiguities: List[str]
    status: str           # active | deprecated | updated
    created_at: str
    updated_at: str

    def to_dict(self):
        return asdict(self)


@dataclass
class RequirementVersion:
    req_id: str
    version: int
    description: str
    changed_at: str
    change_summary: str   # what changed


# ---------------------------------------------------------------------------
# Feature detection — tự động gom nhóm từ keywords
# ---------------------------------------------------------------------------

_FEATURE_PATTERNS = [
    # Auth phải check trước Registration/User vì "đăng nhập" vs "đăng ký"
    (r"đăng nhập|login|sign.?in|auth(?:entication)?|password|mật khẩu|otp|xác thực|verify|token|logout|đăng xuất", "Authentication"),
    (r"đăng ký|register|sign.?up|tạo tài khoản|create.?account",         "Registration"),
    (r"thanh toán|payment|checkout|billing|invoice|hóa đơn|vnpay|momo|visa|mastercard", "Payment"),
    (r"giỏ hàng|cart|basket|order|đơn hàng",                             "Order"),
    (r"sản phẩm|product|catalog|danh mục|category",                      "Product"),
    (r"tìm kiếm|search|filter|lọc|sort",                                 "Search"),
    (r"user|người dùng|profile|account|tài khoản",                       "User Management"),
    (r"report|báo cáo|dashboard|thống kê|analytics",                     "Reporting"),
    # "email" chỉ classify Notification khi đi kèm từ gửi/thông báo/notify
    (r"notification|thông báo|(?:gửi|send).{0,20}(?:email|sms)|push.?notif|alert", "Notification"),
    (r"upload|file|attachment|tệp|image|hình ảnh",                       "File Management"),
    (r"api|endpoint|webhook|integration",                                 "API / Integration"),
    (r"security|bảo mật|permission|phân quyền|role|access",              "Security"),
    (r"performance|tốc độ|latency|cache|caching",                        "Performance"),
]

def _clean_content(text: str) -> str:
    """Strip markdown artifacts và REQ-prefix noise trước khi lưu vào DB."""
    # Bỏ REQ-XXX: prefix (AI đôi khi include trong explanatory text)
    text = re.sub(r"^\*{0,3}REQ-\d+\*{0,3}\s*[:\-]\s*", "", text.strip())
    # Bỏ bold/italic markers
    text = re.sub(r"\*{1,3}", "", text)
    # Bỏ markdown heading markers
    text = re.sub(r"^#+\s*", "", text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _word_overlap(a: str, b: str) -> float:
    """Tỷ lệ overlap từ giữa hai chuỗi (Jaccard-like, case-insensitive)."""
    wa = set(re.findall(r"\w+", a.lower()))
    wb = set(re.findall(r"\w+", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _generate_acceptance_criteria(description: str) -> List[str]:
    """
    Tạo Given-When-Then AC từ requirement text (rule-based, 0 token cost).
    Returns list of AC strings.
    """
    d = description.strip()
    t = d.lower()
    acs = []

    # Pattern: "X có thể Y" / "X can Y"
    m = re.search(r"(?:người dùng|user|hệ thống|system)\s+(?:có thể|can|phải|must|should)\s+(.+)", t)
    action = m.group(1).rstrip(".") if m else d[:60]

    # Positive AC
    if re.search(r"đăng nhập|login|sign.?in", t):
        acs += [
            f"Given người dùng ở trang đăng nhập",
            f"When nhập thông tin hợp lệ và submit",
            f"Then {action} thành công và redirect về trang chủ",
        ]
    elif re.search(r"đăng ký|register|sign.?up", t):
        acs += [
            f"Given người dùng chưa có tài khoản",
            f"When điền đầy đủ thông tin hợp lệ và submit",
            f"Then tài khoản được tạo, email xác thực được gửi",
        ]
    elif re.search(r"thanh toán|payment|checkout", t):
        acs += [
            f"Given người dùng có đơn hàng cần thanh toán",
            f"When chọn phương thức và xác nhận thanh toán",
            f"Then giao dịch thành công, nhận xác nhận",
        ]
    elif re.search(r"tìm kiếm|search", t):
        acs += [
            f"Given người dùng ở trang tìm kiếm",
            f"When nhập từ khóa và submit",
            f"Then hiển thị kết quả phù hợp trong vòng 2s",
        ]
    else:
        # Generic Given-When-Then
        acs += [
            f"Given điều kiện tiên quyết được thỏa mãn",
            f"When người dùng thực hiện hành động liên quan",
            f"Then hệ thống {action}",
        ]

    # Negative AC (luôn thêm)
    acs.append(f"And nếu điều kiện không hợp lệ, hệ thống hiển thị thông báo lỗi rõ ràng")

    # Performance AC nếu có số đo
    perf_m = re.search(r"(\d+)\s*(?:giây|second|ms|phút|minute)", t)
    if perf_m:
        acs.append(f"And thời gian phản hồi ≤ {perf_m.group(0)}")

    return acs


def _normalize_vi(text: str) -> str:
    """Bỏ dấu tiếng Việt, lowercase — để keyword không dấu vẫn match text có dấu."""
    text = text.replace('đ', 'd').replace('Đ', 'D')
    nfd = unicodedata.normalize('NFD', text)
    stripped = ''.join(c for c in nfd if not unicodedata.combining(c))
    return stripped.lower()


def _classify_to_user_groups(text: str, groups: List[Dict]) -> Optional[str]:
    """
    Phân loại requirement vào user-defined group dựa trên keyword overlap.
    Returns tên group nếu match (score > threshold), None nếu không match.
    """
    if not groups:
        return None
    # Normalize text một lần (bỏ dấu, lower) để match cả keyword có dấu lẫn không dấu
    text_norm = _normalize_vi(text)
    best_group: Optional[str] = None
    best_score = 0        # absolute hit score
    best_total_kws = 0    # tie-break: group có nhiều keyword hơn = định nghĩa cụ thể hơn

    for g in groups:
        name_words = re.findall(r"\w+", _normalize_vi(g["name"]))
        explicit_kws = g.get("keywords", [])
        keywords = [w for w in name_words if len(w) > 2]
        keyword_phrases = [_normalize_vi(k).strip() for k in explicit_kws if k.strip()]
        if not keywords and not keyword_phrases:
            continue

        # Absolute hits — không normalize để tránh group ít keyword thắng không công bằng
        # phrase match = 3 điểm, name word match = 1 điểm
        phrase_hits = sum(3 for kw in keyword_phrases if kw in text_norm)
        name_hits   = sum(1 for w in keywords if w in text_norm)
        score = phrase_hits + name_hits
        total_kws = len(keyword_phrases) + len(keywords)

        # Tie-break: nếu bằng điểm → group có nhiều keyword hơn thắng (định nghĩa cụ thể hơn)
        if score > best_score or (score == best_score and score > 0 and total_kws > best_total_kws):
            best_score    = score
            best_group    = g["name"]
            best_total_kws = total_kws

    # Tối thiểu 3 điểm = ít nhất 1 phrase match (3pt) hoặc 3 name-word match
    return best_group if best_score >= 3 else None


def detect_feature(text: str, user_groups: Optional[List[Dict]] = None) -> str:
    """Phân loại feature. Ưu tiên user-defined groups, fallback sang _FEATURE_PATTERNS."""
    if user_groups:
        result = _classify_to_user_groups(text, user_groups)
        if result:
            return result
        return "Unassigned"
    # Fallback: built-in regex patterns
    t = text.lower()
    for pattern, feature in _FEATURE_PATTERNS:
        if re.search(pattern, t, re.I):
            return feature
    return "General"


# ---------------------------------------------------------------------------
# Parser — extract requirements từ free-text
# ---------------------------------------------------------------------------

def parse_requirements(raw_text: str, user_groups: Optional[List[Dict]] = None) -> List[Dict]:
    """
    Parse raw requirement text thành list of requirement dicts.
    Hỗ trợ nhiều format:
      - Numbered list: "1. User có thể..."
      - Bullet: "- User có thể..."
      - Inline REQ-ID: "REQ-001: User có thể..."
      - Paragraph: tách theo dòng trống
    """
    results = []

    # Thử tách theo numbered / bullet list trước
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    items = []

    for line in lines:
        # Đã có REQ-ID
        m = re.match(r"^(REQ-\d+)[:\s]+(.+)$", line, re.I)
        if m:
            items.append(m.group(2).strip())
            continue
        # Numbered: "1." "1)" "1:"
        m = re.match(r"^\d+[.):\s]+(.+)$", line)
        if m:
            items.append(m.group(1).strip())
            continue
        # Bullet: "-" "*" "•"
        m = re.match(r"^[-*•]\s+(.+)$", line)
        if m:
            items.append(m.group(1).strip())
            continue

    # Nếu không tách được → coi mỗi câu là 1 requirement
    if not items:
        items = [s.strip() for s in re.split(r"[.。]\s+", raw_text) if len(s.strip()) > 20]

    # Nếu vẫn không có gì → cả đoạn là 1 requirement
    if not items:
        items = [raw_text.strip()]

    for item in items:
        if not item:
            continue
        item = _clean_content(item)
        if len(item) < 10:
            continue
        title = item[:60] + ("..." if len(item) > 60 else "")
        results.append({
            "title":       title,
            "description": item,
            "feature":     detect_feature(item, user_groups),
            "source":      raw_text[:300],
        })

    return results


def detect_ambiguities(text: str) -> List[str]:
    """Flag các dấu hiệu requirement mơ hồ."""
    flags = []
    t = text.lower()
    if re.search(r"\bnhanh\b|\bnhanh chóng\b|fast|quickly|soon", t):
        flags.append("Mơ hồ về hiệu năng: 'nhanh' cần định nghĩa cụ thể (VD: < 2s)")
    if re.search(r"\bdễ\b|easy|simple|đơn giản", t):
        flags.append("Mơ hồ về UX: 'dễ dùng' không đo được — cần acceptance criteria")
    if re.search(r"\bthường xuyên\b|often|regularly|periodically", t):
        flags.append("Mơ hồ về tần suất: cần chỉ rõ interval cụ thể")
    if re.search(r"\bhoặc\b|or\b", t):
        flags.append("Có thể mâu thuẫn logic: dùng 'hoặc' — cần làm rõ điều kiện")
    if re.search(r"\bphải\b|\bcần\b|must|shall", t) and len(text) < 30:
        flags.append("Requirement quá ngắn — thiếu context hoặc acceptance criteria")
    if not re.search(r"khi|khi nào|when|if|nếu|trong trường hợp", t) and len(text) > 50:
        flags.append("Không có trigger/condition rõ ràng (khi nào thì feature này kích hoạt?)")
    return flags


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Tạo tables nếu chưa có, migrate schema nếu cần."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS requirements (
                req_id               TEXT PRIMARY KEY,
                title                TEXT NOT NULL,
                description          TEXT NOT NULL,
                feature              TEXT NOT NULL DEFAULT 'General',
                source               TEXT,
                version              INTEGER NOT NULL DEFAULT 1,
                ambiguities          TEXT NOT NULL DEFAULT '[]',
                status               TEXT NOT NULL DEFAULT 'active',
                acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS requirement_versions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                req_id         TEXT NOT NULL,
                version        INTEGER NOT NULL,
                description    TEXT NOT NULL,
                changed_at     TEXT NOT NULL,
                change_summary TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (req_id) REFERENCES requirements(req_id)
            );

            CREATE TABLE IF NOT EXISTS feature_groups (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                keywords    TEXT NOT NULL DEFAULT '[]',
                color       TEXT NOT NULL DEFAULT '',
                sort_order  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_req_feature ON requirements(feature);
            CREATE INDEX IF NOT EXISTS idx_req_status  ON requirements(status);
        """)
        # Migrate: thêm cột acceptance_criteria nếu chưa có (cho DB cũ)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(requirements)").fetchall()}
        if "acceptance_criteria" not in cols:
            conn.execute("ALTER TABLE requirements ADD COLUMN acceptance_criteria TEXT NOT NULL DEFAULT '[]'")


# ---------------------------------------------------------------------------
# Feature Groups — CRUD
# ---------------------------------------------------------------------------

def get_all_feature_groups() -> List[Dict]:
    """Lấy tất cả user-defined feature groups, sắp xếp theo sort_order."""
    init_db()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM feature_groups ORDER BY sort_order, name"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["keywords"] = json.loads(d.get("keywords", "[]"))
            result.append(d)
        return result


def save_feature_group(name: str, description: str = "", keywords: Optional[List[str]] = None,
                       color: str = "", sort_order: int = 0) -> Dict:
    """Tạo hoặc cập nhật một feature group."""
    init_db()
    kws = keywords or []
    now = datetime.now().isoformat()
    group_id = re.sub(r"[^a-z0-9]", "_", name.lower())

    with _get_conn() as conn:
        existing = conn.execute("SELECT id FROM feature_groups WHERE name=?", (name,)).fetchone()
        if existing:
            conn.execute("""
                UPDATE feature_groups
                SET description=?, keywords=?, color=?, sort_order=?
                WHERE name=?
            """, (description, json.dumps(kws), color, sort_order, name))
            action = "updated"
        else:
            conn.execute("""
                INSERT INTO feature_groups (id, name, description, keywords, color, sort_order, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (group_id, name, description, json.dumps(kws), color, sort_order, now))
            action = "created"

    return {"action": action, "name": name, "description": description, "keywords": kws}


def delete_feature_group(name: str) -> bool:
    """Xóa một feature group. Requirements trong group chuyển về 'Unassigned'."""
    init_db()
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM feature_groups WHERE name=?", (name,))
        if cur.rowcount > 0:
            # Chuyển requirements trong group này về Unassigned
            conn.execute(
                "UPDATE requirements SET feature='Unassigned' WHERE feature=?", (name,)
            )
            return True
        return False


def reclassify_all_requirements() -> Dict:
    """
    Chạy lại phân loại feature cho tất cả requirements active.
    Dùng user groups nếu có, fallback về _FEATURE_PATTERNS.
    Returns: { reclassified: N, changes: [{req_id, old_feature, new_feature}] }
    """
    init_db()
    groups = get_all_feature_groups()
    changes = []
    now = datetime.now().isoformat()

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT req_id, description, feature FROM requirements WHERE status='active'"
        ).fetchall()
        for row in rows:
            new_feat = detect_feature(row["description"], groups if groups else None)
            if new_feat != row["feature"]:
                conn.execute(
                    "UPDATE requirements SET feature=?, updated_at=? WHERE req_id=?",
                    (new_feat, now, row["req_id"])
                )
                changes.append({
                    "req_id":      row["req_id"],
                    "old_feature": row["feature"],
                    "new_feature": new_feat,
                })

    return {"reclassified": len(changes), "changes": changes, "total_checked": len(rows)}


def _next_req_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*) FROM requirements").fetchone()
    return f"REQ-{(row[0] + 1):03d}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_requirements(raw_text: str) -> Dict:
    """
    Parse raw_text → extract requirements → upsert vào DB.
    Returns: { added: [...], updated: [...], ambiguities: {...} }
    """
    init_db()
    # Dùng user groups nếu có, fallback về _FEATURE_PATTERNS
    user_groups = get_all_feature_groups()
    parsed = parse_requirements(raw_text, user_groups if user_groups else None)
    now = datetime.now().isoformat()

    added, updated = [], []
    all_ambiguities = {}

    skipped = []

    with _get_conn() as conn:
        # Load existing descriptions for dedup check
        existing_rows = conn.execute(
            "SELECT req_id, description FROM requirements WHERE status='active'"
        ).fetchall()
        existing_descs = [(r["req_id"], r["description"]) for r in existing_rows]

        for item in parsed:
            ambiguities = detect_ambiguities(item["description"])
            all_ambiguities[item["title"][:40]] = ambiguities
            ac = _generate_acceptance_criteria(item["description"])

            # ── Dedup: skip nếu overlap > 60% với bất kỳ REQ nào đã có ────
            dup_id = None
            for ex_id, ex_desc in existing_descs:
                if _word_overlap(item["description"], ex_desc) > 0.6:
                    dup_id = ex_id
                    break
            if dup_id:
                skipped.append({"title": item["title"], "duplicate_of": dup_id})
                continue

            # ── Exact match → update (version history) ───────────────────
            existing = conn.execute(
                "SELECT * FROM requirements WHERE LOWER(SUBSTR(description,1,80)) = LOWER(?)",
                (item["description"][:80],)
            ).fetchone()

            if existing:
                new_ver = existing["version"] + 1
                change = _summarize_diff(existing["description"], item["description"])
                conn.execute("""
                    INSERT INTO requirement_versions (req_id, version, description, changed_at, change_summary)
                    VALUES (?, ?, ?, ?, ?)
                """, (existing["req_id"], existing["version"], existing["description"], now, change))
                conn.execute("""
                    UPDATE requirements
                    SET description=?, feature=?, version=?, ambiguities=?,
                        acceptance_criteria=?, status='active', updated_at=?
                    WHERE req_id=?
                """, (item["description"], item["feature"], new_ver,
                      json.dumps(ambiguities), json.dumps(ac), now, existing["req_id"]))
                updated.append({"req_id": existing["req_id"], "title": item["title"], "version": new_ver})
            else:
                req_id = _next_req_id(conn)
                conn.execute("""
                    INSERT INTO requirements
                    (req_id, title, description, feature, source, version, ambiguities,
                     acceptance_criteria, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'active', ?, ?)
                """, (req_id, item["title"], item["description"],
                      item["feature"], item["source"],
                      json.dumps(ambiguities), json.dumps(ac), now, now))
                added.append({"req_id": req_id, "title": item["title"], "feature": item["feature"]})
                existing_descs.append((req_id, item["description"]))  # update local cache

    return {
        "added":         added,
        "updated":       updated,
        "skipped":       skipped,
        "ambiguities":   all_ambiguities,
        "total_new":     len(added),
        "total_updated": len(updated),
        "total_skipped": len(skipped),
    }


def get_all_requirements(feature: Optional[str] = None, status: str = "active") -> List[Dict]:
    """Lấy tất cả requirements, optionally filter theo feature."""
    init_db()
    with _get_conn() as conn:
        if feature:
            rows = conn.execute(
                "SELECT * FROM requirements WHERE status=? AND feature=? ORDER BY req_id",
                (status, feature)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM requirements WHERE status=? ORDER BY feature, req_id",
                (status,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_grouped_by_feature() -> Dict[str, List[Dict]]:
    """Trả về requirements đã gom nhóm theo feature."""
    reqs = get_all_requirements()
    groups: Dict[str, List[Dict]] = {}
    for r in reqs:
        groups.setdefault(r["feature"], []).append(r)
    return groups


def get_requirement(req_id: str) -> Optional[Dict]:
    init_db()
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM requirements WHERE req_id=?", (req_id,)).fetchone()
        return _row_to_dict(row) if row else None


def get_version_history(req_id: str) -> List[Dict]:
    """Lấy lịch sử thay đổi của một requirement."""
    init_db()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM requirement_versions WHERE req_id=? ORDER BY version DESC",
            (req_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def diff_with_new_text(new_text: str) -> Dict:
    """
    So sánh requirements mới (từ new_text) với DB hiện tại.
    Returns: { new: [...], changed: [...], unchanged: [...], deprecated: [...] }
    """
    init_db()
    parsed = parse_requirements(new_text)
    existing = {r["req_id"]: r for r in get_all_requirements()}

    new_items, changed, unchanged = [], [], []

    with _get_conn() as conn:
        for item in parsed:
            row = conn.execute(
                "SELECT * FROM requirements WHERE LOWER(SUBSTR(description,1,80)) = LOWER(?)",
                (item["description"][:80],)
            ).fetchone()
            if not row:
                new_items.append(item)
            elif row["description"] != item["description"]:
                changed.append({
                    "req_id": row["req_id"],
                    "old":    row["description"][:100],
                    "new":    item["description"][:100],
                    "diff":   _summarize_diff(row["description"], item["description"]),
                })
            else:
                unchanged.append(row["req_id"])

    # Requirements trong DB nhưng không có trong new_text → có thể deprecated
    parsed_texts = {p["description"][:80].lower() for p in parsed}
    deprecated = []
    for r in existing.values():
        if r["description"][:80].lower() not in parsed_texts:
            deprecated.append(r["req_id"])

    return {
        "new":        new_items,
        "changed":    changed,
        "unchanged":  unchanged,
        "deprecated": deprecated,
        "summary": (
            f"{len(new_items)} requirement mới, "
            f"{len(changed)} thay đổi, "
            f"{len(unchanged)} không đổi, "
            f"{len(deprecated)} có thể bị loại bỏ"
        )
    }


def deprecate_requirement(req_id: str) -> bool:
    init_db()
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE requirements SET status='deprecated', updated_at=? WHERE req_id=?",
            (datetime.now().isoformat(), req_id)
        )
        return cur.rowcount > 0


def get_stats() -> Dict:
    init_db()
    with _get_conn() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM requirements WHERE status='active'").fetchone()[0]
        by_feat  = conn.execute(
            "SELECT feature, COUNT(*) as cnt FROM requirements WHERE status='active' GROUP BY feature"
        ).fetchall()
        with_amb = conn.execute(
            "SELECT COUNT(*) FROM requirements WHERE status='active' AND ambiguities != '[]'"
        ).fetchone()[0]
        return {
            "total_active":      total,
            "with_ambiguities":  with_amb,
            "by_feature":        {r["feature"]: r["cnt"] for r in by_feat},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> Dict:
    d = dict(row)
    d["ambiguities"] = json.loads(d.get("ambiguities", "[]"))
    return d


def _summarize_diff(old: str, new: str) -> str:
    old_words = set(old.lower().split())
    new_words = set(new.lower().split())
    added   = new_words - old_words
    removed = old_words - new_words
    parts = []
    if added:   parts.append(f"Thêm: {', '.join(list(added)[:5])}")
    if removed: parts.append(f"Bỏ: {', '.join(list(removed)[:5])}")
    return "; ".join(parts) if parts else "Thay đổi nhỏ về diễn đạt"


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = """
    1. User có thể đăng nhập bằng email và password. Cần validate email format và mật khẩu tối thiểu 8 ký tự.
    2. Hệ thống phải gửi email xác nhận sau khi đăng ký tài khoản mới.
    3. User có thể tìm kiếm sản phẩm theo tên, danh mục, và khoảng giá.
    4. Giỏ hàng phải được lưu khi user đăng xuất và khôi phục khi đăng nhập lại.
    5. Thanh toán hỗ trợ VNPay, Momo và thẻ tín dụng.
    6. Admin có thể xem báo cáo doanh thu theo ngày, tuần, tháng.
    """

    print("=== SAVE ===")
    result = save_requirements(sample)
    print(f"Added: {result['total_new']}, Updated: {result['total_updated']}")

    print("\n=== GROUPED ===")
    groups = get_grouped_by_feature()
    for feature, reqs in groups.items():
        print(f"\n[{feature}]")
        for r in reqs:
            flag = " ⚠️" if r["ambiguities"] else ""
            print(f"  {r['req_id']}: {r['title']}{flag}")

    print("\n=== STATS ===")
    print(json.dumps(get_stats(), ensure_ascii=False, indent=2))
