# Changelog

All entries in reverse chronological order. LOC = source lines (excluding tests/docs).

---

### `e334f7c` 2026-02-19 — feat: interactive web browsing, screenshots, location context
**Source: ~8,700 LOC | Tests: 380 | Tools: 16 (48 actions)**

Added:
- Interactive web_browse: 5 actions (browse, click, type, scroll, screenshot)
- Per-conversation browser sessions with auto-cleanup via try/finally
- ToolResult(text, media) type — tools can return images alongside text
- Claude provider: base64 image blocks in tool_result content
- Location context: `persona.location` config + `MICROPAW_LOCATION` env
- Setup wizard: optional location prompt
- `_image_block()` static method in claude.py — single source of truth for image encoding
- ROADMAP_WEB.md: web capabilities roadmap (Phase 1 done, Phase 2 planned)
- SKILLS.md support: `~/.micropaw/SKILLS.md` for multi-step workflow hints
- 4 new tests (location config x2, ToolResult with media, ToolResult backward compat)

Files: `web_browse.py`, `base.py` (tools), `agent.py`, `claude.py`, `config.py`, `setup_wizard.py`

---

### `6dcbca8` 2026-02-19 — refactor: deduplicate providers and channel streaming
**Source: ~8,500 LOC | Tests: 376 | Tools: 16**

Deduplicated:
- Provider template method: base.chat() → _build_body/_do_chat/_parse_response in subclasses
- Concrete close() in base with hasattr guard, removed from all 4 providers
- Removed retry_on_transient import from all 4 providers
- Channel stream_response() helper in channels/base.py with edit_fn/send_fn lambdas
- Replaced ~25-line streaming blocks in telegram/discord/slack with ~8-line calls

Files: `providers/base.py`, `claude.py`, `openai.py`, `gemini.py`, `ollama.py`,
`channels/base.py`, `telegram.py`, `discord.py`, `slack.py`

---

### `6287a96` 2026-02-19 — feat: contacts + files tools, deduplicate codebase, v0.1.2
**Source: ~8,400 LOC | Tests: 376 | Tools: 16**

Added:
- Contacts tool: SQLite-backed address book (add/search/list/update/delete)
- Files tool: persistent user files at `~/.micropaw/files/` (save/read/list/delete)
- New `contacts` table in SQLite schema + 5 CRUD methods on MemoryStore
- Path traversal security for files, case-insensitive contact search
- 29 new tests

Deduplicated:
- Shared `parse_sse()` SSE/NDJSON parser in providers/base.py (used by all 4 providers)
- Calendar date param helper `_add_date_params()`, list formatting helpers
- Email list formatting helper `_format_email_list()`
- Agent `_safe_typing()` helper, `_classify_error` dict lookup, SOUL/USER.md simplification
- Contacts `_contact_row()` dedup in store.py
- Removed unused `json` imports from gemini.py, ollama.py

Files: `contacts_tool.py` (new), `files_tool.py` (new), `store.py`, `cli.py`, `gateway.py`,
`base.py`, `claude.py`, `openai.py`, `gemini.py`, `ollama.py`, `agent.py`, `calendar_tool.py`, `email_tool.py`

---

### `5c4aef0` 2026-02-19 — feat: unified Google OAuth2, deduplicate codebase
**Source: ~8,200 LOC | Tests: 347 | Tools: 14** (+827/−819)

Added:
- Unified Google OAuth2 for Gmail + Calendar (single browser sign-in)
- Gmail REST API replaces IMAP/SMTP, shared `google_auth.py` module
- Channel base class absorbs handler boilerplate from 6 channels
- Shared `ensure_token()`, `api_error()`, API helpers in `google_auth.py`
- Gateway `_validate()` + `_cancel_task()` helpers
- Setup wizard `setattr/getattr` for API key assignment

Removed:
- ~138 lines of duplication across 18 files
- Separate `EmailConfig` + `CalendarConfig` → unified `GoogleConfig`

---

### `1537d8d` 2026-02-18 — docs: bump version to 0.1.1, enlarge badges
(+6/−6, docs only)

