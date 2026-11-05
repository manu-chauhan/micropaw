# Changelog

All entries in reverse chronological order. LOC = source lines (excluding tests/docs).

---

### 2026-11-05: v0.4.0 privacy modes, models on any computer, agents, goals, a Raspberry Pi guide

Upgrade note: nothing to do. `privacy.mode` is `normal` unless you change it, as before. Three changes you will notice. Each reply now ends with a line such as `[via claude (api.anthropic.com)]` (`display.show_provider` turns it off). A configured search provider no longer falls back to another service by itself, and once a chat has read outside content, a search asks first. Your first run after the upgrade prints a one-time note about `privacy.mode`.

Privacy:
- `privacy.mode`: `normal`, `local` or `airgapped`, read once at start and only ever tightened by `MICROPAW_PRIVACY`. `paw privacy`, `paw status`, `paw doctor`, `/privacy` and `/status` show the mode, where each model, search, channel, MCP server and speech service sends data, and file permissions. Local and air-gapped modes accept only this computer, and your network when `privacy.allow_lan` is on.
- Air-gapped mode is enforced in the process, in the programs MicroPaw starts, and by a CI test that fails if a connection leaves loopback. Cloud models, vendor chat apps, Google tools, updates and downloads are refused with a message that says why.
- `paw setup` asks how private to be first, preselects local when it finds a model server on this computer, and asks where your models run. The wizard turns search off unless you pick a provider (the config default is still `auto`), and a cloud fallback for a local main model has to be allowed in plain words.
- Offline search: `web_search` answers from Kiwix archives (`search.kiwix_url`, `search.kiwix_books`) and your own files (`paw docs add <folder>` for `.txt`, `.md` and `.pdf`) with no internet. Results name the source and snapshot date. The model can read a whole article by id, never by an address it makes up.
- Kiwix and your documents are searched before your SearXNG in local mode, and Kiwix is searched in every mode when `search.kiwix_url` is set. An address that is not on this computer or your network is called "the internet" in the approval. A failed search request no longer logs the query, and replies on Telegram, Discord, Slack and WhatsApp name a model on your network without its address.
- Local mode searches only through your own SearXNG (`search.searxng_url`), optionally through a SOCKS proxy such as Tor (`search.proxy`, install `micropaw[tor]`). Every query is shown before it is sent, and the next one asks again. A down server is reported, never replaced by DuckDuckGo. `paw doctor --search` runs one test query after asking.
- Kiwix articles are fetched without cookies, referrers or tracking parameters, and every redirect is checked. In local mode `web_browse` and `http_request` are off unless named in `privacy.allow`. `web_browse` shows each page address for approval, removes tracking parameters, and the browser can load only the page you approved; Chromium's own background traffic is off in every mode. `http_request` does the same in local mode: it asks for every address, shows the cleaned one and any long path or query part, sends no cookies, referrer or caller headers, checks every redirect, and makes GET requests only. Chromium closes after 10 idle minutes.
- `paw privacy share-history` lets cloud models see what was saved in local or air-gapped mode. It says what it does, asks first (`--yes` skips the question) and prints how many rows it changed. `paw setup` now records the mode you pick as your own choice, so the next start does not hold it back.
- Local-mode data stays away from cloud models after you loosen the mode: documents (also in `web_search`), contacts, scheduled jobs, saved files and long tool outputs are flagged like messages and memories. A turn where a local model could see such data saves its messages and memories flagged too. Cloud embedders no longer get local-mode documents when vectors are rebuilt, a delegate task asks before it goes to a cloud helper, and `paw privacy` says how many items are hidden.
- `http_request` in local mode connects to the public address it checked, so a name whose DNS answer changes to this computer or your network cannot reach it. `paw privacy allow web_fetch` (or `web_browse`, `google`) records a choice that a hand edit of `config.json` would not keep, and `paw setup` asks about web page fetches and before it keeps a held setting. The README now explains `MICROPAW_CONFIRM_PRIVACY` for bash, PowerShell and cmd.
- `paw forget --all` also clears the document search index and scheduled jobs, and retention wipes old document words from the index. `paw docs add` skips a PDF it cannot read, reads UTF-16 text, and stores one source per file.
- Link previews are off on Telegram, Slack and Discord, and Markdown images are removed from replies, because the company's server would fetch any address in a reply. `paw connect` says that vendor chat apps see every message.
- README: a Privacy section with what is and is not checked (a local address can still be a proxy to the cloud), the cloud providers' data terms as of today, and how to move models and packages onto an offline machine. The old "no data leaves your machine" claim is gone.

