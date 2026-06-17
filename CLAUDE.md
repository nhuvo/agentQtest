# QA Copilot — Claude Code Rules

## README auto-update

Whenever you add, remove, or modify any of the following, you MUST update README.md in the same response:

- A Python file (`*.py`) — new file, deleted file, or significant logic change
- An API endpoint in `app.py` — new route, changed path, changed auth requirement
- A new environment variable (`.env` / `.env.example`)
- A new dependency in `requirements.txt`
- A new directory or significant structural change

**How to update**: edit only the affected sections in README.md — do not rewrite the whole file unless structure has changed significantly.

**Sections to keep in sync**:
- `Cấu trúc thư mục` — reflect added/removed files
- `API Endpoints` — reflect new/changed/removed routes
- `Cài đặt & Chạy` — reflect new install steps or env vars
- `Switching AI Provider` — reflect new providers or model changes
- `Tech Stack` — reflect new dependencies or tools