---

### `a4b2131` 2026-02-17 — feat: email/calendar tools, reminder broadcast
**Source: ~7,700 LOC | Tests: 334 | Tools: 14** (+1211/−19)

Added:
- Email tool: Gmail IMAP/SMTP (inbox, read, send, search)
- Calendar tool: Google Calendar REST API v3 with OAuth2
- Scheduler: broadcast reminders to ALL active channels
- `EmailConfig`, `CalendarConfig` with env var overrides

---

### `53a21f6` 2026-02-17 — fix: YouTube transcript + cross-channel memory
(+12/−3)

Fixed YouTube XML caption parsing (`<s>` segments), global memories across channels.

---

### `019f123` 2026-02-17 — fix: make memories global across all channels
(+13/−4)

Memory tool, RAG search, auto-ingest now use `_global` user_id. Conversations remain per-channel.

---

### `152df6c` 2026-02-17 — build: replace sentence-transformers with fastembed
**Source: ~7,100 LOC | Tests: 303** (+40/−30)

fastembed uses ONNX Runtime (~50MB) instead of PyTorch (~2GB). Same model, 3x faster on CPU.

---

### `573d5c4` 2026-02-17 — fix: resolve 5 runtime bugs
(+115/−54)

Embedding crash, sqlite-vec threading, gateway shutdown hang, terminal log noise, dead SearXNG.

---

### `ee5b711` 2026-02-17 — feat: cron scheduler, heartbeat, persona files
**Source: ~6,900 LOC | Tests: 303 | Tools: 12** (+991/−14)

Added:
- Cron scheduler: `paw cron add/list/remove` CLI + LLM tool
- Heartbeat mode: `~/.micropaw/HEARTBEAT.md` processed every 30min
- Persona files: `SOUL.md` + `USER.md` for personality/context
- 35 new tests

---

### `b3a8c76` 2026-02-17 — feat: sub-agent delegation, export, health monitoring
**Source: ~6,200 LOC | Tests: 125** (+541/−136)

Added:
- Sub-agent system with NO tool access (secure by design)
- Delegate tool: inline (blocking) + background (fire-and-forget via bus)
- `paw export` with JSON/Markdown output
- Channel health monitoring: auto-restart crashed Discord/Slack

---

### `19e4f32` 2026-02-17 — fix: 9 bugs (usage, SubAgent, deprecated APIs)
(+49/−64)

Usage double-counting, SubAgent Claude crash, deprecated `get_event_loop()`, WebChat blocking.

---

### `0e21b1f` 2026-02-17 — feat: v0.2.0 — Gemini streaming, env vars, caching
**Source: ~5,800 LOC | Tests: 209** (+1184/−15)

Added:
- Fix Gemini streaming usageMetadata extraction
- `CLAUDE_MODEL`/`OPENAI_MODEL`/`GEMINI_MODEL`/`OLLAMA_MODEL` env vars
- WebChat allowed_origins, per-message tool result caching
- 84 new tests

---

### `4f24eaf` 2026-02-17 — fix: v0.2.1 security hardening — 11 fixes
**Tests: 227** (+359/−43)

Critical: delegate chat_id crash, web_search URL crash, store corrupt JSON.
High: context fallback, HTTP redirect leak, shell blocking, bus closed detection.

---

### `fa21379` 2026-02-17 — feat: auto-detect embeddings, sub-agent tools
**Source: ~5,400 LOC | Tests: 238** (+398/−48)

Embedding auto-detect: probes Ollama → OpenAI → fastembed → disabled. Zero config.
Sub-agents inherit all tools except delegate. Max iterations configurable.

---

### `95f9f87` 2026-02-17 — feat: YouTube transcript extraction + RAG auto-ingest
**Source: ~5,100 LOC | Tests: 268** (+359/−12)

Raw httpx InnerTube API for YouTube transcripts. web_browse results auto-index for RAG.

---

### `b0902aa` 2026-02-17 — feat: sqlite-vec ANN search, dimension tracking
**Tests: 256** (+381/−32)

