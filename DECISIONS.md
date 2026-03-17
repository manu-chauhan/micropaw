# MicroPaw — Decision Log & Timeline

## Project Genesis
**Date:** 2026-02-16
**Goal:** Build a "Goldilocks" personal AI assistant — feature-rich like OpenClaw, lightweight like PicoClaw, architecturally clean like NanoClaw.

## Research Phase (Pre-Implementation)

### Projects Analyzed
1. **OpenClaw** (TypeScript, ~200k LOC) — Feature set inspiration but identified bloatware to exclude
2. **PicoClaw** (Go, <10MB) — Minimal footprint philosophy, BYOK-first approach
3. **NanoClaw** (Python, user's first attempt) — Message bus architecture, raw WebSocket RFC 6455, auto-summarization
4. **Pi Agent** (Python, minimal) — Raw httpx providers (no SDKs), minimal system prompt, JSONL session persistence

### Key Decision: Name
- Considered: OpenPaw, NanoClaw, PicoClaw variants
- **Chose: MicroPaw** — Goldilocks between Pico (too small) and Open (too big)

## Architecture Decisions

### D1: Raw httpx for ALL providers (no SDKs)
**Decided:** 2026-02-16
**Rationale:** Inspired by Pi Agent. Using `anthropic`, `openai`, `google-genai` SDKs adds 3 heavy dependencies. All 4 providers (Claude, OpenAI, Gemini, Ollama) can be accessed via raw HTTP POST with httpx. SSE streaming is just line parsing.
**Impact:** Zero SDK dependencies. All providers are ~100 LOC each. Easier to debug and maintain.

### D2: Raw WebSocket (RFC 6455) for WebChat
**Decided:** 2026-02-16
**Rationale:** NanoClaw already has a working RFC 6455 implementation. Using aiohttp just for WebSocket is overkill. Raw asyncio TCP server + WebSocket handshake/framing is ~100 LOC.
**Impact:** No aiohttp dependency for webchat. WhatsApp still uses aiohttp for its webhook server (Meta requires HTTP).

### D3: Message Bus as optional coordination layer
**Decided:** 2026-02-16, **Updated:** 2026-02-17
**Rationale:** NanoClaw's bus architecture is clean but adds complexity for v1. MicroPaw keeps the handler callback pattern (simpler, proven) for request-response messages. Bus wired in v1.1 for background sub-agent result delivery (see D17).
**Impact:** Channels use direct handler callbacks for real-time messages. Bus handles async delivery (background sub-agent results). Hybrid approach: callbacks for streaming, bus for fire-and-forget.

### D4: Playwright for browser automation (not CDP)
**Decided:** 2026-02-16
**Rationale:** User confirmed browser automation is a selling point. Playwright gives full browser control (navigate, click, fill, screenshot, JS eval) without requiring Chrome extension + daemon like OpenClaw's CDP approach. Cross-browser (Chrome, Firefox, WebKit).
**Impact:** `web_browse` tool uses Playwright. Optional dependency: `pip install micropaw[browser]`

### D5: Shell completion via Click (not custom)
**Decided:** 2026-02-16
**Rationale:** Click has built-in shell completion. OpenClaw's ~500 LOC custom completion for 4 shells is unnecessary when Click handles bash/zsh/fish natively.
**Impact:** Zero additional code. Users add one line to their shell config.

### D6: A2UI framework deferred to v2
**Decided:** 2026-02-16
**Rationale:** Agent-generated UIs are cool but need the WebSocket infrastructure (v1) as foundation. Text responses cover 99% of use cases. Good v2 feature.
**Impact:** Not in v1. Noted in future roadmap.

### D7: No daemon services
**Decided:** 2026-02-16
**Rationale:** ~2K LOC for 3 OSes (systemd/launchd/Windows). Users can trivially daemonize with `systemd`, `supervisord`, or `docker compose up -d`.
**Impact:** Users manage their own process lifecycle.

### D8: No ACP protocol
**Decided:** 2026-02-16
**Rationale:** ACP is OpenClaw's custom NDJSON-based RPC for native client apps. MicroPaw uses standard WebSocket — any client (web, mobile, CLI) can connect directly.
**Impact:** Standard protocols only. No custom wire format.

### D9: No OAuth token management
**Decided:** 2026-02-16
**Rationale:** MicroPaw uses BYOK (bring your own keys). API keys don't expire like OAuth tokens. Invalid key → 401 → failover chain tries next provider.
**Impact:** Simple API key auth. No token refresh complexity.

### D10: No wake-word detection
**Decided:** 2026-02-16
**Rationale:** macOS-only niche feature requiring a background daemon with local speech recognition. Chat-first design. Voice messages from Telegram/Discord work without this.
**Impact:** No voice activation. Chat input only.

### D11: Embedded SQLite schema (no external files)
**Decided:** 2026-02-16
**Rationale:** OpenPaw uses a separate schema.sql file loaded via importlib.resources. MicroPaw embeds the schema as a Python constant in store.py — simpler, no file loading issues, schema version is always in sync with code.
**Impact:** One less file. Schema changes are code changes.

### D12: Single Pydantic config model
**Decided:** 2026-02-16
**Rationale:** Proven pattern from OpenPaw. One Config class replaces OpenClaw's 126 config files. Env var overrides via a clean mapping table instead of ad-hoc os.getenv() calls.
**Impact:** `~/.micropaw/config.json` with 0o600 permissions. All secrets in one place.

### D13: Multi-backend web search with automatic failover
**Decided:** 2026-02-16
**Rationale:** DuckDuckGo Instant Answer API was the only search backend — limited to instant answers, no full web results. SearXNG is a free, open-source metasearch engine that aggregates results from Google/Bing/DDG. Public instances support JSON API with no API key needed. Brave Search has the best quality (independent 30B+ page index) but requires an API key.
**Design:** Three-tier fallback chain:
1. **Brave** (if API key configured) — best quality, ~1,000 free queries/month
2. **SearXNG** (default) — 5 public instances, shuffle and try up to 3 with 8s timeout each
3. **DuckDuckGo** (last resort) — always available, lowest quality
**Config:** `SearchConfig` with `provider` (auto/searxng/brave/duckduckgo), `brave_api_key`, `searxng_url` (for self-hosted). Env vars: `BRAVE_API_KEY`, `SEARXNG_URL`.
**Impact:** Zero-config web search that actually works. Users can optionally upgrade to Brave for best results or self-host SearXNG for full control.

## Implementation Timeline

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1: Foundation | pyproject.toml, config, base types, bus, security, logger | ✅ Complete |
| Phase 2: Providers | Claude, OpenAI, Gemini, Ollama — all via httpx + failover | ✅ Complete |
| Phase 3: Agent Core | Agent loop, context compaction, conversation, memory store | ✅ Complete |
| Phase 4: Tools | All 14 tools (file, shell, web, memory, vision, pdf, sessions, delegate, cron, email, calendar) | ✅ Complete |
| Phase 5: Channels | Terminal, WebChat, Telegram, Discord, Slack, WhatsApp | ✅ Complete |
| Phase 6: Gateway + CLI | Central coordinator, Click CLI, setup wizard | ✅ Complete |
| Phase 7: Polish | Docker, tests, .env.example, README | ✅ Complete |

## Phase 7 Deliverables
- **Dockerfile** — Multi-stage: base (core), browser (+ Playwright/Chromium), full (all extras)
- **docker-compose.yml** — 3 service profiles (base, browser, full) with named volume
- **.env.example** — All supported environment variables documented
- **README.md** — Features, quickstart, installation, Docker, architecture, CLI reference
- **125 unit tests** — Config, bus, security, tools, providers, embeddings, sub-agent, delegate — all passing

## Post-v1 Hardening

### Bug Fixes (Batch 1)
- Fixed broad `except Exception: pass` in channels — now logs errors properly
- Fixed `chunk_text()` duplication — moved to `channels/base.py`
- Added missing `google-genai` and `pypdf` to dependency list (now using raw httpx anyway)
- Fixed vector search loading all memories — added pagination
- Fixed Ollama vision — base64 images now sent in the `images` field
- Fixed `MICROPAW_PROVIDER` env var override — was missing from mapping table
- Fixed SSRF DNS resolution bypass — DNS failure now blocks instead of passing through
- Fixed `confirm_dangerous` config — was defined but never wired to the shell tool

### Security Hardening (Batch 1)
- WebSocket: 1MB frame limit, 50 max headers, timing-safe auth
- Shell: 40+ blocked patterns, dangerous command confirmation
- SSRF: DNS rebinding check, private IP ranges blocked
- Config: 0o600 file permissions
- Path: traversal prevention, sensitive file blocking

### Production Hardening
- **Retry with backoff** — `retry_on_transient()` retries 429/503 with exponential backoff + Retry-After header support
- **Rate limiting** — Per-user sliding window + burst detection at gateway level
- **HTTP client cleanup** — All providers and embedding providers implement `close()` for proper connection pool teardown
- **File logging** — RotatingFileHandler (5MB, 3 backups) via `MICROPAW_FILE_LOG` or `MICROPAW_LOG_FILE` env vars
- **SQLite busy_timeout** — 5-second timeout prevents "database is locked" under concurrent access
- **Message size validation** — Configurable max (100KB default) prevents memory exhaustion

### UX Improvements
- **Terminal slash commands** — `/help`, `/new`, `/tools`, `/usage`, `/clear`, `/config` in the REPL
- **Tool call visibility** — Agent tool calls logged at INFO level so users see what tools are being used
- **Per-tool enable/disable** — `disabled_tools` list in permissions config
- **Custom API base URLs** — `base_url` field on Claude, OpenAI, Gemini providers for OpenAI-compatible servers
- **Actionable error messages** — `_classify_error()` maps provider errors to specific hints (invalid key → run setup, timeout → try faster model, etc.)
- **Setup wizard improvements** — Ollama validation + model picker, embedding setup, fallback provider key prompting
- **Conversation reset** — `/new` generates fresh conversation ID in terminal

### Bug Fixes (Batch 2)
- **Critical:** `agent.last_usage` never reset between messages — `/usage` double-counted every prior message's tokens
- **Critical:** SubAgent nudge used empty assistant content (`""`) — Claude API rejects this with 400. Fixed to non-empty nudge
- `asyncio.get_event_loop()` → `asyncio.get_running_loop()` in terminal and embeddings (deprecated in Python 3.12+)
- WebChat `_process_message` blocked the frame loop — client couldn't ping during AI thinking. Now uses `asyncio.create_task()`
- WhatsApp channel missing helpful `"Install with: pip install micropaw[whatsapp]"` on ImportError
- `load_config` silently swallowed corrupt JSON — now warns via logger
- `memory_tool.delete` returned "Memory deleted" for nonexistent keys — now returns "Memory not found"
- `sessions.py` bypassed store abstraction with raw SQL — now uses `store.list_conversations()` and `store.export_messages()`
- Gateway accessed `conversations._terminal_override` private attribute — added public `reset_terminal_session()` method

## File Count & LOC (actual, as of v0.2.1)
- Source files: 59 Python files (48 source + 11 test)
- Source LOC: ~6,240
- Test LOC: ~2,450
- Tests: 227 passing
- Dependencies: 7 core, 7 optional extras

### Web Search Enhancement
- **SearXNG as default** — free metasearch aggregator, 5 public instances with shuffled load balancing
- **Brave Search** — optional best-quality backend with API key
- **Smart fallback** — Brave (if key) → SearXNG (3 tries) → DuckDuckGo (last resort)
- **Zero config** — works out of the box, no API key needed
- **Self-hosted SearXNG** — `SEARXNG_URL` env var or config for self-hosted instances

### D14: Channel Health Monitoring
**Decided:** 2026-02-17
**Rationale:** Discord and Slack channels run as `asyncio.create_task()` — if the task crashes after startup, the gateway doesn't know. Telegram and python-telegram-bot already auto-reconnect natively.
**Design:** `_watch_task()` coroutine pattern: await the background task, on crash log + sleep 5s + restart. Discord adds `on_disconnect`/`on_resumed` events for visibility.
**Impact:** Self-healing channels — crash detection + automatic restart without gateway awareness needed.

### D15: Conversation Export
**Decided:** 2026-02-17
**Rationale:** No way to export chat history from the SQLite database. Users need this for backup, audit, or sharing.
**Design:** `micropaw export` CLI command. No conversation ID → lists all conversations in a Rich table. With `--conversation-id` + `--format` (json/markdown) + `--output` (file/stdout). Read-only SQLite, own MemoryStore connection.
**Impact:** Full conversation export with zero dependencies.

### D16: Sub-Agent System
**Decided:** 2026-02-17
**Rationale:** Main agent should be able to delegate sub-tasks to independent LLM loops. Inspired by NanoClaw's sub-agent pattern. Security: sub-agents have NO tool access (tools=None always) — physically cannot read files, execute shell, or access memory.
**Design:** `SubAgent(provider, system_prompt)` with `run(task) -> str`. Max 10 iterations, nudge on empty response. `DelegateTool` with inline (blocking) and background (fire-and-forget via bus) modes.
**Impact:** Multi-agent delegation. Sub-agents capped at 10 iterations. Bus bounded queues (maxsize=100) prevent memory exhaustion.

### D17: Message Bus Wiring
**Decided:** 2026-02-17
**Rationale:** Bus module existed but was unwired (D3). Now wired specifically for background sub-agent result delivery — NOT replacing handler callbacks for regular messages.
**Design:** Gateway creates MessageBus, injects via `set_delegate_deps()`. Background `_bus_consumer()` task consumes outbound messages and routes to appropriate channel's `send_message()`. Cleaned up in `stop()`.
**Impact:** Background sub-agent results delivered to users via their original channel.

---

## v0.2.0 Roadmap

**Decided:** 2026-02-17
**Goal:** Harden, complete, and polish. No new features — fix gaps, expand coverage, unlock config flexibility.

| # | Feature | Status |
|---|---------|--------|
| 1 | Test coverage expansion (125 → 209 tests) | ✅ Complete |
| 2 | Gemini streaming token tracking | ✅ Complete |
| 3 | Model env var overrides | ✅ Complete |
| 4 | Wire `allowed_origins` into WebChat | ✅ Complete |
| 5 | Smarter context compaction (summarize old messages) | ✅ Already implemented in v0.1.0 |
| 6 | Tool result caching | ✅ Complete |
| 7 | Streaming for Telegram/Discord/Slack | ✅ Already implemented in v0.1.0 |

### D18: Test Coverage Expansion
**Decided:** 2026-02-17
**Rationale:** 125 tests cover config, bus, security, tools, providers (mocked), embeddings, sub-agent, delegate. But agent loop, all 6 channels, provider HTTP calls, context compaction, and most tools have zero test coverage. ~30% estimated coverage.
**Goal:** Reach 70%+ coverage. Prioritize: agent loop (handle_message, tool call loop, error classification), provider response parsing, channel message routing, context compaction, gateway message handling.
**Approach:** Mock httpx responses and asyncio infrastructure. No integration tests yet — unit tests only. Use `pytest-asyncio` for async tests.

### D19: Gemini Streaming Token Tracking
**Decided:** 2026-02-17
**Rationale:** Gemini streaming yields `usage={}` — token tracking shows 0 for Gemini. The Gemini streaming API returns usage metadata in the final chunk's `usageMetadata` field, but the provider's SSE parser ignores it.
**Fix:** Parse `usageMetadata` from the final streaming chunk (`promptTokenCount`, `candidatesTokenCount`). Map to standard `{input, output}` format.

### D20: Model Env Var Overrides
**Decided:** 2026-02-17
**Rationale:** Users can override provider and API keys via env vars (`ANTHROPIC_API_KEY`, etc.) but not the model. Switching models requires editing `~/.micropaw/config.json`. For quick testing or CI, env var overrides are cleaner.
**Design:** Add to env var mapping table in `config.py`:
- `CLAUDE_MODEL` → `provider_config.claude.model`
- `OPENAI_MODEL` → `provider_config.openai.model`
- `GEMINI_MODEL` → `provider_config.gemini.model`
- `OLLAMA_MODEL` → `provider_config.ollama.model`

### D21: Wire `allowed_origins` into WebChat
**Decided:** 2026-02-17
**Rationale:** `WebChatConfig.allowed_origins` exists in the Pydantic config model but the WebChat channel never checks it during the WebSocket handshake. Any origin can connect — security gap for public deployments.
**Fix:** During WebSocket upgrade, parse the `Origin` header and reject connections not in `allowed_origins` (when the list is non-empty). Empty list = allow all (default, backwards-compatible).

### D22: Smarter Context Compaction
**Decided:** 2026-02-17
**Status:** Already implemented in v0.1.0 (`core/context.py`). The `ContextManager.compact()` method already summarizes old messages via the LLM and preserves the last 10 messages intact. Falls back to keeping full history if summarization fails or returns empty. No changes needed.

### D23: Tool Result Caching
**Decided:** 2026-02-17
**Rationale:** Within a single conversation turn, the agent may call `web_search` with the same query multiple times, or `file_read` on the same path. Each call burns tokens and adds latency. Caching identical tool calls within a conversation avoids this waste.
**Design:** Per-conversation LRU cache (maxsize=32) keyed on `(tool_name, sorted_args_tuple)`. TTL: `web_search`/`web_browse`/`http_request` = 5 minutes, `file_read` = 2 minutes, `memory`/`sessions` = 0 (never cache). Cache cleared on `/new`. Implemented in the agent's tool dispatch loop — no changes to individual tools.
**Constraint:** Only cache read-only tools. Tools with side effects (`file_write`, `shell`, `memory` save/delete, `delegate`) are never cached.

### D24: Streaming for Telegram/Discord/Slack
**Decided:** 2026-02-17
**Status:** Already implemented in v0.1.0. All three channels already have streaming with placeholder message → `on_chunk` closure → throttled `edit_message` → final edit with full response. Throttle intervals: Telegram 0.5s, Discord 0.8s, Slack 1.0s. No changes needed.

---

## v0.2.1 Security Hardening

**Decided:** 2026-02-17
**Goal:** Comprehensive security audit and fix pass. 11 fixes across 11 files, 227 tests.

### Bug Fixes (Batch 3 — Security Hardening)

**Critical:**
- `delegate.py` — background sub-agent parsed channel from `chat_id.split(":")[0]`; crashes if conversation_id contains colons. Now passes channel name directly.
- `web_search.py` — SearXNG source name extracted via `url.split("//")[1]`; crashes on malformed URLs. Replaced with `urlparse().hostname`.
- `store.py` — `json.loads(tool_calls_json)` in `get_messages()` crashed on corrupt data, losing the entire conversation. Now catches `JSONDecodeError`/`KeyError`/`TypeError` and returns empty tool_calls for that message.

**High:**
- `context.py` — compaction returned full history when summarization failed, allowing unbounded context growth. Now falls back to truncation (system + last KEEP_RECENT messages).
- `http_request.py` — redirect following leaked `Authorization` header to cross-origin hosts. Now strips sensitive headers when redirect target has a different netloc.
- `shell.py` — blocking patterns missed `base64|sh` (no space), `xxd|sh`, process substitution `<(...)`. Added 4 new patterns.
- `bus.py` — `consume_inbound()`/`consume_outbound()` spun at 1Hz on closed bus until timeout. Now check `_closed` flag before blocking.

**Medium:**
- `config.py` — no warning when `config.json` is world-readable (contains API keys). Now logs a warning with `chmod 600` instructions.
- `web_search.py` — `max_results` parameter unclamped; LLM could request 1000 results. Clamped to 20.
- `webchat.py` — outbound WebSocket frames not validated against `MAX_FRAME_SIZE`. Now truncates oversized payloads.
- `pdf_read.py` — silently returned empty content when all requested pages were out of bounds. Now returns an explicit error message.

---

## v0.2.2 — RAG + sqlite-vec + YouTube Transcripts

**Decided:** 2026-02-17

### D25: RAG Pipeline (Auto-Ingest, Auto-Retrieve, Auto-Inject)
**Rationale:** Users read files and PDFs but can't ask about them later. Manual knowledge management is tedious.
**Design:** Three-stage automatic pipeline: (1) Auto-ingest — file_read/pdf_read results >500 chars are chunked (~2k chars, 200 overlap) and embedded in background via `asyncio.create_task()`. (2) Auto-retrieve — every user message is embedded and searched against stored chunks. (3) Auto-inject — top 5 relevant chunks injected into system prompt.
**Impact:** Zero-config RAG. No user action needed. Cross-platform (read on Telegram, ask on Discord).

### D26: sqlite-vec for ANN Vector Search
**Rationale:** Brute-force cosine similarity over all chunks is O(n) and degrades beyond ~2000 chunks. sqlite-vec provides ANN search directly inside SQLite with no separate vector database.
**Design:** Required dependency. `vec_chunks` virtual table with cosine distance metric. Three-path search: sqlite-vec ANN → brute-force cosine fallback → text LIKE fallback. `kv_meta` table tracks embedding dimensions. On dimension change: clear embeddings (derived data), preserve text (source of truth), background re-embed.
**Impact:** Scales to tens of thousands of chunks. Graceful fallback if sqlite-vec removed.

### D27: YouTube Transcript Extraction
**Rationale:** Users paste YouTube URLs expecting the agent to understand the video. Playwright renders YouTube pages as UI garbage (nav, comments, cookie banners). The actual transcript is hidden behind a button click.
**Design:** Smart detection in `web_browse` — regex detects YouTube URLs, fetches transcript via YouTube's InnerTube API (raw httpx, ANDROID client context). Parses XML captions (both `<p>` manual and `<text>` auto-generated formats). Falls back to Playwright if transcript unavailable.
**Impact:** Zero extra dependencies. ~100 LOC added to web_browse.py. Works on residential IPs (aligns with local-first philosophy). Transcripts auto-ingest into RAG pipeline.

### D28: Web Browse Auto-Ingest
**Rationale:** RAG auto-ingest only triggered on file_read/pdf_read. Web content (articles, YouTube transcripts) is the most common thing users want to reference later.
**Design:** Add `web_browse` to `_INGEST_TOOLS` in agent.py. Source key uses `url` arg instead of `path` arg.
**Impact:** 1-line change. Browsed pages and YouTube transcripts now auto-index for RAG.

### Metrics (v0.2.2)
- Source LOC: ~6,873
- Test LOC: ~3,101
- Tests: 268 passing
- Dependencies: 8 core (added sqlite-vec), 7 optional extras

---

## v0.3.0 — Scheduler, Persona, and Runtime Fixes

**Decided:** 2026-02-17

### D29: Cron/Scheduled Tasks
**Rationale:** Gap analysis vs PicoClaw/MimiClaw showed both have cron scheduling. MicroPaw had no way to run recurring or delayed tasks.
**Design:** Interval-based (not crontab syntax — no `croniter` dependency). Both recurring and one-shot jobs. Dual interface: CLI (`paw cron add/list/remove`) and LLM tool (`cron` with action parameter). Single SQLite table `cron_jobs` in existing DB. Scheduler is an async loop inside Gateway alongside the bus consumer, checks DB every 60s. 50 job max to prevent runaway scheduling.
**New files:** `scheduler.py` (~60 LOC), `tools/cron_tool.py` (~30 LOC)
**Modified:** `gateway.py` (start/stop scheduler), `cli.py` (cron subgroup + CronTool registration), `config.py` (SchedulerConfig)

### D30: HEARTBEAT.md Proactive Mode
**Rationale:** PicoClaw and MimiClaw both have proactive task execution. Users want the agent to do things without being asked.
**Design:** Built into scheduler (not a DB cron job). Reads `~/.micropaw/HEARTBEAT.md` every 30min (configurable). Finds unchecked `- [ ]` items, sends each through the agent. Delivery: terminal first, then first active channel. Config: `scheduler.heartbeat_interval` (default 1800s).
**Impact:** ~20 LOC added to scheduler.py. Zero new files or dependencies.

### D31: Structured Persona Files (SOUL.md / USER.md)
**Rationale:** Both PicoClaw and MimiClaw support structured persona customization. MicroPaw only had `PersonaConfig` with a single personality string.
**Design:** Optional files in `~/.micropaw/`: `SOUL.md` replaces default personality if present, `USER.md` injects "About the user:" context. 2KB cap each. Read on every message (allows live editing). File presence is the config — no config changes needed.
**Modified:** `core/agent.py` (`_build_system_prompt()`)
**Impact:** ~15 LOC. No new files, no config changes, no dependencies.

### Bug Fixes (Batch 4 — Runtime Testing)

1. **Embedding provider crash** — `_create_explicit_provider()` created `LocalEmbedding` without checking if sentence-transformers was installed. Setup wizard offered "local" without checking. Fix: import check with NullEmbedding fallback in gateway.py, `[NOT INSTALLED]` label in setup wizard.

2. **sqlite-vec threading error** — `_try_load_vec()` accessed `self._db._conn` from the main thread but aiosqlite creates it on a background thread. Fix: run extension loading via `self._db._execute()` on aiosqlite's internal thread.

3. **Gateway shutdown hang** — `channel.stop()` for Telegram blocked indefinitely during event loop teardown. Fix: `asyncio.wait_for(timeout=5.0)` on all channel/provider/embedder cleanup.

4. **Terminal log noise** — INFO log lines interleaved with chat conversation output. Fix: `quiet_console()` suppresses console handler to WARNING+ during chat/start. File logging always enabled at `~/.micropaw/logs/micropaw.log`.

5. **Web search returning empty/generic answers** — All 5 SearXNG public instances dead (429/403). DDG Instant Answer API only returns knowledge base lookups, not web results. Fix: replaced with DDG Lite HTML parser that returns real search results with titles, URLs, and snippets.

### D32: Replace sentence-transformers with fastembed
**Decided:** 2026-02-17
**Rationale:** sentence-transformers pulls in PyTorch (~2GB installed). This conflicts with MicroPaw's lightweight philosophy. fastembed by Qdrant uses ONNX Runtime instead — same models (nomic-ai/nomic-embed-text-v1.5, 768 dims), near-identical retrieval quality (~99% of sentence-transformers on MTEB), ~50MB install vs ~2GB, ~3x faster inference on CPU.
**Alternatives considered:**
- **model2vec** (~7MB, 500x faster) — rejected due to ~20% retrieval quality drop (33.5 vs 41.9 MTEB retrieval), which matters for RAG.
- **Ollama embeddings** — already supported as auto-detect option, but requires Ollama running.
**Design:** Drop-in replacement in `LocalEmbedding` class. `fastembed.TextEmbedding` replaces `SentenceTransformer`. Same lazy loading, same thread pool execution. Core dependency in pyproject.toml.
**Impact:** ~40x smaller install. No PyTorch. Same embedding quality. Same model.

### Bug Fixes (Batch 5 — Cross-Channel)

6. **Memories isolated per channel** — Memory tool used `context.user_id` (Telegram numeric ID vs `"terminal-user"`) to scope all save/search/list/delete operations. A memory saved on Telegram was invisible from terminal and vice versa. For a personal assistant with one owner, memories must be global.
   - **Fix:** Memory tool, agent `_prepare()` (recent memories + RAG search), and `_auto_ingest()` all now use `"_global"` as user_id instead of the channel-specific user ID. Conversations remain per-channel (correct — separate chat histories), but knowledge is unified across all channels.
   - **Files:** `tools/memory_tool.py`, `core/agent.py`

7. **YouTube transcript extraction returning empty** — `fetch_youtube_transcript()` returned metadata but empty transcript text. YouTube changed their caption XML format: text now lives inside `<s>` (segment) children of `<p>` tags, not as direct text content. The parser only read `elem.text` which is empty when children exist.
   - **Fix:** Parser now checks for direct text first, then falls back to joining text from `<s>` child elements. Both old and new XML formats are handled.
   - **Files:** `tools/web_browse.py`

8. **Reminders not delivered to all active channels** — `_deliver_all()` in the scheduler only delivered to the originating channel and terminal. Other active channels (e.g., Discord when reminder was set via Telegram) were silently skipped because their chat_id was unknown.
   - **Fix:** Added `_last_chat_ids` registry to Scheduler, populated by Gateway on every incoming message via `record_activity()`. `_deliver_all()` now broadcasts to all active channels: originating channel (stored chat_id), terminal (no chat_id needed), and all other channels using their last-known chat_id from recent user activity.
   - **Files:** `scheduler.py`, `gateway.py`

### D33: Email Tool (Gmail via IMAP/SMTP) — *Superseded by D35*
**Decided:** 2026-02-17 → **Replaced:** 2026-02-19
**Original:** IMAP/SMTP with Gmail App Passwords, stdlib `imaplib`/`smtplib`. Separate `EmailConfig`.
**Why replaced:** Clunky setup (manual App Password), separate auth from calendar, two config sections.

### D34: Calendar Tool (Google Calendar via REST API v3) — *Superseded by D35*
**Decided:** 2026-02-17 → **Replaced:** 2026-02-19
**Original:** Inline OAuth2 flow in calendar_tool.py, separate `CalendarConfig`.
**Why replaced:** Duplicated OAuth2 logic, not shared with email.

### D35: Unified Google OAuth2 for Gmail + Calendar
**Decided:** 2026-02-19
**Rationale:** D33 (IMAP/SMTP) and D34 (inline OAuth2) used two separate auth mechanisms with two setup sections. Google OAuth2 supports multiple scopes in a single consent screen — one browser sign-in grants access to both Gmail API and Calendar API.
**Design:**
- **Shared `google_auth.py` module** (~140 LOC): Token management, browser OAuth2 flow, scope tracking, auto-refresh. Used by both email and calendar tools.
- **Gmail REST API v1** replaces IMAP/SMTP: `httpx` to `gmail.googleapis.com/gmail/v1`. Actions: authorize, inbox, read, send, search. MIME encoding via stdlib `email.message`.
- **Calendar tool** imports from `google_auth` instead of inline OAuth2 (~100 LOC removed).
- **Unified `GoogleConfig`** replaces `EmailConfig` + `CalendarConfig`: `enabled`, `client_id`, `client_secret`, `gmail_enabled`, `calendar_enabled`. Config migration in `load_config()` handles old keys.
- **Scope tracking**: Token file stores granted `scopes` string. `needs_reauth()` detects when newly-enabled services require re-authorization.
- **Single setup section** in `paw setup`: one prompt for Google Cloud credentials, checkbox for Gmail/Calendar, option to open console URL in browser.
**Dependencies:** Zero new — uses httpx (core), `http.server`/`webbrowser`/`email` (stdlib).
**Config:** `GoogleConfig` with `client_id`, `client_secret`. Env vars: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
**Files changed:** New `google_auth.py`, rewritten `email_tool.py` + `calendar_tool.py`, updated `config.py`, `gateway.py`, `setup_wizard.py`.

### D36: Contacts Tool (SQLite-backed address book)
**Decided:** 2026-02-19
**Rationale:** Email/calendar tools require knowing exact addresses. A contacts tool lets the LLM auto-resolve "email Alice" by searching contacts first. If a person has multiple emails (work/personal) or multiple contacts match, the LLM presents options to the user.
**Design:**
- New `contacts` table in SQLite: `id`, `name`, `email`, `phone`, `company`, `notes`, timestamps.
- CRUD methods on `MemoryStore`: `add_contact`, `search_contacts`, `get_all_contacts`, `update_contact`, `delete_contact`.
- Case-insensitive LIKE search across name, email, company.
- `ContactsTool` follows `memory_tool.py` pattern: module-level `_store` + `set_store()`, actions: add/search/list/update/delete.
- Tool description guides LLM: "Search contacts to find email addresses before sending email."
**Files:** `contacts_tool.py` (new), `store.py` (table + methods), `cli.py` + `gateway.py` (registration + wiring).

### D37: Files Tool (persistent user file storage)
**Decided:** 2026-02-19
**Rationale:** Users need a way to save notes, documents, and code snippets that persist across sessions. Merges "notes" and "file sync" concepts into one simple tool.
**Design:**
- Files stored at `~/.micropaw/files/` (filesystem, no database).
- Actions: save (write content), read (with 50KB display truncation), list (directory listing with sizes), delete.
- Subdirectories supported (e.g. `notes/meeting.md`).
- Path traversal security: `resolve()` + parent check prevents `../` escape.
- No file size limit on save.
**Files:** `files_tool.py` (new), `cli.py` (registration).

### D38: Provider Template Method Pattern
**Decided:** 2026-02-19
**Rationale:** All 4 providers duplicated the same chat() pattern: build body → log → retry → parse response. Also duplicated close() with `_client.aclose()`.
**Design:**
- Base class provides concrete `chat()` calling `_build_body()` → `retry_on_transient(_do_chat)` → `_parse_response()`.
- Subclasses override only the three hooks. `close()` in base with `hasattr(self, "_client")` guard.
- Removed `retry_on_transient` import from all 4 providers.
**Impact:** -16 LOC net across 5 files. Single retry/log path.

### D39: Channel stream_response() Helper
**Decided:** 2026-02-19
**Rationale:** Telegram, Discord, Slack all had ~25 lines of identical streaming logic (accumulate chunks, throttle edits, chunk final response).
**Design:**
- `stream_response()` in `channels/base.py` takes `edit_fn`/`send_fn` lambdas for platform-specific message editing.
- Accumulate + throttle + edit pattern with configurable `edit_interval` and `max_msg_len`.
**Impact:** Replaced ~25-line blocks with ~8-line calls in each channel. Removed `import time` from 3 channels.

### D40: ToolResult Type for Media Returns
**Decided:** 2026-02-19
**Rationale:** Screenshots from web_browse need to pass image bytes back through the agent to the provider. Plain string returns can't carry binary media.
**Design:**
- `ToolResult(text, media: list[MediaItem])` dataclass in `tools/base.py`.
- Agent normalizes `str | ToolResult` in `_execute_tools`.
- Claude provider emits base64 image blocks in tool_result content. Other providers: text-only.
**Impact:** Tools can now return images alongside text. Backward compatible — existing str-returning tools unchanged.

### D41: Interactive Web Browse with Sessions
**Decided:** 2026-02-19
**Rationale:** Single-page browsing (navigate → extract text → close) can't handle multi-step interactions like clicking links, filling forms, or scrolling to find content.
**Design:**
- 5 actions: browse, click, type, scroll, screenshot.
- Module-level `_sessions` dict keyed by `conversation_id` → `(context, page)`.
- `_get_session()` creates or reuses; `close_session()` called by agent via `try/finally`.
- Click: CSS selector → `get_by_text()` fallback. Type: CSS selector → `get_by_placeholder()` fallback.
- Screenshot returns `ToolResult` with `MediaItem(type="image", ...)`.
**Impact:** Multi-step browsing in a single conversation turn. Sessions auto-cleanup.

### D42: Location Context
**Decided:** 2026-02-19
**Rationale:** "Near me" queries need geographic context. Without it, web searches return generic results.
**Design:**
- `persona.location` field in config + `MICROPAW_LOCATION` env override.
- Injected into system prompt: "User's location: X. Use this for local queries like 'near me'."
- Added to setup wizard as optional prompt.
**Impact:** Zero-config for users who set location once. LLM can add location to search queries automatically.

### D43: claude.py _image_block() Static Method
**Decided:** 2026-02-19
**Rationale:** Identical base64 image encoding appeared in both tool_result and user message branches of `_build_body`.
**Design:** Single `_image_block(media)` static method, used via list comprehension in both paths.
**Impact:** -8 LOC, single source of truth for image encoding format.

### D44: Browser Install Hint in Setup Wizard
**Decided:** 2026-02-19
**Rationale:** Users discover Playwright is missing mid-conversation when they first try to browse — bad first impression. The setup wizard is the natural place for optional dependency setup.
**Design:**
- After permissions, before embeddings: "Install web browsing support? (Playwright + Chromium, ~150MB)"
- If yes: runs `pip install playwright` + `playwright install chromium` via subprocess.
- If no: skips silently. Runtime error still provides install instructions.
- Uses `capture_output=True` to keep wizard clean. Catches `CalledProcessError`/`FileNotFoundError`.
- Future phases: chat-based config tool (D-next), then gated install tool (D-next+1).
**Impact:** Users who want browsing get it working in setup, no mid-chat surprises.

### Metrics (current)
- Source LOC: ~8,800
- Test LOC: ~4,700
- Tests: 393 passing (7 skipped: browser)
- Tools: 17 (50 actions)
- Dependencies: 8 core, 7 optional extras

---

## Cross-Channel Communication

### D45: Unified DM Conversations + Cross-Channel Send
**Decided:** 2026-03-16
**Rationale:** Personal assistant should share conversation context across all channels. Industry data shows omnichannel (shared context) yields 2x higher satisfaction vs multichannel (isolated). MicroPaw already shared memories globally — conversations were the missing piece.

**Design — Unified DMs (D2-style):**
- `get_conversation_id()` returns `"dm:owner"` for all DMs (`channel_id is None`) regardless of platform.
- Group chats keep isolated platform-specific IDs (`"{platform}:{channel_id}"`).
- `/new` command resets to `"dm:owner:{random}"` — works across all DM channels.
- Single owner model: MicroPaw is a personal assistant, all DMs are the same user.

**Design — Cross-Channel Send (Features A+B):**
- `ChannelSendTool` exposes `channel_send` to the LLM: send a message to any active channel.
- Routes through existing `MessageBus` → `_bus_consumer` → `channel.send_message()`.
- `_channel_registry` in `channel_send.py` tracks last-known `chat_id` per channel, populated by Gateway on every incoming message.
- Terminal auto-registered at startup (needs no `chat_id`).
- System prompt tells the AI which other channels are active.

**Impact:**
- Chat on terminal, continue on Telegram — full history preserved.
- "Send this to my Telegram" works naturally via `channel_send` tool.
- Group chats remain isolated — no context leaks.
- Zero new dependencies. ~80 LOC new (`channel_send.py`), ~15 LOC changed across 4 files.

### Bug Fixes (Batch 6 — Cross-Channel Audit, 9 rounds, 60 fixes)

**Round 1 (5 fixes):** channel_send rate limiter bypass → per-turn cap; Slack DM detection (`D*` prefix); cron/delegate used `conversation_id` as chat address → `ToolContext.chat_id`; module state cleanup on gateway stop; terminal phantom channel registration.

**Round 2 (5 fixes):** WebChat/WhatsApp `send_message` prefix stripping; Discord DM `fetch_user` fallback; scheduler double-prefixed `channel_id` → pass `None`; `/rejoin` stuck after `/newall`.

**Round 3 (4 fixes):** SubAgent `ToolResult` normalization; `/rejoin` isolation via `_rejoined` set; sessions tool `dm:` filter; SubAgent + DelegateTool tests (7 new).

**Round 4 (6 fixes):** Heartbeat polluting `dm:owner` → dedicated conversation; scheduler fallback with mismatched `chat_id` removed; WebChat bare user ID; Discord `_watch_task` fresh client on crash; SOUL/USER/SKILLS.md mtime-cached reads; sessions `info`/`clear` auth check.

**Round 5 (6 fixes):** Context compaction never persisted → delete old messages + persist summary; Discord/Telegram `send_message` `ValueError` guard; `channel_send` message length cap (4096); sessions `@staticmethod` removed; tool error cache excludes `"Error:"` prefix.

**Round 6 (9 fixes):** Gemini API key moved from URL to `x-goog-api-key` header; shell tool: block `declare`, `/proc/self/environ`, `node -e`, `__import__`; `compact_messages` ID ordering; `validate_url` DNS async via `run_in_executor`; WhatsApp `verify_token` redacted; tool error cache; Google OAuth error log sanitized; `LocalEmbedding._load()` thread-safe.

**Round 7 (8 fixes):** `__version__` 0.1.2→0.2.1; terminal `!cmd` bypassed all shell security → checks `_BLOCKED_RE`; connector Gemini key in header; `setup_wizard` `sys.executable -m pip`; user message content log → DEBUG; WebChat auth token log → DEBUG; `compact_messages` simplified; terminal `proc.kill()` + `proc.wait()`.

**Round 8 (7 fixes):** Terminal `_run_shell` defensive `ProcessLookupError` catch; scheduler heartbeat async file read; shell patterns extended to `less`/`more`/`head`/`tail`; cron `int(job_id)` guard; vision async `read_bytes`; YouTube InnerTube `.json()` guard; files tool 10MB save cap.

**Round 9 (7 fixes):** Google OAuth `OSError` on port conflict → handled with `SO_REUSEADDR`; sessions `dm:` access restricted to current conversation; compaction summary persisted to DB; `_tool_cache` type annotation fixed; email `_b64_decode` proper padding; SubAgent fabricated assistant message removed; heartbeat skipped from channel registry via `_INTERNAL_PLATFORMS`.
