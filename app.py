"""
QA Copilot — FastAPI Backend

WHAT: Python web server serving the QA Copilot application
WHY:  Replaces direct browser→Anthropic calls (CORS/key leak risk)
HOW:  FastAPI proxies Anthropic API, runs mock tests, tracks performance
"""

import os
import json
import time
import secrets
import logging
from dotenv import load_dotenv

load_dotenv()  # đọc .env trước khi dùng os.getenv()
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from performance_monitor import PerformanceMonitor
from mock_api_responses import run_batch, validate_response, TestStatus
from perf_store import log_request, get_history, get_provider_comparison, get_token_breakdown_analysis
from testcase_store import (
    save_testcases, get_all_testcases, get_grouped_by_feature as get_tc_grouped,
    get_testcase_stats, get_tc_history, deprecate_testcase,
)
from ai_provider import call_ai, get_provider_info
from requirement_store import (
    save_requirements, get_all_requirements, get_grouped_by_feature,
    get_requirement, get_version_history, diff_with_new_text,
    deprecate_requirement, get_stats as req_stats, init_db,
    get_all_feature_groups, save_feature_group, delete_feature_group,
    reclassify_all_requirements,
)
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from skill_store import save_skill, get_all_skills, get_skill, delete_skill
from cron_store import (
    save_cron, get_all_crons, get_cron, toggle_cron as _toggle_cron_db, delete_cron,
    log_cron_run, get_cron_logs, init_cron_db,
)

logger = logging.getLogger("qa_copilot")

# ---------------------------------------------------------------------------
# APScheduler — chạy skill theo lịch
# ---------------------------------------------------------------------------

_scheduler = BackgroundScheduler(timezone="Asia/Ho_Chi_Minh")


def _run_cron_job(cron_id: str):
    """Thực thi skill test cases được lập lịch."""
    import time as _t
    cron = get_cron(cron_id)
    if not cron or not cron["enabled"]:
        return
    skill = get_skill(cron["skill_id"])
    if not skill or not skill.get("test_cases"):
        log_cron_run(cron_id, "ERROR", "Skill không tồn tại hoặc rỗng", 0, 0, 0, 0)
        return
    t0 = _t.time()
    try:
        report  = run_batch(skill["test_cases"])
        s       = report["summary"]
        passed  = s["passed"]
        failed  = s["failed"]
        blocked = s.get("blocked", 0)
        status  = "PASS" if failed == 0 else "FAIL"
        summary = f"{passed}✅ {failed}❌ {blocked}⚠️ — {s['pass_rate']}"
    except Exception as e:
        passed = failed = blocked = 0
        status  = "ERROR"
        summary = str(e)[:200]
    log_cron_run(cron_id, status, summary, passed, failed, blocked,
                 int((_t.time() - t0) * 1000))


def _reload_scheduler():
    """Đọc cron_jobs từ DB và sync lại APScheduler."""
    _scheduler.remove_all_jobs()
    for c in get_all_crons():
        if not c["enabled"]:
            continue
        try:
            minute, hour, day, month, dow = c["cron_expr"].split()
            _scheduler.add_job(
                _run_cron_job,
                CronTrigger(minute=minute, hour=hour, day=day,
                            month=month, day_of_week=dow),
                args=[c["id"]], id=c["id"], replace_existing=True,
            )
        except Exception as ex:
            logger.warning("Không schedule cron '%s': %s", c["name"], ex)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    init_cron_db()
    _reload_scheduler()
    _scheduler.start()
    yield
    _scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="QA Copilot", version="1.0.0", docs_url=None, redoc_url=None,
              lifespan=_lifespan)
monitor = PerformanceMonitor()

# API keys vẫn đọc trực tiếp để check health
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "")
GREENODE_API_KEY  = os.getenv("GREENODE_API_KEY", "")

# ---------------------------------------------------------------------------
# [SEC-1] CORS — chỉ cho phép origin cụ thể
# ---------------------------------------------------------------------------

_ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# ---------------------------------------------------------------------------
# [SEC-2] Authentication — X-API-Key header
# Đặt APP_API_KEY trong environment; nếu không set thì chỉ allow localhost
# ---------------------------------------------------------------------------

APP_API_KEY = os.getenv("APP_API_KEY", "")