sqlite-vec for fast ANN search. Three-path: sqlite-vec → brute-force cosine → text LIKE.
Dual-write ingest, cascade delete, safe model switching with background re-index.

---

### `61d0d90` 2026-02-17 — feat: RAG out of the box
**Source: ~4,800 LOC | Tests: 249** (+368/−11)

Auto-ingest file/PDF reads → chunk → embed. Auto-retrieve on every message.
Top 5 chunks injected into system prompt. New `memory/chunker.py`.

---

### `65ab473` 2026-02-16 — feat: multi-backend web search
**Source: ~4,500 LOC | Tests: 116** (+443/−25)

SearXNG (5 instances, shuffled) → Brave Search (optional) → DuckDuckGo Lite fallback.

---

### `3e877f3` 2026-02-16 — fix: 5 bugs (channel blocking, SSRF, Slack streaming)
**Tests: 120** (+77/−11)

Discord/Slack no longer block gateway start. Case-insensitive SSRF hostname check.

---

### `380676a` 2026-02-16 — fix: security hardening, safe context compaction
**Tests: 108** (+140/−10)

Block `/etc/passwd`, `/etc/ssh/`. Context compaction preserves ALL history on failure.

---

### `b2993bf` 2026-02-16 — fix: channel reliability, usage tracking
(+91/−48)

Gateway reports channel failures. Terminal `/usage` wired to agent token counts.

---

### `e6326be` 2026-02-16 — feat: UX fixes, security hardening, docs update
**Tests: 101** (+537/−92)

Shell confirm_dangerous (16 patterns), terminal slash commands, per-tool disable, custom base_url.

---

### `64b9035` 2026-02-16 — feat: production hardening
**Tests: 95** (+249/−20)

Retry with backoff (429/503), rate limiting, HTTP client cleanup, message size validation, file logging.

---

### `fbbe222` 2026-02-16 — feat: `microclaw test` + improved channel setup
(+274/−50)

Live token validation for all channels. Discord invite URL, WhatsApp webhook summary.

---

### `3f90a9f` 2026-02-16 — fix: 10 bugs (crashes, security, correctness)
**Tests: 88** (+71/−21)

Streaming JSON crashes, OpenAI empty choices, Gemini functionResponse, Telegram overflow.

---

### `22439fb` 2026-02-16 — feat: guided channel connector
(+355/−11)

`microclaw connect` interactive setup for Telegram, Discord, Slack, WhatsApp, WebChat.

---

### `7fd0d50` 2026-02-16 — feat: terminal UX + local embeddings
(+80/−22)

Clean terminal (color prompts, markdown). sentence-transformers `BAAI/bge-small-en-v1.5`.

---

### `6e1bb54` 2026-02-16 — fix(security): patch 7 vulnerabilities
**Tests: 88** (+85/−6)

Shell env leak, subshell bypass, base64 pipe. WebSocket OOM/header-flood. WhatsApp timing attack.

---

### `5f2a66e` 2026-02-16 — feat: Phase 7 — Docker, tests, README
**Tests: 78** (+950/−8)

Dockerfile (multi-stage), docker-compose, .env.example, README, 78 unit tests.

---

### `87ecb60` 2026-02-16 — feat: MicroClaw v0.1.0 — complete implementation
**Source: ~4,500 LOC | Tests: 0 | Tools: 11** (+4667)

Initial implementation: 4 providers (Claude, OpenAI, Gemini, Ollama) via raw httpx.
6 channels (Terminal, WebChat, Telegram, Discord, Slack, WhatsApp).
11 tools, SQLite + vector memory, Pydantic config, SSRF protection, failover chain.

---

### `d479ee8` 2026-02-17 — rename: MicroClaw → MicroPaw
(+404/−404)

Name collision with existing `github.com/microclaw/microclaw` (Rust). All imports, config, CLI updated.

---

*Docs-only commits (badges, README rewrites, diagrams, PyPI prep) omitted for brevity.*
*Full git history: `git log --oneline --shortstat`*
