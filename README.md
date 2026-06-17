# QA Copilot

QA Copilot là AI agent hỗ trợ QA Engineer trong toàn bộ vòng đời kiểm thử phần mềm — từ phân tích requirement đến viết test case, chạy API test và tạo bug report — hỗ trợ nhiều AI provider: Anthropic Claude, Google Gemini, OpenAI GPT.

---

## Tính năng

| Capability | Mô tả |
|---|---|
| **CAP-1** Read & Analyze Requirement | Phân tích requirement, gán ID (REQ-001...), flag ambiguity, tự động lưu vào DB |
| **CAP-2** Risk Analysis | Ma trận Impact × Likelihood, phân loại HIGH/MEDIUM/LOW |
| **CAP-3** Write Test Plan | Scope, strategy, entry/exit criteria, effort estimate |
| **CAP-4** Write Test Cases | TC_[FEATURE]_[NUMBER], đủ 4 loại: Happy Path / Negative / Boundary / Edge Case |
| **CAP-5** Run API Tests | Mock API test runner — chạy local, **0 token cost**, hỗ trợ **test chaining** (`{{TC_ID.field}}`) |
| **CAP-6** Requirement Synthesis | Diff version, impact analysis, regression risk |
| **CAP-7** Bug Report | BUG-[N], Jira-ready format, severity + root cause category |
| **CAP-8** Coverage Matrix | Traceability REQ ↔ TC ↔ Result, coverage % |
| **CAP-9** Export | Markdown, JSON, CSV, Jira, Confluence — chạy local, **0 token cost** |

---

## Kiến trúc

```
┌─────────────────────────────────────────────────────┐
│                Browser (index.html)                 │
│   Chat UI · Mock Test Runner · Requirements Tab     │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP / X-API-Key
┌──────────────────────▼──────────────────────────────┐
│                  app.py  (FastAPI)                  │
│                                                     │
│  classify_cap()  ──►  CAP-5  ──► mock_api_responses │
│                   ──►  CAP-9  ──► formatter (local) │
│                   ──►  other  ──► ai_provider.py    │
│                                      │              │
│  requirement_store.py  (SQLite)      ▼              │
│  performance_monitor.py (metrics)  Anthropic /      │
│                                    Gemini / OpenAI  │
└─────────────────────────────────────────────────────┘
```

**Smart routing** — CAP-5 và CAP-9 xử lý local (0 token). Các CAP còn lại gọi AI provider được cấu hình qua `.env`. Với Anthropic, **Smart Routing** tự chọn haiku (cheap) hoặc sonnet (capable) tùy độ phức tạp CAP.

---

## Cấu trúc thư mục

```
agentQtest/
├── app.py                  # FastAPI backend — routing, auth, rate limiting, APScheduler
├── ai_provider.py          # Multi-provider abstraction + Smart Routing (haiku/sonnet auto-select)
├── requirement_store.py    # SQLite storage cho requirements (versioning, grouping, diff)
├── testcase_store.py       # SQLite storage cho test cases (auto-save từ CAP-4)
├── skill_store.py          # SQLite storage cho Skill templates (reusable test collections)
├── cron_store.py           # SQLite storage cho Cron jobs + run logs
├── perf_store.py           # SQLite persistent performance log + token breakdown analysis
├── mock_api_responses.py   # Mock API engine cho CAP-5
├── performance_monitor.py  # In-memory session metrics (realtime panel)
├── requirements.txt        # Python dependencies
├── .env                    # Config thật (KHÔNG commit — đã có trong .gitignore)
├── .env.example            # Template cấu hình (commit được)
├── .gitignore
├── static/
│   └── index.html          # Web UI (Vanilla JS, no framework)
└── greennode-agentbase-skills/  # Deploy skill lên AgentBase
```

---

## Cài đặt & Chạy

### 1. Cài dependencies cơ bản

```bash
pip install -r requirements.txt
```

> `requirements.txt` chứa: fastapi, uvicorn, anthropic, python-multipart, python-dotenv

