"""
Mock API Responses

WHAT: Simulates API responses for QA testing without hitting real endpoints
WHY:  Enables CAP-5 (Run Test Cases via API) to work offline/demo mode
HOW:  Pattern-match endpoint + method → return predefined response with latency simulation
"""

import time
import random
import json
import re
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Test chaining helpers
# ---------------------------------------------------------------------------

def _get_nested(obj: Any, path: str) -> Any:
    """
    Traverse nested dict/list using dot-notation.
    E.g. _get_nested({"user": {"id": 42}}, "user.id") → 42
    """
    for key in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


def _resolve(value: Any, ctx: Dict[str, Dict[str, Any]]) -> Any:
    """
    Replace {{TC_ID.field}} placeholders with extracted values from ctx.
    Works recursively on strings, dicts, lists.
    """
    if isinstance(value, str):
        def replacer(m):
            ref = m.group(1)          # e.g. "TC_AUTH_001.token"
            tc_id, _, field_path = ref.partition(".")
            extracted = ctx.get(tc_id, {})
            val = _get_nested(extracted, field_path) if field_path else extracted.get(tc_id)
            return str(val) if val is not None else m.group(0)
        return re.sub(r"\{\{([^}]+)\}\}", replacer, value)
    if isinstance(value, dict):
        return {k: _resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, ctx) for v in value]
    return value


def _extract(body: Dict[str, Any], extract_map: Dict[str, str]) -> Dict[str, Any]:
    """
    Extract fields from response body using extract_map.
    extract_map: {"var_name": "dot.path"} → {"var_name": actual_value}
    """
    return {var: _get_nested(body, path) for var, path in extract_map.items()}


