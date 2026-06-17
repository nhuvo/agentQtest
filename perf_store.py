"""
Persistent Performance Store

WHAT: Lưu lịch sử mỗi lần gọi AI vào SQLite, kèm token breakdown chi tiết
WHY:  Giúp so sánh cost giữa các provider và tìm nguyên nhân tốn token
HOW:  Mỗi request → 1 row trong bảng `perf_log` với đủ breakdown
"""

import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional

DB_PATH = os.getenv("DB_PATH", "qa_copilot.db")

# Ước tính token từ số ký tự (quy tắc chung: ~4 chars = 1 token)
def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_perf_store():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS perf_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT    NOT NULL,
                provider         TEXT    NOT NULL,
                model            TEXT    NOT NULL,
                cap              TEXT    NOT NULL,

                -- Token breakdown (từ API response)
                input_tokens     INTEGER NOT NULL DEFAULT 0,
                output_tokens    INTEGER NOT NULL DEFAULT 0,
                total_tokens     INTEGER NOT NULL DEFAULT 0,

                -- Token breakdown ước tính (để phân tích nguồn gây tốn token)
                est_system_tokens    INTEGER NOT NULL DEFAULT 0,
                est_history_tokens   INTEGER NOT NULL DEFAULT 0,
                est_user_msg_tokens  INTEGER NOT NULL DEFAULT 0,
                history_turns        INTEGER NOT NULL DEFAULT 0,

                -- Pricing
                cost             REAL    NOT NULL DEFAULT 0.0,
                price_input_per_1m  REAL NOT NULL DEFAULT 0.0,
                price_output_per_1m REAL NOT NULL DEFAULT 0.0,

                -- Performance
                duration_ms      INTEGER NOT NULL DEFAULT 0,
                success          INTEGER NOT NULL DEFAULT 1,
                error_type       TEXT,

                -- Content preview
                prompt_preview   TEXT,
                response_len     INTEGER NOT NULL DEFAULT 0,

                -- Routing
                routed_local     INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_ts ON perf_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_provider ON perf_log(provider)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_perf_cap ON perf_log(cap)")


def log_request(
    provider: str,
    model: str,
    cap: str,
    system_prompt: str,
    messages: List[Dict],        # list of {"role": ..., "content": ...}
    input_tokens: int,
    output_tokens: int,
    cost: float,
    duration_ms: int,
    success: bool,
    error_type: Optional[str],
    response_text: str,
    routed_local: bool = False,
    price_input_per_1m: float = 0.0,
    price_output_per_1m: float = 0.0,
):
    # Tính token breakdown ước tính để phân tích
    est_system   = _estimate_tokens(system_prompt)
    history_msgs = messages[:-1] if len(messages) > 1 else []
    last_msg     = messages[-1]["content"] if messages else ""

    est_history  = sum(_estimate_tokens(m["content"]) for m in history_msgs)
    est_user_msg = _estimate_tokens(last_msg)
    prompt_preview = (last_msg[:120] + "…") if len(last_msg) > 120 else last_msg

    with _conn() as conn:
        conn.execute("""
            INSERT INTO perf_log (
                ts, provider, model, cap,
                input_tokens, output_tokens, total_tokens,
                est_system_tokens, est_history_tokens, est_user_msg_tokens, history_turns,
                cost, price_input_per_1m, price_output_per_1m,
                duration_ms, success, error_type,
                prompt_preview, response_len, routed_local
            ) VALUES (?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?, ?,?,?, ?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            provider, model, cap,
            input_tokens, output_tokens, input_tokens + output_tokens,
            est_system, est_history, est_user_msg, len(history_msgs),
            cost, price_input_per_1m, price_output_per_1m,
            duration_ms, int(success), error_type,
            prompt_preview, len(response_text), int(routed_local),
        ))


def get_history(limit: int = 100, provider: Optional[str] = None, cap: Optional[str] = None) -> List[Dict]:
    q = "SELECT * FROM perf_log WHERE 1=1"
    params = []
    if provider:
        q += " AND provider = ?"
        params.append(provider)
    if cap:
        q += " AND cap = ?"
        params.append(cap)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_provider_comparison() -> Dict:
    """Tổng hợp cost/token/latency theo từng provider — dùng để so sánh."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT
                provider, model,
                COUNT(*)                            AS total_runs,
                SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS successes,
                ROUND(AVG(input_tokens),  1)        AS avg_input_tok,
                ROUND(AVG(output_tokens), 1)        AS avg_output_tok,
                ROUND(AVG(total_tokens),  1)        AS avg_total_tok,
                ROUND(SUM(cost), 6)                 AS total_cost,
                ROUND(AVG(cost), 6)                 AS avg_cost,
                ROUND(AVG(duration_ms), 0)          AS avg_latency_ms,
                MIN(duration_ms)                    AS min_latency_ms,
                MAX(duration_ms)                    AS max_latency_ms
            FROM perf_log
            WHERE routed_local = 0
            GROUP BY provider, model
            ORDER BY total_cost DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_token_breakdown_analysis(limit: int = 50) -> Dict:
    """
    Phân tích nguồn gây tốn token theo từng request.
    Giúp tìm hiểu: system prompt / history / user message cái nào chiếm nhiều nhất.
    """
    with _conn() as conn:
        # Top 10 request tốn token nhất
        top_expensive = conn.execute("""
            SELECT ts, provider, cap, prompt_preview,
                   input_tokens, output_tokens,
                   est_system_tokens, est_history_tokens, est_user_msg_tokens,
                   history_turns, cost, duration_ms
            FROM perf_log
            WHERE routed_local = 0
            ORDER BY total_tokens DESC
            LIMIT 10
        """).fetchall()

        # Trung bình breakdown theo CAP
        by_cap = conn.execute("""
            SELECT cap,
                   COUNT(*) as runs,
                   ROUND(AVG(input_tokens),  0) as avg_input,
                   ROUND(AVG(output_tokens), 0) as avg_output,
                   ROUND(AVG(est_system_tokens),  0) as avg_est_system,
                   ROUND(AVG(est_history_tokens), 0) as avg_est_history,
                   ROUND(AVG(est_user_msg_tokens),0) as avg_est_user_msg,
                   ROUND(AVG(history_turns), 1)      as avg_history_turns,
                   ROUND(AVG(cost), 6) as avg_cost
            FROM perf_log
            WHERE routed_local = 0
            GROUP BY cap
            ORDER BY avg_input DESC
        """).fetchall()

        # Tổng toàn bộ
        totals = conn.execute("""
            SELECT
                COUNT(*) as total_runs,
                SUM(input_tokens)  as total_input_tok,
                SUM(output_tokens) as total_output_tok,
                ROUND(SUM(cost), 6) as total_cost,
                ROUND(AVG(est_system_tokens / CAST(NULLIF(input_tokens,0) AS REAL) * 100), 1) as pct_system,
                ROUND(AVG(est_history_tokens/ CAST(NULLIF(input_tokens,0) AS REAL) * 100), 1) as pct_history,
                ROUND(AVG(est_user_msg_tokens/CAST(NULLIF(input_tokens,0) AS REAL)* 100), 1) as pct_user_msg
            FROM perf_log
            WHERE routed_local = 0
        """).fetchone()

    return {
        "totals":        dict(totals) if totals else {},
        "by_cap":        [dict(r) for r in by_cap],
        "top_expensive": [dict(r) for r in top_expensive],
    }


# Init on import
init_perf_store()