### 2. Cài thêm SDK cho provider khác (tùy chọn)

```bash
# Nếu dùng Google Gemini
pip install google-generativeai

# Nếu dùng OpenAI
pip install openai
```

### 3. Cấu hình `.env`

Copy file mẫu rồi điền thông tin thật:

```bash
cp .env.example .env
```

Chỉnh sửa `.env`:

```env
# ── Chọn AI provider ──────────────────────────────────
# Giá trị hợp lệ: anthropic | gemini | openai | qwen | gemma | minimax
AI_PROVIDER=anthropic

# ── Model override (để trống = dùng mặc định theo provider) ──
# AI_MODEL=

# ── API Keys — điền key của provider bạn dùng ────────
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza-...
OPENAI_API_KEY=sk-...

# ── GreenNode AgentBase (cho Qwen / Gemma / MiniMax) ─
GREENODE_API_KEY=vn-...
GREENNODE_BASE_URL=https://api.greennode.ai/v1

# ── App Security ──────────────────────────────────────
APP_API_KEY=                   # để trống = chỉ cho localhost
APP_ENV=development            # hoặc: production
APP_HOST=127.0.0.1             # production: 0.0.0.0
APP_PORT=8000

# ── CORS ──────────────────────────────────────────────
ALLOWED_ORIGINS=http://localhost:8000

# ── Rate Limiting ─────────────────────────────────────
RATE_LIMIT_PER_MIN=20
```

### 4. Khởi động server

```bash
python3 app.py
```

Mở trình duyệt: **http://localhost:8000**

---

### 5. Triển khai cho team (máy cá nhân làm server)

#### Bước 1 — Tìm IP máy bạn

```bash
# macOS
ipconfig getifaddr en0

# Linux
ip route get 1 | awk '{print $7}'
```

#### Bước 2 — Cập nhật `.env`

```env
APP_HOST=0.0.0.0          # lắng nghe tất cả interface
APP_PORT=8000
APP_API_KEY=your-secret   # team sẽ cần key này để truy cập
ALLOWED_ORIGINS=*         # cho phép mọi origin
```

#### Bước 3 — Mở firewall (macOS)

```bash
# Cho phép port 8000
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $(which python3)
```

Hoặc vào **System Settings → Network → Firewall → Options** → thêm `python3`.

#### Bước 4 — Khởi động

```bash
python3 app.py
```

#### Bước 5 — Team truy cập

1. Mở `http://<IP-của-bạn>:8000` trong browser
2. Nhập `APP_API_KEY` vào ô **🔑** góc trên phải — tự động lưu vào localStorage
3. Bắt đầu dùng

> **Lưu ý bảo mật**: `APP_API_KEY` chỉ cần nhập 1 lần mỗi máy. Không share key qua kênh không bảo mật. Nếu cần revoke, đổi key trong `.env` và restart server.

---

## Switching AI Provider

Chỉ cần đổi `AI_PROVIDER` trong `.env` rồi restart server:

| `.env` | Provider | Model mặc định | Giá (input / output per 1M tokens) |
|---|---|---|---|
| `AI_PROVIDER=anthropic` | Anthropic Claude | claude-sonnet-4-6 | $3.00 / $15.00 USD |
| `AI_PROVIDER=gemini` | Google Gemini | gemini-2.5-flash | $1.25 / $5.00 USD |
| `AI_PROVIDER=openai` | OpenAI GPT | gpt-4o | $5.00 / $15.00 USD |
| `AI_PROVIDER=qwen` | Qwen 3.5 27B (GreenNode MAAS) | qwen/qwen3-5-27b | 11,521 / 92,165 VND/1M tokens |
| `AI_PROVIDER=gemma` | Gemma 4 31B-IT (GreenNode MAAS) | google/gemma-4-31b-it | TBA |
| `AI_PROVIDER=minimax` | MiniMax M2.5 (GreenNode MAAS) | minimax/minimax-m2.5 | TBA |