class TestStatus(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    BLOCKED = "BLOCKED"
    SKIP    = "SKIP"


@dataclass
class MockResponse:
    status_code: int
    body: Dict[str, Any]
    headers: Dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    latency_ms: int = 120


@dataclass
class ValidationResult:
    status: TestStatus
    passed_checks: list
    failed_checks: list
    duration_ms: int
    request: Dict
    response: Dict
    diff: Optional[str] = None


# ---------------------------------------------------------------------------
# Mock response library — keyed by (METHOD, path_pattern)
# ---------------------------------------------------------------------------

_MOCK_DB: Dict[Tuple[str, str], MockResponse] = {
    # Auth
    ("POST", "/auth/login"): MockResponse(
        status_code=200,
        body={"token": "eyJhbGciOiJIUzI1NiJ9.mock", "expires_in": 3600, "user_id": "usr_001"},
        latency_ms=210,
    ),
    ("POST", "/auth/login/invalid"): MockResponse(
        status_code=401,
        body={"error": "invalid_credentials", "message": "Email hoặc mật khẩu không đúng"},
        latency_ms=180,
    ),
    ("POST", "/auth/logout"): MockResponse(
        status_code=200,
        body={"message": "Logged out successfully"},
        latency_ms=90,
    ),
    ("POST", "/auth/refresh"): MockResponse(
        status_code=200,
        body={"token": "eyJhbGciOiJIUzI1NiJ9.refreshed", "expires_in": 3600},
        latency_ms=150,
    ),

    # Users
    ("GET", "/users"): MockResponse(
        status_code=200,
        body={"data": [{"id": "usr_001", "email": "user@example.com", "role": "qa"}], "total": 1},
        latency_ms=130,
    ),
    ("GET", "/users/{id}"): MockResponse(
        status_code=200,
        body={"id": "usr_001", "email": "user@example.com", "name": "QA Engineer", "role": "qa", "created_at": "2024-01-01T00:00:00Z"},
        latency_ms=95,
    ),
    ("POST", "/users"): MockResponse(
        status_code=201,
        body={"id": "usr_002", "email": "new@example.com", "role": "viewer"},
        latency_ms=220,
    ),
    ("PUT", "/users/{id}"): MockResponse(
        status_code=200,
        body={"id": "usr_001", "email": "updated@example.com", "role": "qa"},
        latency_ms=180,
    ),
    ("DELETE", "/users/{id}"): MockResponse(
        status_code=204,
        body={},
        latency_ms=160,
    ),

    # Products
    ("GET", "/products"): MockResponse(
        status_code=200,
        body={"data": [{"id": "prod_001", "name": "Item A", "price": 99.99, "stock": 50}], "total": 1, "page": 1},
        latency_ms=145,
    ),
    ("GET", "/products/{id}"): MockResponse(
        status_code=200,
        body={"id": "prod_001", "name": "Item A", "price": 99.99, "stock": 50, "category": "electronics"},
        latency_ms=100,
    ),
    ("POST", "/products"): MockResponse(
        status_code=201,
        body={"id": "prod_002", "name": "New Product", "price": 49.99, "stock": 100},
        latency_ms=250,
    ),

    # Orders
    ("POST", "/orders"): MockResponse(
        status_code=201,
        body={"order_id": "ord_001", "status": "pending", "total": 199.98, "items": 2},
        latency_ms=310,
    ),
    ("GET", "/orders/{id}"): MockResponse(
        status_code=200,
        body={"order_id": "ord_001", "status": "processing", "total": 199.98, "payment_status": "paid"},
        latency_ms=140,
    ),
    ("PATCH", "/orders/{id}/status"): MockResponse(
        status_code=200,
        body={"order_id": "ord_001", "status": "shipped", "updated_at": "2024-06-14T10:00:00Z"},
        latency_ms=190,
    ),

    # Search
    ("GET", "/search"): MockResponse(
        status_code=200,
        body={"results": [], "total": 0, "query": "test"},
        latency_ms=320,
    ),

    # Health
    ("GET", "/health"): MockResponse(
        status_code=200,
        body={"status": "ok", "version": "1.0.0", "uptime": 99.9},
        latency_ms=30,
    ),

    # 404 fallback
    ("GET", "/not-found"): MockResponse(
        status_code=404,
        body={"error": "not_found", "message": "Resource không tồn tại"},
        latency_ms=80,
    ),

    # 500 error simulation — latency realistic nhưng không quá 1s
    ("POST", "/error/500"): MockResponse(
        status_code=500,
        body={"error": "internal_server_error", "message": "Lỗi server nội bộ"},
        latency_ms=350,
    ),

    # Rate limit simulation
    ("GET", "/rate-limited"): MockResponse(
        status_code=429,
        body={"error": "too_many_requests", "retry_after": 60},
        latency_ms=50,
        headers={"Content-Type": "application/json", "Retry-After": "60"},
    ),
}


def _normalize_path(path: str) -> str:
    """Replace path params like /users/123 or /users/usr_001 → /users/{id}."""
    import re
    path = re.sub(r"/[0-9a-f-]{8,}", "/{id}", path)          # UUID-style
    path = re.sub(r"/\d+", "/{id}", path)                     # numeric
    path = re.sub(r"/[a-zA-Z]+_[a-zA-Z0-9_-]+", "/{id}", path)  # slug like usr_001, prod_abc
    return path


def get_mock_response(method: str, path: str, jitter: bool = True) -> MockResponse:
    """
    Fetch a mock response for the given METHOD + path.
    Falls back to a generic 200 if no match found.
    """
    method = method.upper()
    normalized = _normalize_path(path)
    key = (method, normalized)

    resp = _MOCK_DB.get(key) or _MOCK_DB.get((method, path))

    if resp is None:
        # Generic fallback
        resp = MockResponse(
            status_code=200,
            body={"message": "mock response", "endpoint": path, "method": method},
            latency_ms=120,
        )

    # Add ±20% jitter to latency
    if jitter:
        jitter_ms = int(resp.latency_ms * random.uniform(0.8, 1.2))
        time.sleep(jitter_ms / 1000)
        return MockResponse(resp.status_code, resp.body, resp.headers, jitter_ms)

    time.sleep(resp.latency_ms / 1000)
    return resp


# ---------------------------------------------------------------------------
# Validator — compare actual response against expected spec
# ---------------------------------------------------------------------------

def validate_response(
    method: str,
    path: str,
    expected_status: int,
    expected_fields: Optional[Dict[str, Any]] = None,
    max_latency_ms: int = 2000,
    request_body: Optional[Dict] = None,
) -> ValidationResult:
    """
    Run a mock API test case and return a ValidationResult.

    Args:
        method:           HTTP method (GET, POST, ...)
        path:             Endpoint path
        expected_status:  Expected HTTP status code
        expected_fields:  Dict of field → expected_value to validate in response body
        max_latency_ms:   SLA threshold in milliseconds
        request_body:     Request payload (for logging)
    """
    start = time.time()
    mock = get_mock_response(method, path)
    duration_ms = int((time.time() - start) * 1000)

    passed, failed = [], []

    # 1. Status code check
    if mock.status_code == expected_status:
        passed.append(f"status_code == {expected_status} ✅")
    else:
        failed.append(f"status_code: expected {expected_status}, got {mock.status_code} ❌")

    # 2. Field value checks
    if expected_fields:
        for field_name, expected_val in expected_fields.items():
            actual_val = mock.body.get(field_name)
            if actual_val == expected_val:
                passed.append(f"{field_name} == {expected_val!r} ✅")
            else:
                failed.append(f"{field_name}: expected {expected_val!r}, got {actual_val!r} ❌")

    # 3. Response body not empty (when status < 400)
    if mock.status_code < 400 and not mock.body:
        failed.append("response body is empty ❌")
    elif mock.status_code < 400:
        passed.append("response body not empty ✅")

    # 4. Latency SLA
    if duration_ms <= max_latency_ms:
        passed.append(f"latency {duration_ms}ms ≤ {max_latency_ms}ms ✅")
    else:
        failed.append(f"latency {duration_ms}ms > {max_latency_ms}ms ❌")

    # Determine status
    if failed:
        status = TestStatus.FAIL
    else:
        status = TestStatus.PASS

    diff = None
    if failed:
        diff = "\n".join(failed)

    return ValidationResult(
        status=status,
        passed_checks=passed,
        failed_checks=failed,
        duration_ms=duration_ms,
        request={"method": method, "path": path, "body": request_body or {}},
        response={"status_code": mock.status_code, "body": mock.body},
        diff=diff,
    )


# ---------------------------------------------------------------------------
# Batch runner — run multiple test cases at once
# ---------------------------------------------------------------------------

def run_batch(test_cases: list) -> Dict[str, Any]:
    """
    Run a list of test case dicts and return aggregated report.

    Each test_case dict:
      { "tc_id", "method", "path", "expected_status", "expected_fields",
        "max_latency_ms", "body", "headers",
        "extract":    {"var": "dot.path"},       ← extract from response
        "depends_on": ["TC_ID_1", "TC_ID_2"] }  ← block if dependency failed
    Template syntax in path/body/headers/expected_fields: {{TC_ID.var}}
    """
    results   = []
    counts    = {s: 0 for s in TestStatus}
    ctx: Dict[str, Dict[str, Any]] = {}       # tc_id → extracted vars
    failed_ids: set = set()                   # track failed TCs for blocking

    for tc in test_cases:
        tc_id = tc.get("tc_id", "—")

        # ── BLOCKED: dependency failed ────────────────────────────────────
        deps = tc.get("depends_on") or []
        blocked_by = [d for d in deps if d in failed_ids]
        if blocked_by:
            counts[TestStatus.BLOCKED] += 1
            results.append({
                "tc_id":       tc_id,
                "endpoint":    f"{tc.get('method','GET')} {tc.get('path','/')}",
                "status":      TestStatus.BLOCKED,
                "duration_ms": 0,
                "passed":      [],
                "failed":      [f"Blocked — dependency failed: {', '.join(blocked_by)}"],
                "diff":        f"Blocked by: {', '.join(blocked_by)}",
                "extracted":   {},
                "chained_from": blocked_by,
            })
            failed_ids.add(tc_id)
            continue

        # ── Resolve {{TC_ID.var}} templates ──────────────────────────────
        resolved_path    = _resolve(tc.get("path", "/"), ctx)
        resolved_body    = _resolve(tc.get("body"), ctx)
        resolved_fields  = _resolve(tc.get("expected_fields"), ctx)

        result = validate_response(
            method=tc.get("method", "GET"),
            path=resolved_path,
            expected_status=tc.get("expected_status", 200),
            expected_fields=resolved_fields,
            max_latency_ms=tc.get("max_latency_ms", 2000),
            request_body=resolved_body,
        )

        # ── Extract vars from response for downstream TCs ─────────────────
        extracted = {}
        if tc.get("extract") and result.response.get("body"):
            extracted = _extract(result.response["body"], tc["extract"])
            ctx[tc_id] = extracted

        if result.status != TestStatus.PASS:
            failed_ids.add(tc_id)

        counts[result.status] += 1
        results.append({
            "tc_id":       tc_id,
            "endpoint":    f"{tc.get('method','GET')} {resolved_path}",
            "status":      result.status,
            "duration_ms": result.duration_ms,
            "passed":      result.passed_checks,
            "failed":      result.failed_checks,
            "diff":        result.diff,
            "extracted":   extracted,
            "chained_from": [d for d in deps if d not in failed_ids],
        })

    total = len(results)
    pass_rate = (counts[TestStatus.PASS] / total * 100) if total else 0
    slowest = max(results, key=lambda r: r["duration_ms"]) if results else None

    return {
        "summary": {
            "total": total,
            "passed": counts[TestStatus.PASS],
            "failed": counts[TestStatus.FAIL],
            "blocked": counts[TestStatus.BLOCKED],
            "skipped": counts[TestStatus.SKIP],
            "pass_rate": f"{pass_rate:.1f}%",
            "avg_duration_ms": int(sum(r["duration_ms"] for r in results) / total) if total else 0,
            "slowest_endpoint": f"{slowest['endpoint']} ({slowest['duration_ms']}ms)" if slowest else "—",
        },
        "results": results,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("MOCK API RESPONSES — DEMO")
    print("=" * 60)

    test_suite = [
        {"tc_id": "TC_AUTH_001", "method": "POST", "path": "/auth/login",
         "expected_status": 200, "expected_fields": {"expires_in": 3600}},
        {"tc_id": "TC_AUTH_002", "method": "POST", "path": "/auth/login/invalid",
         "expected_status": 401},
        {"tc_id": "TC_USER_001", "method": "GET", "path": "/users/123",
         "expected_status": 200, "expected_fields": {"role": "qa"}},
        {"tc_id": "TC_PROD_001", "method": "GET", "path": "/products",
         "expected_status": 200},
        {"tc_id": "TC_HEALTH_001", "method": "GET", "path": "/health",
         "expected_status": 200, "expected_fields": {"status": "ok"}, "max_latency_ms": 100},
        {"tc_id": "TC_ERR_001", "method": "POST", "path": "/error/500",
         "expected_status": 500, "max_latency_ms": 1000},  # negative test — expect 500
        {"tc_id": "TC_RATE_001", "method": "GET", "path": "/rate-limited",
         "expected_status": 429, "expected_fields": {"retry_after": 60}},  # rate limit test
    ]

    report = run_batch(test_suite)

    print(f"\nTotal: {report['summary']['total']} | "
          f"✅ {report['summary']['passed']} passed | "
          f"❌ {report['summary']['failed']} failed")
    print(f"Pass rate: {report['summary']['pass_rate']}")
    print(f"Avg latency: {report['summary']['avg_duration_ms']}ms")
    print(f"Slowest: {report['summary']['slowest_endpoint']}")
    print()
    for r in report["results"]:
        icon = "✅" if r["status"] == TestStatus.PASS else "❌"
        print(f"{icon} {r['tc_id']:20s} {r['endpoint']:30s} {r['duration_ms']}ms")
        if r["diff"]:
            print(f"   └─ {r['diff']}")
