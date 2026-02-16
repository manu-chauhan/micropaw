# MicroClaw — Decision Log & Timeline

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
- **Chose: MicroClaw** — Goldilocks between Pico (too small) and Open (too big)

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
**Decided:** 2026-02-16
**Rationale:** NanoClaw's bus architecture is clean but adds complexity for v1. MicroClaw keeps the handler callback pattern (simpler, proven) with the bus available for future WebSocket streaming and multi-agent scenarios.
**Impact:** Channels use direct handler callbacks. Bus module exists but isn't wired into the main flow yet — reserved for v2.

### D4: Playwright for browser automation (not CDP)
**Decided:** 2026-02-16
**Rationale:** User confirmed browser automation is a selling point. Playwright gives full browser control (navigate, click, fill, screenshot, JS eval) without requiring Chrome extension + daemon like OpenClaw's CDP approach. Cross-browser (Chrome, Firefox, WebKit).
**Impact:** `web_browse` tool uses Playwright. Optional dependency: `pip install microclaw[browser]`

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
**Rationale:** ACP is OpenClaw's custom NDJSON-based RPC for native client apps. MicroClaw uses standard WebSocket — any client (web, mobile, CLI) can connect directly.
**Impact:** Standard protocols only. No custom wire format.

### D9: No OAuth token management
**Decided:** 2026-02-16
**Rationale:** MicroClaw uses BYOK (bring your own keys). API keys don't expire like OAuth tokens. Invalid key → 401 → failover chain tries next provider.
**Impact:** Simple API key auth. No token refresh complexity.

### D10: No wake-word detection
**Decided:** 2026-02-16
**Rationale:** macOS-only niche feature requiring a background daemon with local speech recognition. Chat-first design. Voice messages from Telegram/Discord work without this.
**Impact:** No voice activation. Chat input only.

### D11: Embedded SQLite schema (no external files)
**Decided:** 2026-02-16
**Rationale:** OpenPaw uses a separate schema.sql file loaded via importlib.resources. MicroClaw embeds the schema as a Python constant in store.py — simpler, no file loading issues, schema version is always in sync with code.
**Impact:** One less file. Schema changes are code changes.

### D12: Single Pydantic config model
**Decided:** 2026-02-16
**Rationale:** Proven pattern from OpenPaw. One Config class replaces OpenClaw's 126 config files. Env var overrides via a clean mapping table instead of ad-hoc os.getenv() calls.
**Impact:** `~/.microclaw/config.json` with 0o600 permissions. All secrets in one place.

## Implementation Timeline

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1: Foundation | pyproject.toml, config, base types, bus, security, logger | ✅ Complete |
| Phase 2: Providers | Claude, OpenAI, Gemini, Ollama — all via httpx + failover | ✅ Complete |
| Phase 3: Agent Core | Agent loop, context compaction, conversation, memory store | ✅ Complete |
| Phase 4: Tools | All 11 tools (file, shell, web, memory, vision, pdf, sessions) | ✅ Complete |
| Phase 5: Channels | Terminal, WebChat, Telegram, Discord, Slack, WhatsApp | ✅ Complete |
| Phase 6: Gateway + CLI | Central coordinator, Click CLI, setup wizard | ✅ Complete |
| Phase 7: Polish | Docker, tests, .env.example, README | ✅ Complete |

## Phase 7 Deliverables
- **Dockerfile** — Multi-stage: base (core), browser (+ Playwright/Chromium), full (all extras)
- **docker-compose.yml** — 3 service profiles (base, browser, full) with named volume
- **.env.example** — All supported environment variables documented
- **README.md** — Features, quickstart, installation, Docker, architecture, CLI reference
- **78 unit tests** — Config, bus, security, tools, providers, embeddings — all passing

## File Count & LOC (actual)
- Source files: 35 Python files + 6 test files
- Source LOC: 4,462
- Test LOC: ~450
- Dependencies: 7 core, 5 optional extras