Models on any computer:
- `paw models add NAME ADDRESS` tests a server on your network or tailnet and saves it (`--key-env`, `--use main|fast|vision|fallback`). It only lists models, never runs one. `paw models remove` refuses while something uses the server. `paw models scan` also checks your config and `--host`, and `--lan` or `--range` look at your own network on the usual model ports, after asking first. `paw doctor` checks every model, memory-search and speech server you use.
- One rule for typed addresses: `192.168.1.50` and Ollama's own `0.0.0.0:11434` format both work in `OLLAMA_HOST` and the config. Tailscale addresses and `*.ts.net` names count as your own network for models and speech, and a LAN name that did not resolve at start is tried again later. The agent's own tools still cannot reach LAN hosts.
- Plain messages name the other machine when it is asleep, firewalled or wants a key. LAN traffic skips HTTP proxies. Speech servers on another computer work by provider name, and a voice note that cannot be heard says which server did not answer.
- Review fixes: a mistyped server address in `paw setup` is asked again, a bad config file gives one plain line (`paw setup --reset` starts again and keeps `config.json.bak`), `paw models scan` and `add` stay on this computer and your network in `local` and `airgapped` mode, the refusal for a name that cannot be found says to use its IP address or `privacy.lan_hosts`, and `paw models remove` also checks `voice.stt` and `voice.tts`. A network scan is limited to 512 addresses and reports when its time limit cut it short.
- README: how to open Ollama, llama.cpp, vLLM, LM Studio, Jan and KoboldCpp to the network per OS, with the security warnings, Tailscale, and a Raspberry Pi section with measured numbers marked as desktop numbers.

Models and agents:
- A model registry with presets (openrouter, groq, deepseek, mistral, together, xai, cerebras, fireworks and more, plus lmstudio, llamacpp, vllm, jan, koboldcpp) and your own servers under `providers`. One `provider:model` spec everywhere, one shared connection per spec, fallbacks in order, and local servers get one request at a time.
- Named agents (`paw agent add`) with their own model, persona, tools, workspace and chats, routed per chat or with `/agent`. A local agent never takes a cloud model unless you say so.
- Local model care: context size by RAM, Ollama capability checks, size-aware picks, a first-run scan, and an offer to pull a model sized for your memory.
- Budgets and a usage ledger: `/usage`, `paw usage`, a daily cap, and every agent role metered on its own.
- Faster turns: read-only tools run side by side, one turn at a time per chat, summaries in the background, one commit per turn, and large tool output saved for `tool_output`.

Tools and skills:
- MCP servers over streamable HTTP (https only for remote ones) and `paw mcp add|list|test|trust`. Inspect and HAR tools for reverse engineering. Email-triggered jobs with sender and DMARC checks.
- Skills in the agentskills layout with a prompt index and a `skill` tool where you approve proposals. `/search` and a session search tool over past conversations (SQLite FTS5).
- Goal loops: `/goal` and `paw goal` work in rounds until a check command passes, with limits on rounds, time, spend and repeats.
- `web_browse` preview serves a workspace page on loopback and reports errors, state and a screenshot.

Shell, install and service:
- The shell runs in a sandbox (bubblewrap, sandbox-exec, Docker or Podman) with secrets redacted from output. PowerShell on Windows.
- `install.sh` and `install.ps1` with uv, `paw service` (systemd, launchd, Windows logon task), `paw update`, `paw extras add`, a headless `paw start` with a supervisor, a uv-built Docker image and a compose file with an Ollama profile.
- Channels: approval buttons, Discord and Slack media, Telegram voice replies.

Fixes: scheduler jobs run on the agent that created them; the Gmail ids a job handled are remembered per job; Ollama numeric `keep_alive` and stream errors; saved turns after side effects; owner-only config and log folders; env secrets that the config names are hidden from child processes.

Tests: 1840 passed.

---

### 2026-11-01: v0.3.0 approvals, any-model roles, MCP, photos and voice

Upgrade note: actions that change things now ask you first (reply `yes <id>`). Shell runs and relative file writes land in `~/micropaw`. fastembed is no longer installed by default: Ollama or OpenAI embeddings work as before, or `pip install "micropaw[embeddings]"` for offline ONNX embeddings.

Security:
- Approvals before side effects: host shell commands, email send, channel_send, calendar create and delete, HTTP other than a plain GET, and file writes outside the workspace or to build and CI files. Asks are single-use, tied to their chat and expire after 10 minutes. `always <id>` saves an exact-match rule. Cron, heartbeat and background sub-agents have nobody to ask, so these are refused there. This replaces `confirm_dangerous`, whose warning could never be satisfied.
- Outside-content mark: reading web pages, email, calendar, files, PDFs, images, command output or MCP results marks the conversation (stored; `/new` clears it). While marked, saved rules don't apply and any HTTP request asks. Memories saved while marked carry the mark.
- http_request checks the address it actually connected to (DNS rebinding), caps responses at 5 MB, never forwards headers to another site and won't re-send a POST across sites.
- Audit log of every tool call and approval in `~/.micropaw/audit.jsonl`; `paw log` shows it.
- delegate no longer takes a model-written system prompt; background sub-agents get their own browser session.