> **GreenNode credits**: 1 credit = 1 VND. Thanh toán qua credits — xem số dư tại [GreenNode AI Platform](https://aiplatform.console.vngcloud.vn).

---

## Input Sources (Read Requirement)

Nhấn nút **📎** bên cạnh ô chat để đính kèm requirement từ nhiều nguồn:

| Nguồn | Định dạng | Ghi chú |
|---|---|---|
| Text | Paste trực tiếp | Mặc định |
| File văn bản | `.txt` | Đọc UTF-8 |
| PDF | `.pdf` | Extract text từ tất cả pages |
| Word | `.docx` | Extract paragraphs |
| URL / Link | `http(s)://...` | Scrape text, bỏ nav/footer/script |
| Ảnh / Screenshot | `.png` `.jpg` `.jpeg` `.webp` | Dùng AI Vision (Gemini / Claude) để đọc text trong ảnh |

Sau khi extract, text tự động điền vào ô chat — bạn chỉnh sửa rồi gửi như bình thường.

GreenNode models dùng chung `GREENODE_API_KEY` và `GREENNODE_BASE_URL` (OpenAI-compatible API).

Để override model cụ thể, thêm `AI_MODEL=<tên-model>` vào `.env`.

Provider đang active được hiển thị ở góc trên bên phải của Web UI.

---

## Requirements Management

Khi bạn nhập requirement vào chat (CAP-1), hệ thống tự động:

1. **Parse** — tách thành từng requirement riêng (hỗ trợ danh sách có số, bullet point, đoạn văn)
2. **Gán ID** — `REQ-001`, `REQ-002`, ...
3. **Gom nhóm** — theo custom feature groups (nếu đã tạo) hoặc built-in patterns
4. **Phát hiện ambiguity** — flag các từ mơ hồ: "nhanh", "dễ", "nhiều", ...
5. **Tự động sinh Acceptance Criteria** — Given/When/Then, 0 token cost
6. **Lưu vào SQLite** — tồn tại qua session resets, có versioning, dedup tự động

### Custom Feature Groups

Nhấn nút **🗂 Groups** trong tab Requirements để quản lý nhóm tính năng:

| Thao tác | Mô tả |
|---|---|
| Tạo group | Đặt tên + mô tả + keywords phân loại + màu sắc |
| Xóa group | Requirements trong group chuyển về "Unassigned" |
| ↺ Reclassify All | Phân loại lại tất cả requirements theo groups hiện tại |

**Ưu tiên phân loại:** custom groups → built-in regex → "General"

Nếu không có group nào match → requirement vào "Unassigned" để dễ phát hiện và bổ sung group mới.

### Xem requirements

- **Tab Requirements** trên Web UI — filter theo feature, search, xem version history
- **API** — `GET /api/requirements/grouped`

### Diff version

Nhập requirement mới vào tab Requirements → hệ thống so sánh với version hiện tại và trả về:
- `new`: requirement chưa có
- `changed`: đã thay đổi nội dung
- `unchanged`: không đổi
- `deprecated`: bị xóa trong version mới

Dữ liệu lưu trong `qa_copilot.db` (SQLite, không cần cài thêm phần mềm).

---

## API Endpoints

### Chat & Core

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `POST` | `/api/chat` | Chat với QA Agent | ✅ |
| `POST` | `/api/mock-test` | Chạy mock API test batch | ✅ |
| `GET` | `/api/provider` | Thông tin provider đang active | ✅ |
| `GET` | `/health` | Health check + provider status | ❌ |

### Performance

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `GET` | `/api/stats` | Metrics tổng hợp | ✅ |
| `GET` | `/api/stats/recent` | Recent interactions (`?count=10`) | ✅ |
| `GET` | `/api/stats/report` | Detailed performance report | ✅ |
| `POST` | `/api/stats/reset` | Reset session metrics | ✅ |

### Requirements

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `POST` | `/api/requirements/save` | Lưu / upsert requirements | ✅ |
| `GET` | `/api/requirements` | Lấy tất cả requirements | ✅ |
| `GET` | `/api/requirements/grouped` | Grouped by feature | ✅ |
| `GET` | `/api/requirements/stats` | Thống kê (total, by status, by feature) | ✅ |
| `POST` | `/api/requirements/diff` | So sánh text mới với version hiện tại | ✅ |
| `POST` | `/api/requirements/reclassify` | Phân loại lại tất cả requirements theo groups hiện tại | ✅ |
| `GET` | `/api/requirements/{req_id}` | Chi tiết 1 requirement | ✅ |
| `GET` | `/api/requirements/{req_id}/history` | Version history | ✅ |
| `DELETE` | `/api/requirements/{req_id}` | Deprecate requirement | ✅ |

### Feature Groups

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `GET` | `/api/feature-groups` | Lấy tất cả user-defined feature groups | ✅ |
| `POST` | `/api/feature-groups` | Tạo hoặc cập nhật feature group | ✅ |
| `PUT` | `/api/feature-groups/{name}` | Cập nhật group theo tên | ✅ |
| `DELETE` | `/api/feature-groups/{name}` | Xóa group (reqs → Unassigned) | ✅ |

### Skill Library

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `GET` | `/api/skills` | Lấy tất cả skills (sorted by used_count) | ✅ |
| `POST` | `/api/skills` | Tạo hoặc cập nhật skill từ test case list | ✅ |
| `GET` | `/api/skills/{id}` | Chi tiết skill (auto-increment used_count) | ✅ |
| `DELETE` | `/api/skills/{id}` | Xóa skill | ✅ |

### Cron Jobs

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `GET` | `/api/crons` | Lấy tất cả cron jobs | ✅ |
| `POST` | `/api/crons` | Tạo cron job mới (ngôn ngữ tự nhiên → cron expr) | ✅ |
| `PATCH` | `/api/crons/{id}/toggle` | Bật/tắt cron job | ✅ |
| `DELETE` | `/api/crons/{id}` | Xóa cron job + logs | ✅ |
| `GET` | `/api/crons/{id}/logs` | Lịch sử chạy (`?limit=20`) | ✅ |
| `POST` | `/api/crons/{id}/run` | Chạy ngay lập tức (không chờ lịch) | ✅ |

### AI Utilities

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `POST` | `/api/ai/generate-testcases` | AI sinh 4-6 test cases từ `req_id` hoặc `text` tự do | ✅ |

### Performance History

| Method | Endpoint | Mô tả | Auth |
|---|---|---|---|
| `GET` | `/api/perf/history` | Log toàn bộ request (`?limit=100&provider=gemini&cap=CAP-1`) | ✅ |
| `GET` | `/api/perf/compare` | So sánh cost/latency/token giữa các provider | ✅ |
| `GET` | `/api/perf/token-analysis` | Breakdown token theo nguồn (system/history/user) + top 10 tốn nhất | ✅ |

### Ví dụ gọi API

```bash
# Chat
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-secret-key" \
  -d '{"messages": [{"role": "user", "content": "Viết test case cho chức năng đăng nhập"}]}'

# Xem requirements grouped by feature
curl http://localhost:8000/api/requirements/grouped \
  -H "X-API-Key: your-secret-key"

# Xem provider đang dùng
curl http://localhost:8000/health
```

---

## Mock API Endpoints có sẵn (CAP-5)

| Method | Path | Status | Mô tả |
|---|---|---|---|
| `POST` | `/auth/login` | 200 | Đăng nhập — trả về `token`, `user_id` |
| `POST` | `/auth/login/invalid` | 401 | Sai credentials |
| `POST` | `/auth/logout` | 200 | Đăng xuất |
| `POST` | `/auth/refresh` | 200 | Refresh token |
| `GET` | `/users` | 200 | Danh sách users |
| `GET` | `/users/{id}` | 200 | Lấy user (id: uuid, số, hoặc slug như `usr_001`) |
| `POST` | `/users` | 201 | Tạo user |
| `PUT` | `/users/{id}` | 200 | Cập nhật user |
| `DELETE` | `/users/{id}` | 204 | Xóa user |
| `GET` | `/products` | 200 | Danh sách sản phẩm |
| `POST` | `/orders` | 201 | Tạo đơn hàng |
| `GET` | `/health` | 200 | Health check |
| `POST` | `/error/500` | 500 | Simulate lỗi server |
| `GET` | `/rate-limited` | 429 | Simulate rate limit |

### Test Chaining

CAP-5 hỗ trợ **truyền dữ liệu giữa các test cases**:

| Field | Mô tả | Ví dụ |
|---|---|---|
| `extract` | Lưu field từ response vào biến | `token=token`, `uid=user_id` |
| `depends_on` | Chặn nếu TC phụ thuộc bị FAIL | `depends_on: ["TC_CHAIN_1"]` |
| `{{TC_ID.var}}` | Template — thay thế tại runtime | `/users/{{TC_CHAIN_1.uid}}` → `/users/usr_001` |

**Kết quả trả về:**
- `extracted` — các biến đã extract từ response
- `chained_from` — danh sách TC đã depend_on
- `BLOCKED` — status khi dependency thất bại (cascade)

Nhấn **🔗 Load chain example** trong UI để load demo 5-step chain.

---

## Security

- **Authentication**: `X-API-Key` header (localhost bypass khi `APP_API_KEY` để trống)
- **CORS**: Restricted theo `ALLOWED_ORIGINS`
- **Rate limiting**: Per-IP, default 20 req/min (cấu hình qua `RATE_LIMIT_PER_MIN`)
- **Input validation**: Max 100 message turns, max 50 test cases/batch, max field length
- **XSS protection**: Tất cả server data được escape trước khi render ra HTML
- **Error handling**: Stack trace chỉ log server-side, client chỉ nhận message chung
- **Secret isolation**: API keys đọc từ `.env` (không hardcode), `.env` bị `.gitignore`

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| AI (default) | Anthropic Claude (claude-sonnet-4-6) |
| AI (optional) | Google Gemini (gemini-1.5-pro), OpenAI (gpt-4o) |
| Smart Routing | APScheduler 3.10+ (cron scheduling) + auto model-tier selection |
| Storage | SQLite (built-in Python, không cần cài thêm) |
| Frontend | Vanilla HTML/CSS/JS (no framework) |
| Config | python-dotenv (.env file) |
| Deploy | GreenNode AgentBase (via `greennode-agentbase-skills/`) |

---

## Skill Library & Cron Jobs

### Skill Library (Tuần 1)

Lưu bộ test case thành **template tái sử dụng**:

1. Tạo test case queue ở **Mock API Runner**
2. Chuyển sang tab **🧰 Skill Library** → nhấn **"Save Queue as Skill"**
3. Đặt tên, mô tả, category → **Lưu**
4. Sau đó load lại bằng nút **▶ Load** → queue tự động điền

### Cron Jobs (Tuần 3)

Lập lịch chạy Skill tự động bằng ngôn ngữ tự nhiên:

| Cú pháp | Cron expr | Giải thích |
|---|---|---|
| `mỗi giờ` / `hourly` | `0 * * * *` | Mỗi đầu giờ |
| `every day 9am` / `mỗi ngày 9h` | `0 9 * * *` | Hằng ngày lúc 9:00 |
| `mỗi thứ hai 8h` / `every Monday 8am` | `0 8 * * 1` | Thứ Hai hằng tuần |
| `weekly 10h` | `0 10 * * 1` | Thứ Hai hằng tuần lúc 10:00 |

**Lịch sử chạy**: mỗi lần cron job thực thi, kết quả (PASS/FAIL/ERROR + số TC) được lưu vào `cron_run_logs`. Xem qua nút **📋 Logs** trong UI.