def require_api_key(request: Request):
    """Kiểm tra X-API-Key header. Bỏ qua nếu request từ localhost và APP_API_KEY chưa set."""
    if not APP_API_KEY:
        # Dev mode: chỉ cho phép localhost
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="Access denied: set APP_API_KEY to enable remote access.")
        return
    key = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(key, APP_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")

# ---------------------------------------------------------------------------
# [SEC-3] Rate limiting đơn giản — per-IP, in-memory
# ---------------------------------------------------------------------------

from collections import defaultdict
_rate_store: Dict[str, List[float]] = defaultdict(list)
RATE_LIMIT = int(os.getenv("RATE_LIMIT_PER_MIN", "20"))

def check_rate_limit(request: Request):
    if RATE_LIMIT <= 0:
        return
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = _rate_store[ip]
    # Xóa requests cũ hơn 60s
    _rate_store[ip] = [t for t in window if now - t < 60]
    if len(_rate_store[ip]) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded: max {RATE_LIMIT} requests/minute.")

# ---------------------------------------------------------------------------
# Router — classify CAP from last user message (port từ integration_framework)
# CAP-5 và CAP-9 xử lý local, không gọi Anthropic → tiết kiệm token
# ---------------------------------------------------------------------------

import re as _re
import re

def classify_cap(text: str) -> str:
    """
    Phân loại capability từ nội dung message.
    Mirrors detectCap() ở frontend nhưng chạy server-side để routing chính xác hơn.
    """
    t = text.lower()
    if _re.search(r"requirement|yêu cầu|feature|user story|acceptance criteria", t): return "CAP-1"
    if _re.search(r"\brisk\b|rủi ro|impact.*likelihood|risk score", t):              return "CAP-2"
    if _re.search(r"test plan|kế hoạch test|test strategy", t):                      return "CAP-3"
    if _re.search(r"export|xuất|markdown|jira|confluence|csv|excel|xlsx", t):        return "CAP-9"
    if _re.search(r"test case|viết test|tc_|happy path|boundary|edge case", t):      return "CAP-4"
    if _re.search(r"\bapi\b|endpoint|chạy test|run test|cap-5", t):                  return "CAP-5"
    if _re.search(r"synthesis|tổng hợp|diff.*requirement|requirement.*diff", t):     return "CAP-6"
    if _re.search(r"\bbug\b|lỗi|bug report|bug-\d+", t):                             return "CAP-7"
    if _re.search(r"coverage|matrix|traceability", t):                               return "CAP-8"
    return "CHAT"


# ---------------------------------------------------------------------------
# CAP-5 local handler — parse test specs từ message, chạy mock, trả kết quả
# ---------------------------------------------------------------------------

_TC_PATTERNS = [
    # "POST /auth/login expect 200"
    _re.compile(r"(GET|POST|PUT|PATCH|DELETE)\s+(/[\w/{}.-]+)\s+expect\s+(\d+)", _re.I),
    # "endpoint: /health method: GET status: 200"
    _re.compile(r"endpoint[:\s]+(/[\w/{}.-]+).*?method[:\s]+(GET|POST|PUT|PATCH|DELETE).*?status[:\s]+(\d+)", _re.I | _re.S),
]

def _extract_test_cases_from_text(text: str) -> list:
    """Tách test case specs từ free-text message."""
    found = []
    for pattern in _TC_PATTERNS:
        for i, m in enumerate(pattern.finditer(text)):
            groups = m.groups()
            if len(groups) == 3:
                method, path, status = groups[0], groups[1], int(groups[2])
            else:
                path, method, status = groups[0], groups[1], int(groups[2])
            found.append({
                "tc_id": f"TC_INLINE_{i+1:03d}",
                "method": method.upper(),
                "path": path,
                "expected_status": status,
                "max_latency_ms": 2000,
            })
    return found


def handle_cap5_local(text: str) -> str:
    """Chạy mock API test từ text message, trả formatted report (không dùng token)."""
    test_cases = _extract_test_cases_from_text(text)

    if not test_cases:
        return (
            "⚠️ **CAP-5 — Không parse được test case từ message.**\n\n"
            "Vui lòng dùng format:\n"
            "```\nPOST /auth/login expect 200\nGET /health expect 200\n```\n"
            "Hoặc dùng **Mock API Test Runner** ở sidebar để nhập thủ công."
        )

    report = run_batch(test_cases)
    s = report["summary"]

    lines = [
        f"## 📊 CAP-5 — API Test Results\n",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total  | {s['total']} |",
        f"| ✅ Passed | {s['passed']} |",
        f"| ❌ Failed | {s['failed']} |",
        f"| Pass Rate | **{s['pass_rate']}** |",
        f"| Avg Latency | {s['avg_duration_ms']}ms |",
        f"| Slowest | {s['slowest_endpoint']} |",
        f"\n### Chi tiết\n",
    ]

    for r in report["results"]:
        icon = "✅" if r["status"] == "PASS" else ("⚠️" if r["status"] == "BLOCKED" else "❌")
        lines.append(f"{icon} **{r['tc_id']}** — `{r['endpoint']}` — {r['duration_ms']}ms")
        if r["diff"]:
            lines.append(f"   > {r['diff']}")

    # Nếu có FAIL → tự động tạo bug report skeleton
    failed = [r for r in report["results"] if r["status"] == "FAIL"]
    if failed:
        lines.append("\n---\n### 🐛 Auto Bug Reports\n")
        for i, r in enumerate(failed, 1):
            lines.append(
                f"**BUG-{i:03d}** | Severity: Major | Priority: P1\n"
                f"- **TC**: {r['tc_id']}\n"
                f"- **Endpoint**: `{r['endpoint']}`\n"
                f"- **Actual**: {r['diff']}\n"
                f"- **Root Cause Category**: API Contract Violation\n"
            )

    # Record vào performance monitor
    for r in report["results"]:
        monitor.record_interaction(
            interaction_id=r["tc_id"],
            prompt=r["endpoint"],
            response=str(r["passed"]),
            duration=r["duration_ms"] / 1000,
            tokens_used=0,
            tools_used=["CAP-5"],
            success=(r["status"] == "PASS"),
            error=r["diff"] if r["status"] == "FAIL" else None,
            cost=0.0,
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CAP-9 local handler — export session data (không dùng token)
# ---------------------------------------------------------------------------

def _csv_escape(val: str) -> str:
    """Escape a value for CSV: wrap in quotes if it contains comma/newline/quote."""
    if val is None:
        return ""
    val = str(val).replace('"', '""')
    if any(c in val for c in (',', '"', '\n', '\r')):
        return f'"{val}"'
    return val


def _build_coverage_matrix_csv(db_reqs: list, db_tcs: list) -> str:
    """Tạo coverage matrix CSV: REQ ↔ TC traceability."""
    # Index TCs by linked REQs
    req_to_tcs: dict = {}
    for tc in db_tcs:
        for req_id in (tc.get("linked_reqs") or "").split(","):
            req_id = req_id.strip()
            if req_id:
                req_to_tcs.setdefault(req_id, []).append(tc["tc_id"])

    header = "REQ_ID,Content,Feature,Status,TC_Count,TC_IDs,Coverage"
    rows = [header]
    covered = uncovered = 0
    for r in db_reqs:
        req_id  = r.get("req_id", "")
        content = (r.get("description") or r.get("content") or "")[:100].replace("\n", " ")
        feature = r.get("feature", "")
        status  = r.get("status", "active")
        tcs     = req_to_tcs.get(req_id, [])
        tc_count = len(tcs)
        tc_ids   = "; ".join(tcs)
        coverage = "Covered" if tcs else "Not Covered"
        if tcs:
            covered += 1
        else:
            uncovered += 1
        rows.append(",".join(_csv_escape(str(v)) for v in (
            req_id, content, feature, status, tc_count, tc_ids, coverage
        )))

    total = covered + uncovered
    pct   = round(covered / total * 100) if total else 0
    summary = (
        f"\n✅ **Coverage Matrix** — {covered}/{total} requirements covered "
        f"(**{pct}%**). {uncovered} chưa có test case."
    )
    return "```csv\n" + "\n".join(rows) + "\n```" + summary


def handle_cap9_local(text: str, api_messages: list) -> str:
    """Tạo export từ conversation history + SQLite store, không cần gọi AI."""
    t = text.lower()

    # Pull full data from SQLite stores
    db_tcs   = get_all_testcases()
    db_reqs  = []
    try:
        from requirement_store import get_all_requirements
        db_reqs = get_all_requirements()
    except Exception:
        pass

    # Fallback: scan conversation for IDs not yet in DB
    content_blocks = [m["content"] for m in api_messages if m.get("role") == "assistant"]
    full_content   = "\n\n".join(content_blocks)
    conv_reqs = _re.findall(r"REQ-\d+[^\n]*", full_content)
    bugs      = _re.findall(r"BUG-\d+[^\n]*", full_content)

    is_coverage = bool(_re.search(r"coverage|matrix|traceability", t))
    is_excel    = "excel" in t or "xlsx" in t

    # ── Coverage matrix export (excel/csv/markdown) ───────────────────────────
    if is_coverage:
        if not db_reqs and not db_tcs:
            return (
                "⚠️ **CAP-9** — Chưa có dữ liệu.\n"
                "Hãy thực hiện **CAP-1** (requirements) và **CAP-4** (test cases) trước."
            )
        csv_content = _build_coverage_matrix_csv(db_reqs, db_tcs)
        if is_excel:
            # Excel có thể mở file CSV trực tiếp — hướng dẫn cách lưu
            return (
                csv_content + "\n\n"
                "💡 **Lưu thành file Excel**: Copy nội dung CSV trên → mở Excel → "
                "Data → From Text/CSV → Paste. Hoặc lưu file `.csv` rồi mở bằng Excel."
            )
        return csv_content

    # ── CSV / Excel export (test cases) ──────────────────────────────────────
    if "csv" in t or is_excel:
        if db_tcs:
            header = "TC_ID,Title,Feature,Type,Priority,Status,Steps,Expected Result,Linked REQs"
            rows = [header]
            for tc in db_tcs:
                row = ",".join(_csv_escape(str(tc.get(f, "") or "")) for f in (
                    "tc_id", "title", "feature", "type", "priority", "status",
                    "steps", "expected", "linked_reqs"
                ))
                rows.append(row)
            total = len(db_tcs)
            return (
                f"```csv\n" + "\n".join(rows) + "\n```\n\n"
                f"✅ Exported **{total} test cases** từ database "
                f"({len(set(tc['feature'] for tc in db_tcs))} features)."
            )
        # Nothing in DB yet — tell user to run CAP-4 first
        tc_ids = list(dict.fromkeys(_re.findall(r"TC_[A-Z_]+\d+", full_content)))
        if tc_ids:
            rows = ["TC_ID,Title,Feature,Type,Priority,Status,Steps,Expected Result,Linked REQs"]
            rows += [f"{tid},,,,,,," for tid in tc_ids]
            return (
                "```csv\n" + "\n".join(rows) + "\n```\n\n"
                "⚠️ Test cases chưa được lưu vào DB. "
                "Hãy nhập requirement qua CAP-4 để lưu đầy đủ."
            )
        return (
            "⚠️ **CAP-9** — Chưa có test case nào trong database.\n"
            "Hãy thực hiện **CAP-4** trước để tạo và lưu test cases."
        )

    # ── JSON export ───────────────────────────────────────────────────────────
    if "json" in t:
        data = {
            "export_type": "QA Copilot Session",
            "test_cases":  db_tcs,
            "requirements": [dict(r) for r in db_reqs] if db_reqs else list(dict.fromkeys(conv_reqs)),
            "bugs":         list(dict.fromkeys(bugs)),
        }
        return f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"

    # ── Default: Markdown summary ─────────────────────────────────────────────
    req_list  = [r.get("req_id", "") + " — " + (r.get("description") or r.get("content") or "")[:80] for r in db_reqs] if db_reqs else list(dict.fromkeys(conv_reqs))
    lines = [
        "## 📤 CAP-9 — Session Export (Markdown)\n",
        f"### 📄 Requirements ({len(req_list)})",
    ]
    for r in req_list: lines.append(f"- {r}")
    lines.append(f"\n### 📋 Test Cases ({len(db_tcs)})")
    for tc in db_tcs:
        lines.append(f"- **{tc['tc_id']}** [{tc.get('type','')}] — {tc.get('title','')}")
    lines.append(f"\n### 🐛 Bugs ({len(set(bugs))})")
    for b in dict.fromkeys(bugs): lines.append(f"- {b}")

    if not req_list and not db_tcs and not bugs:
        return (
            "⚠️ **CAP-9** — Chưa có dữ liệu để export.\n"
            "Hãy thực hiện CAP-1 → CAP-4 trước để có requirement và test case."
        )

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert QA Agent with deep knowledge of software testing methodologies.
You support QA engineers through the complete testing lifecycle.

## IDENTITY
- Name: QA Copilot
- Persona: Methodical, precise, proactive — you think like a tester
- Language: Vietnamese by default; all technical artifacts (IDs, field names, JSON, API logs) stay in English

---

## SESSION STATE
Maintain this state throughout the conversation:
{
  "requirements": {
    "current_version": null,
    "history": [],
    "ambiguities": []
  },
  "test_plan": null,
  "test_cases": [],
  "test_results": [],
  "bugs": [],
  "coverage_matrix": {}
}

---

## CAPABILITIES (9 core)

### [CAP-1] READ & ANALYZE REQUIREMENT
Trigger: User pastes requirement text
Actions:
1. Parse and extract: features, user stories, acceptance criteria, constraints, APIs
2. Assign ID to each requirement: REQ-001, REQ-002...
3. Flag AMBIGUITY: requirements thiếu rõ ràng, mâu thuẫn, hoặc không testable
4. Ask max 3 clarifying questions trước khi tiếp tục
5. Store in requirements.current_version với timestamp
Output: Formatted requirement list + danh sách câu hỏi clarification

### [CAP-2] RISK ANALYSIS
Trigger: Sau khi đọc requirement, hoặc user yêu cầu
Actions:
1. Đánh giá risk từng feature theo ma trận: Impact (1-5) × Likelihood (1-5) = Risk Score
2. Classify: HIGH (≥15), MEDIUM (8-14), LOW (<8)
3. Gợi ý test priority dựa trên risk score
4. Đặc biệt flag: security, payment, auth, data loss scenarios
Output: Risk matrix table + recommended test focus areas

### [CAP-3] WRITE TEST PLAN
Trigger: User yêu cầu sau khi requirement đã được phân tích
Pre-condition: CAP-1 phải đã chạy
Actions:
1. Scope & objectives
2. Test strategy: functional, regression, negative, boundary, performance
3. Environment assumptions (OS, browser, API version...)
4. Entry/Exit criteria
5. Effort estimate (S/M/L per feature)
6. Risk-based test priority (dựa trên CAP-2)
Output: Structured test plan in Markdown

### [CAP-4] WRITE TEST CASES
Trigger: User yêu cầu sau khi test plan được approve
Pre-condition: CAP-3 phải đã chạy
Actions:
1. Generate theo format:
   - TC_[FEATURE]_[NUMBER] (e.g., TC_LOGIN_001)
   - Title, Priority (P0/P1/P2), Preconditions
   - Steps (numbered), Expected Result
   - Test Type: Happy Path | Negative | Boundary | Edge Case
2. Luôn sinh đủ 4 loại test case cho mỗi feature
3. Gợi ý boundary values cụ thể (e.g., field maxlength, empty string, null)
4. Update coverage_matrix: map REQ-ID → [TC-IDs]
Output: Test case table + coverage summary

### [CAP-5] RUN TEST CASES VIA API
Trigger: User cung cấp API details + yêu cầu chạy
Input cần có: endpoint, method, headers, request body, expected response
Actions:
1. Build request từ test case spec
2. Execute API call
3. Validate: status code, response schema, field values, response time
4. Mark result: PASS / FAIL / BLOCKED / SKIP
5. Nếu FAIL: tự động trigger CAP-7 (Bug Report)
6. Log: { request, response, diff, duration_ms }
Output: Test execution report với pass/fail breakdown

### [CAP-6] REQUIREMENT SYNTHESIS
Trigger: Tự động sau mỗi lần user cung cấp requirement mới
Actions:
1. Diff với version trước: thêm / sửa / xóa gì
2. Identify impacted test cases (cần update/rerun)
3. Flag regression risk: thay đổi này ảnh hưởng flow nào?
4. Append vào requirements.history với version number
Output: Change summary + impact analysis + list TC cần review

### [CAP-7] BUG REPORT
Trigger: Test FAIL, hoặc user báo bug thủ công
Actions:
1. Generate bug report:
   - BUG-[NUMBER] (e.g., BUG-001)
   - Title (concise, actionable)
   - Severity: Critical / Major / Minor / Trivial
   - Priority: P0 / P1 / P2
   - Environment, Steps to Reproduce, Actual vs Expected
   - Root cause category: Logic Error | Missing Validation | API Contract Violation | UI Issue | Performance
2. Link to failed test case
Output: Formatted bug report (Jira-ready format)

### [CAP-8] COVERAGE MATRIX
Trigger: User yêu cầu hoặc sau khi test cases được generate
Actions:
1. Build traceability matrix: Requirements ↔ Test Cases ↔ Results
2. Highlight: covered ✅ | not covered ❌ | partially covered ⚠️
3. Calculate coverage %
4. Gợi ý thêm test cases cho các requirement chưa covered
Output: Matrix table + coverage percentage + gap analysis

### [CAP-9] EXPORT
Trigger: User yêu cầu xuất file
Formats supported:
- Markdown (default)
- JSON (for tool integration)
- CSV/Excel-ready table (copy-paste friendly)
- Jira format (issue description + acceptance criteria)
- Confluence wiki format

---

## BEHAVIOR RULES
1. Luôn bắt đầu bằng: "Bạn muốn tôi làm gì hôm nay?" và liệt kê capabilities
2. Khi nhận requirement mới → tự động chạy CAP-1 và CAP-6 (nếu đã có version trước)
3. Không viết test plan nếu requirement chưa được clarify
4. Không chạy API test nếu test case chưa được review
5. Khi FAIL → tự động tạo bug report, không cần hỏi
6. Hỏi thêm thông tin khi thiếu, thay vì đoán mò
7. Sau mỗi action lớn, tóm tắt state hiện tại (version requirement, số TC, số bug)

## FALLBACK RULE
- Nếu không tìm thấy thông tin trong context → nói rõ: "Tôi chưa có thông tin này, bạn có thể cung cấp thêm không?"
- Không tự bịa expected result hay API response

## PERFORMANCE TRACKING
Sau mỗi API test run, báo cáo:
- Total: X passed, Y failed, Z blocked
- Pass rate: X%
- Avg response time: Xms
- Slowest endpoint: X (Xms)

---

## OUTPUT FORMAT
Dùng emoji để dễ scan:
✅ PASS  ❌ FAIL  ⚠️ BLOCKED  🔴 P0  🟠 P1  🟡 P2
🐛 Bug  📋 Test Case  📄 Requirement  📊 Report"""


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class ChatRequest(BaseModel):
    messages: List[Message]
    cap: Optional[str] = "CHAT"
    api_messages: Optional[List[Message]] = None

    # [SEC-9] Giới hạn conversation history để tránh token abuse
    @field_validator("messages", "api_messages", mode="before")
    @classmethod
    def cap_messages(cls, v):
        if v is not None and len(v) > 100:
            raise ValueError("messages vượt giới hạn 100 turns")
        return v

class MockTestCase(BaseModel):
    tc_id: str
    method: str = "GET"
    path: str
    expected_status: int = 200
    expected_fields: Optional[Dict[str, Any]] = None
    max_latency_ms: int = 2000
    body: Optional[Dict] = None
    headers: Optional[Dict[str, str]] = None
    extract: Optional[Dict[str, str]] = None      # {"var": "dot.path"} — extract from response
    depends_on: Optional[List[str]] = None        # ["TC_ID"] — block if dependency failed

    # [SEC] Giới hạn độ dài input
    @field_validator("tc_id", "path", mode="before")
    @classmethod
    def cap_length(cls, v):
        if isinstance(v, str) and len(v) > 500:
            raise ValueError("Giá trị vượt giới hạn 500 ký tự")
        return v

class MockRunRequest(BaseModel):
    test_cases: List[MockTestCase]

    @field_validator("test_cases", mode="before")
    @classmethod
    def cap_test_cases(cls, v):
        if len(v) > 50:
            raise ValueError("Tối đa 50 test cases mỗi lần chạy")
        return v


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse("static/index.html")

@app.get("/health")
async def health():
    info = get_provider_info()
    GREENODE_API_KEY = os.getenv("GREENODE_API_KEY", "")
    CURSOR_API_KEY   = os.getenv("CURSOR_API_KEY", "")
    keys = {
        "anthropic": bool(ANTHROPIC_API_KEY),
        "gemini":    bool(GEMINI_API_KEY),
        "openai":    bool(OPENAI_API_KEY),
        "qwen":      bool(GREENODE_API_KEY),
        "gemma":     bool(GREENODE_API_KEY),
        "minimax":   bool(GREENODE_API_KEY),
        "cursor":    bool(CURSOR_API_KEY),
    }
    return {
        "status":   "ok",
        "provider": info["provider"],
        "model":    info["model"],
        "key_set":  keys.get(info["provider"], False),
    }

@app.get("/api/provider")
async def api_provider(_auth=Depends(require_api_key)):
    """Trả về provider & model đang active."""
    info = get_provider_info()
    GREENODE_API_KEY = os.getenv("GREENODE_API_KEY", "")
    CURSOR_API_KEY   = os.getenv("CURSOR_API_KEY", "")
    keys = {
        "anthropic": bool(ANTHROPIC_API_KEY),
        "gemini":    bool(GEMINI_API_KEY),
        "openai":    bool(OPENAI_API_KEY),
        "qwen":      bool(GREENODE_API_KEY),
        "gemma":     bool(GREENODE_API_KEY),
        "minimax":   bool(GREENODE_API_KEY),
        "cursor":    bool(CURSOR_API_KEY),
    }
    return {**info, "keys_configured": keys}


@app.post("/api/chat")
async def chat(req: ChatRequest, _auth=Depends(require_api_key), _rate=Depends(check_rate_limit)):
    """
    Smart router — classify CAP, then:
      CAP-5 → mock_api_responses (0 token)
      CAP-9 → local exporter     (0 token)
      others → Anthropic API
    """
    last_msg = req.messages[-1].content if req.messages else ""

    # --- Server-side CAP classification (overrides frontend hint if more specific) ---
    cap = classify_cap(last_msg)
    if cap == "CHAT" and req.cap and req.cap != "CHAT":
        cap = req.cap  # fallback to frontend hint

    start = time.time()

    # ── ROUTE: CAP-5 local (mock API test) ──────────────────────────────────
    if cap == "CAP-5":
        response_text = handle_cap5_local(last_msg)
        duration = time.time() - start
        return {
            "content": response_text,
            "cap": cap,
            "routed_local": True,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "duration_ms": int(duration * 1000),
            "cost": 0.0,
        }

    # ── ROUTE: CAP-9 local (export) ─────────────────────────────────────────
    if cap == "CAP-9":
        history = [{"role": m.role, "content": m.content} for m in (req.api_messages or req.messages)]
        response_text = handle_cap9_local(last_msg, history)
        duration = time.time() - start
        monitor.record_interaction(
            interaction_id=f"cap9_{int(start*1000)}",
            prompt=last_msg, response=response_text,
            duration=duration, tokens_used=0,
            tools_used=["CAP-9"], success=True, cost=0.0,
        )
        return {
            "content": response_text,
            "cap": cap,
            "routed_local": True,
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "duration_ms": int(duration * 1000),
            "cost": 0.0,
        }

    # ── ROUTE: AI Provider (CAP-1/2/3/4/6/7/8/CHAT) ────────────────────────
    interaction_id = f"{cap}_{int(start * 1000)}"
    error_msg      = None
    response_text  = ""
    input_tokens   = 0
    output_tokens  = 0
    cost           = 0.0

    _routed_model = None
    try:
        ai_resp = call_ai(
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            system=SYSTEM_PROMPT,
            cap=cap,
        )
        _routed_model = ai_resp.model
        response_text = ai_resp.text
        input_tokens  = ai_resp.input_tokens
        output_tokens = ai_resp.output_tokens
        cost          = ai_resp.cost

    except ValueError as e:
        error_msg = "ConfigError"
        logger.error("AI provider config error: %s", e)
        raise HTTPException(status_code=500, detail="Server configuration error. Liên hệ administrator.")
    except Exception as e:
        error_msg = type(e).__name__
        logger.error("AI provider error [%s]: %s", error_msg, e, exc_info=True)
        # Trả thông báo thân thiện theo loại lỗi
        msg = "Đã vượt quá rate limit, thử lại sau." if "ratelimit" in error_msg.lower() \
            else "API key không hợp lệ." if "auth" in error_msg.lower() \
            else "Lỗi xử lý request, thử lại sau."
        raise HTTPException(status_code=500, detail=msg)
    finally:
        duration = time.time() - start
        monitor.record_interaction(
            interaction_id=interaction_id,
            prompt=last_msg,
            response=response_text,
            duration=duration,
            tokens_used=input_tokens + output_tokens,
            tools_used=[cap],
            success=error_msg is None,
            error=error_msg,
            cost=cost,
        )
        # Persistent log với token breakdown chi tiết
        from ai_provider import PROVIDER, MODEL, _PRICING
        price_in, price_out = _PRICING.get(PROVIDER, (0.0, 0.0))
        log_request(
            provider=PROVIDER,
            model=_routed_model or MODEL or "",
            cap=cap,
            system_prompt=SYSTEM_PROMPT,
            messages=[{"role": m.role, "content": m.content} for m in req.messages],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            duration_ms=int(duration * 1000),
            success=error_msg is None,
            error_type=error_msg,
            response_text=response_text,
            routed_local=False,
            price_input_per_1m=price_in,
            price_output_per_1m=price_out,
        )

    # Auto-save requirements nếu là CAP-1
    if cap == "CAP-1" and response_text:
        _try_autosave_requirements(last_msg, response_text)

    # Auto-save test cases nếu là CAP-4
    if cap == "CAP-4" and response_text:
        try:
            save_testcases(response_text, source_cap="CAP-4")
        except Exception as e:
            logger.warning("Auto-save testcases failed: %s", e)

    return {
        "content": response_text,
        "cap": cap,
        "routed_local": False,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "duration_ms": int((time.time() - start) * 1000),
        "cost": cost,
    }


@app.post("/api/mock-test")
async def run_mock_tests(req: MockRunRequest, _auth=Depends(require_api_key), _rate=Depends(check_rate_limit)):
    """Run mock API test cases via mock_api_responses module."""
    test_cases = [tc.model_dump() for tc in req.test_cases]
    report = run_batch(test_cases)

    # Record each test run in performance monitor
    for r in report["results"]:
        monitor.record_interaction(
            interaction_id=r["tc_id"],
            prompt=r["endpoint"],
            response=json.dumps(r["failed"]),
            duration=r["duration_ms"] / 1000,
            tokens_used=0,
            tools_used=["mock_api"],
            success=(r["status"] == TestStatus.PASS),
            error=r["diff"] if r["status"] == TestStatus.FAIL else None,
            cost=0.0,
        )

    return report


@app.get("/api/stats")
async def get_stats(_auth=Depends(require_api_key)):
    return monitor.get_summary_stats()


@app.get("/api/stats/recent")
async def get_recent(
    count: int = Query(default=10, ge=1, le=100),  # [SEC-7] Clamp count
    _auth=Depends(require_api_key),
):
    return monitor.get_recent_interactions(count)


@app.get("/api/stats/report")
async def get_report(_auth=Depends(require_api_key)):
    return {"report": monitor.get_detailed_report()}


@app.post("/api/stats/reset")
async def reset_stats(_auth=Depends(require_api_key)):  # [SEC-8] Auth required
    monitor.reset()
    return {"message": "Stats reset successfully"}


# ---------------------------------------------------------------------------
# Serve static files last (so API routes take priority)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Requirements endpoints
# ---------------------------------------------------------------------------

class SaveRequirementsRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v):
        if not v.strip():
            raise ValueError("text không được để trống")
        if len(v) > 20000:
            raise ValueError("text vượt giới hạn 20,000 ký tự")
        return v

class DiffRequest(BaseModel):
    text: str


@app.post("/api/requirements/save")
async def api_save_requirements(req: SaveRequirementsRequest, _auth=Depends(require_api_key)):
    """Parse và lưu requirements từ free-text vào DB."""
    return save_requirements(req.text)


@app.get("/api/requirements")
async def api_get_requirements(feature: Optional[str] = None, _auth=Depends(require_api_key)):
    """Lấy tất cả requirements, filter theo feature nếu cần."""
    return get_all_requirements(feature=feature)


@app.get("/api/requirements/grouped")
async def api_get_grouped(_auth=Depends(require_api_key)):
    """Lấy requirements đã gom nhóm theo feature."""
    return get_grouped_by_feature()


@app.get("/api/requirements/stats")
async def api_req_stats(_auth=Depends(require_api_key)):
    return req_stats()


@app.post("/api/requirements/diff")
async def api_diff(req: DiffRequest, _auth=Depends(require_api_key)):
    """So sánh text mới với requirements đang có trong DB."""
    return diff_with_new_text(req.text)


@app.get("/api/requirements/{req_id}")
async def api_get_requirement(req_id: str, _auth=Depends(require_api_key)):
    r = get_requirement(req_id.upper())
    if not r:
        raise HTTPException(status_code=404, detail=f"{req_id} không tồn tại")
    return r


@app.get("/api/requirements/{req_id}/history")
async def api_get_history(req_id: str, _auth=Depends(require_api_key)):
    return get_version_history(req_id.upper())


@app.delete("/api/requirements/{req_id}")
async def api_deprecate(req_id: str, _auth=Depends(require_api_key)):
    ok = deprecate_requirement(req_id.upper())
    if not ok:
        raise HTTPException(status_code=404, detail=f"{req_id} không tồn tại")
    return {"message": f"{req_id} đã được đánh dấu deprecated"}


@app.post("/api/requirements/reclassify")
async def api_reclassify(_auth=Depends(require_api_key)):
    """Chạy lại phân loại feature cho tất cả requirements theo groups hiện tại."""
    return reclassify_all_requirements()


# ---------------------------------------------------------------------------
# Feature Groups
# ---------------------------------------------------------------------------

class FeatureGroupRequest(BaseModel):
    name: str
    description: str = ""
    keywords: List[str] = []
    color: str = ""
    sort_order: int = 0


@app.get("/api/feature-groups")
async def api_get_feature_groups(_auth=Depends(require_api_key)):
    """Lấy tất cả user-defined feature groups."""
    return get_all_feature_groups()


@app.post("/api/feature-groups")
async def api_create_feature_group(req: FeatureGroupRequest, _auth=Depends(require_api_key)):
    """Tạo hoặc cập nhật một feature group."""
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name không được trống")
    return save_feature_group(req.name.strip(), req.description, req.keywords, req.color, req.sort_order)


@app.put("/api/feature-groups/{name}")
async def api_update_feature_group(name: str, req: FeatureGroupRequest, _auth=Depends(require_api_key)):
    """Cập nhật feature group theo tên."""
    return save_feature_group(name, req.description, req.keywords, req.color, req.sort_order)


@app.delete("/api/feature-groups/{name}")
async def api_delete_feature_group(name: str, _auth=Depends(require_api_key)):
    """Xóa feature group. Requirements trong group chuyển về 'Unassigned'."""
    ok = delete_feature_group(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Group '{name}' không tồn tại")
    return {"message": f"Đã xóa group '{name}'"}


# ---------------------------------------------------------------------------
# Skill Library
# ---------------------------------------------------------------------------

class SaveSkillRequest(BaseModel):
    name: str
    test_cases: List[Dict[str, Any]]
    description: str = ""
    category: str = "General"
    tags: List[str] = []

    @field_validator("test_cases")
    @classmethod
    def cap_tc(cls, v):
        if len(v) > 50:
            raise ValueError("Tối đa 50 test cases mỗi skill")
        return v


@app.get("/api/skills")
async def api_get_skills(_auth=Depends(require_api_key)):
    return get_all_skills()


@app.post("/api/skills")
async def api_save_skill(req: SaveSkillRequest, _auth=Depends(require_api_key)):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name không được trống")
    return save_skill(req.name.strip(), req.test_cases, req.description, req.category, req.tags)


@app.get("/api/skills/{skill_id}")
async def api_get_skill(skill_id: str, _auth=Depends(require_api_key)):
    s = get_skill(skill_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' không tồn tại")
    return s


@app.delete("/api/skills/{skill_id}")
async def api_delete_skill(skill_id: str, _auth=Depends(require_api_key)):
    ok = delete_skill(skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' không tồn tại")
    return {"message": f"Đã xóa skill '{skill_id}'"}


# ---------------------------------------------------------------------------
# Cron Jobs
# ---------------------------------------------------------------------------

class CronRequest(BaseModel):
    name: str
    skill_id: str
    schedule: str

class CronToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/crons")
async def api_get_crons(_auth=Depends(require_api_key)):
    return get_all_crons()


@app.post("/api/crons")
async def api_create_cron(req: CronRequest, _auth=Depends(require_api_key)):
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="name không được trống")
    if not req.skill_id.strip():
        raise HTTPException(status_code=422, detail="skill_id không được trống")
    result = save_cron(req.name.strip(), req.skill_id.strip(), req.schedule.strip())
    _reload_scheduler()
    return result


@app.patch("/api/crons/{cron_id}/toggle")
async def api_toggle_cron(cron_id: str, req: CronToggleRequest, _auth=Depends(require_api_key)):
    ok = _toggle_cron_db(cron_id, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Cron '{cron_id}' không tồn tại")
    _reload_scheduler()
    return {"cron_id": cron_id, "enabled": req.enabled}


@app.delete("/api/crons/{cron_id}")
async def api_delete_cron(cron_id: str, _auth=Depends(require_api_key)):
    ok = delete_cron(cron_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Cron '{cron_id}' không tồn tại")
    _reload_scheduler()
    return {"message": f"Đã xóa cron '{cron_id}'"}


@app.get("/api/crons/{cron_id}/logs")
async def api_cron_logs(cron_id: str, limit: int = Query(20, ge=1, le=100),
                        _auth=Depends(require_api_key)):
    return get_cron_logs(cron_id, limit)


@app.post("/api/crons/{cron_id}/run")
async def api_run_cron_now(cron_id: str, _auth=Depends(require_api_key)):
    """Chạy cron job ngay lập tức (không chờ lịch)."""
    cron = get_cron(cron_id)
    if not cron:
        raise HTTPException(status_code=404, detail=f"Cron '{cron_id}' không tồn tại")
    _run_cron_job(cron_id)
    updated = get_cron(cron_id)
    return {
        "message": "Đã chạy xong",
        "last_status": updated["last_status"],
        "last_summary": updated["last_summary"],
    }


# ---------------------------------------------------------------------------
# Auto-save requirements từ CAP-1 chat response
# ---------------------------------------------------------------------------

def _try_autosave_requirements(user_text: str, agent_reply: str):
    """
    Nếu agent trả về danh sách REQ-xxx → tự động lưu vào DB.
    Trigger: user message chứa requirement text dài (> 100 chars).
    """
    if len(user_text) < 100:
        return
    if not re.search(r"requirement|yêu cầu|user story|feature", user_text, re.I):
        return
    # Tìm phần requirement trong user message để save
    try:
        save_requirements(user_text)
    except Exception:
        pass  # Không block chat nếu save lỗi


app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Test Cases endpoints
# ---------------------------------------------------------------------------

@app.get("/api/testcases")
async def list_testcases(_auth=Depends(require_api_key)):
    return get_all_testcases()

@app.get("/api/testcases/grouped")
async def testcases_grouped(_auth=Depends(require_api_key)):
    return get_tc_grouped()

@app.get("/api/testcases/stats")
async def testcases_stats(_auth=Depends(require_api_key)):
    return get_testcase_stats()

@app.get("/api/testcases/{tc_id}/history")
async def testcase_history(tc_id: str, _auth=Depends(require_api_key)):
    return get_tc_history(tc_id)

@app.delete("/api/testcases/{tc_id}")
async def delete_testcase(tc_id: str, _auth=Depends(require_api_key)):
    ok = deprecate_testcase(tc_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Test case không tồn tại")
    return {"message": f"{tc_id} deprecated"}

class SaveTCRequest(BaseModel):
    text: str

@app.post("/api/testcases/save")
async def save_tc_manual(req: SaveTCRequest, _auth=Depends(require_api_key)):
    """Lưu test cases từ text (manual paste)."""
    result = save_testcases(req.text, source_cap="MANUAL")
    if result["total_new"] == 0 and result["total_updated"] == 0:
        raise HTTPException(status_code=422, detail="Không tìm thấy test case nào (cần format TC_FEATURE_NNN)")
    return result


# ---------------------------------------------------------------------------
# Performance history endpoints
# ---------------------------------------------------------------------------

@app.get("/api/perf/history")
async def perf_history(
    limit: int = Query(100, ge=1, le=500),
    provider: Optional[str] = None,
    cap: Optional[str] = None,
    _auth=Depends(require_api_key),
):
    """Lịch sử các lần gọi AI, kèm token breakdown."""
    return get_history(limit=limit, provider=provider, cap=cap)


@app.get("/api/perf/compare")
async def perf_compare(_auth=Depends(require_api_key)):
    """So sánh cost / latency / token giữa các AI provider."""
    return get_provider_comparison()


@app.get("/api/perf/token-analysis")
async def perf_token_analysis(_auth=Depends(require_api_key)):
    """
    Phân tích nguồn gây tốn token:
    - % token từ system prompt
    - % token từ conversation history
    - % token từ user message
    - Top 10 request tốn nhất
    - Breakdown theo CAP
    """
    return get_token_breakdown_analysis()


# ---------------------------------------------------------------------------
# File / URL extract endpoint
# ---------------------------------------------------------------------------

from fastapi import UploadFile, File, Form

@app.post("/api/extract")
async def extract_content(
    file: Optional[UploadFile] = File(None),
    url:  Optional[str]        = Form(None),
    _auth=Depends(require_api_key),
):
    """
    Extract text từ nhiều nguồn:
    - .txt  → đọc thẳng
    - .pdf  → pypdf
    - .docx → python-docx
    - URL   → requests + BeautifulSoup
    - Image (.png/.jpg/.jpeg/.webp) → Gemini Vision
    """
    import io

    # ── URL ──────────────────────────────────────────────────────────────────
    if url and not file:
        try:
            import requests as _req
            from bs4 import BeautifulSoup
            resp = _req.get(url.strip(), timeout=10,
                            headers={"User-Agent": "Mozilla/5.0 QACopilot"})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            # Xóa script/style/nav/footer
            for tag in soup(["script","style","nav","footer","header","aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # Giới hạn ~8000 chars
            text = text[:8000]
            return {"text": text, "source": url, "type": "url", "chars": len(text)}
        except Exception as e:
            raise HTTPException(400, f"Không thể fetch URL: {e}")

    if not file:
        raise HTTPException(400, "Cần truyền file hoặc url")

    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    data = await file.read()

    # ── TXT ──────────────────────────────────────────────────────────────────
    if ext == "txt":
        try:
            text = data.decode("utf-8", errors="replace")
            return {"text": text[:8000], "source": filename, "type": "txt", "chars": len(text)}
        except Exception as e:
            raise HTTPException(400, f"Lỗi đọc txt: {e}")

    # ── PDF ──────────────────────────────────────────────────────────────────
    if ext == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages  = [p.extract_text() or "" for p in reader.pages]
            text   = "\n\n".join(pages)[:8000]
            return {"text": text, "source": filename, "type": "pdf",
                    "chars": len(text), "pages": len(reader.pages)}
        except Exception as e:
            raise HTTPException(400, f"Lỗi đọc PDF: {e}")

    # ── DOCX ─────────────────────────────────────────────────────────────────
    if ext == "docx":
        try:
            from docx import Document
            doc    = Document(io.BytesIO(data))
            text   = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            text   = text[:8000]
            return {"text": text, "source": filename, "type": "docx", "chars": len(text)}
        except Exception as e:
            raise HTTPException(400, f"Lỗi đọc DOCX: {e}")

    # ── IMAGE (Vision) ────────────────────────────────────────────────────────
    if ext in ("png", "jpg", "jpeg", "webp", "gif"):
        try:
            import base64
            from ai_provider import PROVIDER, MODEL
            img_b64   = base64.b64encode(data).decode()
            mime_map  = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "png": "image/png",  "webp": "image/webp", "gif": "image/gif"}
            mime_type = mime_map.get(ext, "image/png")

            vision_prompt = (
                "Đây là ảnh chứa requirement hoặc tài liệu. "
                "Hãy extract toàn bộ text có trong ảnh, giữ nguyên cấu trúc. "
                "Nếu là screenshot giao diện, mô tả các tính năng/yêu cầu bạn thấy. "
                "Trả về plain text."
            )

            if PROVIDER == "gemini":
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY",""))
                response = client.models.generate_content(
                    model=MODEL or "gemini-2.5-flash",
                    contents=[
                        types.Part.from_bytes(data=data, mime_type=mime_type),
                        vision_prompt,
                    ]
                )
                text = response.text or ""
            elif PROVIDER == "anthropic":
                import anthropic as _ant
                client = _ant.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
                msg = client.messages.create(
                    model=MODEL or "claude-sonnet-4-6",
                    max_tokens=2048,
                    messages=[{"role":"user","content":[
                        {"type":"image","source":{"type":"base64","media_type":mime_type,"data":img_b64}},
                        {"type":"text","text":vision_prompt},
                    ]}]
                )
                text = msg.content[0].text
            else:
                raise HTTPException(400, f"Provider '{PROVIDER}' chưa hỗ trợ Vision. Dùng gemini hoặc anthropic.")

            return {"text": text[:8000], "source": filename, "type": "image", "chars": len(text)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Lỗi xử lý ảnh: {e}")

    raise HTTPException(400, f"Định dạng '.{ext}' chưa được hỗ trợ. Dùng: txt, pdf, docx, png, jpg, jpeg, webp hoặc url.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    # [SEC-10/11] Default localhost; reload chỉ bật khi dev mode
    is_dev  = os.getenv("APP_ENV", "development") == "development"
    host    = os.getenv("APP_HOST", "127.0.0.1")   # production: set APP_HOST=0.0.0.0
    port    = int(os.getenv("APP_PORT", "8000"))
    print("=" * 60)
    print("QA Copilot Server")
    print("=" * 60)
    print(f"Mode:    {'development' if is_dev else 'production'}")
    print(f"Binding: {host}:{port}")
    info = get_provider_info()
    keys = {"anthropic": ANTHROPIC_API_KEY, "gemini": GEMINI_API_KEY, "openai": OPENAI_API_KEY}
    active_key = keys.get(info["provider"], "")
    print(f"Provider: {info['provider'].upper()} · model={info['model']}")
    print(f"API Key:  {'✅ set' if active_key else '❌ not set — điền vào .env'}")
    print(f"Auth:     {'APP_API_KEY set ✅' if APP_API_KEY else 'localhost-only (set APP_API_KEY for remote)'}")
    print(f"CORS:     {_ALLOWED_ORIGINS}")
    print("=" * 60)
    uvicorn.run("app:app", host=host, port=port, reload=is_dev)