Features:
- Per-job models, local or cloud: `models.fast` (summaries, sub-agents) and `models.vision` (photos), as `provider:model`. Presets for lmstudio, llamacpp, vllm, openrouter, groq, deepseek, mistral, together.
- MCP client for stdio servers (REA, GhidraMCP, blender-mcp, Godot and others). Servers never see MicroPaw's keys; calls ask unless allowed.
- Telegram photos, voice notes and audio. Voice is transcribed by any OpenAI-style endpoint (`voice.stt`), including a local Whisper server. Photos go to the main model or are described by the vision model.
- `/memory` and `/forget <key>`.
- Claude: default model `claude-sonnet-5-5` (`claude-sonnet-4-20250514` was retired on 15 Jun 2026), prompt caching, thinking blocks sent back within a turn, effort setting.

Fixes:
- Ollama now sends assistant tool calls back, so local models stop repeating tool calls. Gemini: user images, parallel tool results in one turn, system prompts joined, thought signatures kept. OpenAI: `max_completion_tokens` for api.openai.com; images from tools reach OpenAI, Gemini and Ollama.
- Calendar: all-day events end the next day; timed events use your local timezone.
- WhatsApp: webhook answered at once and messages de-duplicated, so retries don't run twice.
- Heartbeat and CLI-created jobs reach you on a headless `paw start`.
- Telegram commands (`/memory`, `/cron enable`) were filtered out before reaching MicroPaw.
- The system prompt is stable and no longer lists tools twice; date and time are given every turn.
- Core install 24 MB, down from 198 MB.

Tests: 519 passed.

---

### 2026-10-27: v0.2.7 security hotfix

Upgrade note: MicroPaw now answers only accounts you list. After upgrading, message your bot once; it replies with your ID. Then run `paw allow telegram <id>` (or discord, slack, whatsapp). WhatsApp now refuses to start without `app_secret`. Jobs created before this release stay enabled, so check them once with `paw cron list` and remove any you don't recognise.

Security:
- Owner-only access on Telegram, Discord, Slack and WhatsApp. An empty allowlist now means nobody, not everybody. Strangers are told their ID once a day and refused; group members other than you are ignored. New `paw allow <channel> <id>` edits the list without restarting.
- Telegram setup asked for usernames while the bot compared numeric IDs, so restricting access locked the owner out. Setup now asks for numeric IDs on every channel.
- WhatsApp webhook signatures are always checked; `app_secret` is required.
- Recurring cron jobs created by the assistant start paused. You get a message from MicroPaw itself (not the model) showing the exact task, and turn it on with `/cron enable <id>` or `paw cron enable <id>`. Minimum interval 5 minutes. Each job runs in its own conversation, so its output never carries your DM history into a group.
- file_read refuses common credential files (~/.git-credentials, ~/.config/gh, .npmrc, .pypirc, SSH private keys). file_write refuses files that run code later (systemd user units, ~/.local/bin, git hooks, .pth files, shell rc files, launch agents, Windows startup). Path checks now work with Windows separators and case-insensitive file systems.
- URL checks block every non-public address, including 100.64.0.0/10 (CGNAT, Alibaba Cloud metadata, Tailscale). web_browse re-checks the page address after redirects, clicks and scrolls.
- Gmail and Calendar IDs are validated, so a crafted event ID can no longer turn "delete event" into "delete calendar".
- Shell commands run without API keys, tokens or secrets in their environment.
- DM memories and documents are no longer added to group chat prompts.
- Web chat accepts only its own origin unless allowed_origins is set.
- config.json is forced to mode 600 on every save, not only when first created.

Fixes:
- Long tool-heavy chats could fail with HTTP 400 when the history window started on a tool result whose call had been cut off.
- An empty message (for example an image-only Discord DM) is no longer saved as an empty turn that broke the shared conversation on Claude.
- 29 tests failed on Python 3.12+ because of `asyncio.get_event_loop()` in one test file.

Tests: 454 passed on Python 3.11, 3.12 and 3.13.

---

### `e334f7c` 2026-02-19 — feat: interactive web browsing, screenshots, location context
**Source: ~8,800 LOC | Tests: 393 | Tools: 16 (48 actions)**

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
