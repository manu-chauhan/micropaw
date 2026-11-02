![MicroPaw](https://raw.githubusercontent.com/manu-chauhan/just_images_extras/main/puppy.png)

# MicroPaw

### A local-first AI agent that runs on your machine, talks on every platform, and keeps your keys to yourself.

#### Your AI. Your model. Your machine.

6 channels. 4 providers. 17 tools. Cross-channel conversations. Shared memory everywhere. ~9,200 lines of Python. Zero SDK dependencies.

[![PyPI](https://img.shields.io/pypi/v/micropaw?style=for-the-badge)](https://pypi.org/project/micropaw/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-393%20passed-brightgreen?style=for-the-badge)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](https://github.com/manu-chauhan/micropaw/blob/main/LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/manuchauhan)

---

> **100% free with Ollama** — Run MicroPaw entirely on your machine with zero API keys, zero cost, zero data leaving your network. Just `ollama pull llama3.1` (or `phi3` / `gemma2:2b` on lighter hardware) and go. Cloud providers (Claude, GPT-4o, Gemini) are optional.

```bash
pip install micropaw
paw setup     # pick a provider — Ollama is free, cloud keys are optional
paw chat      # start talking
```

**Or from source:**
```bash
git clone https://github.com/manu-chauhan/micropaw.git
cd micropaw
pip install .
paw setup && paw chat
```

---

## What Can You Do?

```
┌─ Search & Browse ────────┐  ┌─ Files & Knowledge ────────┐  ┌─ Communicate ─────────────┐
│ Web search (3 engines)    │  │ Read files & PDFs          │  │ Gmail (read/send/search)   │
│ Browse pages interactively│  │ YouTube video Q&A          │  │ Google Calendar            │
│ Click, type, scroll       │  │ Save/manage user files     │  │ Scheduled reminders        │
│ Take & analyze screenshots│  │ RAG — ask later, anywhere  │  │ Proactive heartbeat tasks  │
└───────────────────────────┘  └────────────────────────────┘  └────────────────────────────┘

┌─ Memory & Context ───────┐  ┌─ System ──────────────────┐  ┌─ Intelligence ────────────┐
│ Unified cross-channel chat│  │ Shell commands (hardened)  │  │ Multi-agent delegation     │
│ Cross-channel messaging   │  │ HTTP requests (SSRF-safe)  │  │ Context compaction         │
│ Vector search (sqlite-vec)│  │ Vision (image analysis)    │  │ Streaming on all channels  │
│ Auto-embed everything     │  │ Location-aware ("near me") │  │ Provider failover chain    │
└───────────────────────────┘  └───────────────────────────┘  └────────────────────────────┘
```

### See It in Action

```
You:      "Find pizza places near me and check the menu of the top result"

MicroPaw: [web_search] "pizza near San Francisco"
          → Found 5 results. Tony's Pizza Napoletana rated 4.8★

          [web_browse] → opens tonys-pizza.com
          [scroll]     → scrolls to menu section
          [screenshot] → captures menu

          "Tony's Pizza Napoletana (4.8★, 0.3 mi away) has a great
           Neapolitan-style menu. The Margherita is $18, Marinara $15.
           Want me to check hours or find more options?"
```

```
You:      !ls -la ~/Downloads     ← run shell commands without leaving the chat
You:      "summarize that PDF"    ← then ask the AI about what you see
```

```
You (Telegram, Day 1):   "Read this report" → attaches quarterly.pdf
MicroPaw:                 ✓ PDF chunked → embedded → stored for RAG

You (Discord, Day 3):    "What did Q4 revenue look like?"
MicroPaw:                "Revenue grew 23% in Q4, driven by..."
                          (auto-retrieved from the PDF you read on Telegram)
```

> Set your location in config (`persona.location`) or via `MICROPAW_LOCATION` env — powers all "near me" queries.

---

## Highlights

### Intelligence

- **Same brain, everywhere** — Start a conversation on Telegram, pick it up on Discord, ask from WhatsApp. Everything stays on your machine.

- **Cross-channel conversations** — All your DM conversations share a single thread. Chat on Terminal, continue on Telegram, pick up on Discord — same history, same context. Group chats stay isolated. `/new` branches this channel into a fresh conversation, `/rejoin` returns to the shared thread, `/new all` starts fresh everywhere.

- **Cross-channel messaging** — Tell MicroPaw "send this to my Telegram" from Terminal, or "forward that to Discord" from anywhere. The `channel_send` tool routes messages to any active channel instantly.

- **RAG out of the box** — Read a PDF, paste a YouTube URL, or browse a web page. Documents are auto-chunked, embedded, and searchable across all channels. Ask about them days later from any platform.

- **Interactive web browsing** — Browse pages, click links, type into search boxes, scroll, and take screenshots. Multimodal providers (Claude, GPT-4o, Gemini) can analyze screenshots to make decisions. Sessions persist across tool calls within a conversation turn.

- **YouTube transcripts** — Paste a YouTube link and MicroPaw extracts the full transcript automatically. No API key, no extra dependency. Auto-indexed for RAG.

- **sqlite-vec ANN search** — Vector search powered by [sqlite-vec](https://github.com/asg017/sqlite-vec) directly inside SQLite. Scales to tens of thousands of chunks. Falls back to brute-force cosine if removed.

- **Multi-agent delegation** — The main agent spawns sub-agents for parallel tasks. Sub-agents inherit all tools except `delegate` (no recursive spawning). Max 25 iterations, configurable.

- **Context compaction** — Conversations never hit a wall. When context fills up, older messages are summarized by the LLM while the last 10 are kept verbatim. Unbounded conversations.

- **Scheduled tasks** — Tell MicroPaw "remind me every 2 hours to stretch" or use `paw cron add`. Recurring and one-shot jobs, managed via CLI or conversationally through the LLM. Reminders broadcast to all active channels.

- **Email & Calendar** — Check your Gmail inbox, send emails, search by sender/subject. List, create, and delete Google Calendar events. All via natural language — "what's on my calendar tomorrow?" or "send an email to Alice saying I'll be late."

- **Proactive heartbeat** — Drop tasks in `~/.micropaw/HEARTBEAT.md` and MicroPaw picks them up every 30 minutes. No prompting needed — it just does the work.

- **Custom persona & skills** — Create `~/.micropaw/SOUL.md` for personality, `~/.micropaw/USER.md` for user context, and `~/.micropaw/SKILLS.md` for multi-step workflow hints. Your assistant, your rules.

### Reliability

- **Automatic failover** — Provider chain with exponential backoff and Retry-After support. Claude down? Switches to GPT-4o, then Ollama. Keeps working.

- **Self-healing channels** — If a channel crashes, MicroPaw detects it, waits 5 seconds, and restarts. Other channels keep running.

- **Streaming everywhere** — Real-time token-by-token output on Terminal, Telegram (0.5s), Discord (0.8s), Slack (1.0s). No waiting for full responses.

- **Rate limiting** — Per-user sliding window + burst detection at the gateway. Protects your API keys from runaway requests.

### Security

- **Owner-only by default**: only accounts you list can talk to it. A stranger is told their ID once and refused. Add your own accounts with `paw allow telegram <id>`.

- **No hidden standing tasks**: recurring tasks the assistant schedules start paused until you reply `/cron enable <id>`.

- **Shell hardening**: commands run without your API keys or tokens in their environment, and 40+ patterns (reverse shells, fork bombs, `rm -rf /`) are blocked. The blocklist is a speed bump, not a sandbox, so shell stays off unless you turn it on.

- **SSRF protection** — URL scheme whitelist, private IP range blocking, DNS rebinding detection, cloud metadata service blocking.

- **Path traversal prevention** — Symlink resolution, `..` detection, sensitive file blocking (`/etc/shadow`, `~/.ssh`, API keys).

- **WebSocket hardening** — 1MB frame limit, 50 max headers, origin validation, timing-safe authentication.

### Architecture

- **Zero SDK dependencies** — All 4 AI providers via raw httpx. No `anthropic`, `openai`, or `google-genai` packages. Switching providers is a config change.

- **16 built-in tools** — Web search (Brave -> SearXNG -> DuckDuckGo), browser automation, file I/O, shell, HTTP requests, memory, contacts, user files, vision, PDF, sessions, delegation, cron scheduling, email (Gmail), calendar (Google Calendar) — all security-hardened.

- **Raw WebSocket** — WebChat channel implements RFC 6455 from scratch. No aiohttp. ~100 LOC.

- **Tool result caching** — Read-only tool calls cached per-message (LRU-32). Same query twice? Instant response, zero wasted tokens.

- **Fully local option** — Run entirely offline with Ollama. No data leaves your machine. No telemetry. No phone home. Ever.

- **Docker ready** — Multi-stage Dockerfile (base / browser / full). `docker compose up` with profiles. Named volume for persistence.

---

## Why MicroPaw?

| | **MicroPaw** | **OpenClaw** | **NanoClaw** | **PicoClaw** | **MimiClaw** |
|---|---|---|---|---|---|
| **Language** | Python | TypeScript | TypeScript | Go | C |
| **Codebase** | ~9,200 LOC | ~430k LOC | ~500 LOC | ~4k LOC | ~15k LOC |
| **Channels** | 6 | 15+ | 1 (WhatsApp) | 5 | 3 |
| **Tools** | 16 (48 actions) | 25 + 53 prompt templates | SDK-dependent | 6 | 6 |
| **AI SDKs** | None — raw httpx | Internal framework | Anthropic SDK | None — raw HTTP | None — raw HTTP |
| **RAG / Vectors** | sqlite-vec ANN + fallback | sqlite-vec + BM25 | No | No | No |
| **Telemetry** | None, ever | Local JSONL (opt-out) | None | None | None |
| **License** | MIT | MIT | MIT | MIT | MIT |

**MicroPaw sits in the sweet spot** — full-featured like OpenClaw (RAG, 6 channels, 17 tools, scheduling, email, calendar, multi-agent delegation) but lightweight like PicoClaw (~9,200 LOC, zero SDK dependencies). No telemetry, no bloat, no wrappers.

> Started at **4,667 LOC** in the first commit. Grew to **~9,200 source + ~5,150 test LOC** while adding 6 channels, 17 tools, RAG, multi-agent delegation, scheduling, email, calendar — and 389 tests. Every feature earned its lines.

---

## One AI, Every Platform

![Terminal](https://img.shields.io/badge/Terminal-%23121011?style=for-the-badge&logo=gnubash&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)
![Discord](https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=for-the-badge&logo=slack&logoColor=white)
![WhatsApp](https://img.shields.io/badge/WhatsApp-25D366?style=for-the-badge&logo=whatsapp&logoColor=white)
![WebChat](https://img.shields.io/badge/WebChat-0088CC?style=for-the-badge&logo=websocket&logoColor=white)

### Talk on Telegram during the day. Switch to Discord at night. Ask from WhatsApp on the go.

**Your assistant carries the full conversation and memory across every channel — same personality, same knowledge, same context. Set up once, run everywhere.**

> **Zero telemetry. Zero data collection. MicroPaw never phones home — your conversations, API keys, and data stay on your machine. Always.**

```
        Telegram     Discord     Slack     WhatsApp     WebChat
            \           |          |          |           /
             \          |          |          |          /
              +---------+----------+----------+---------+
                                   |
                                   |
   Terminal  ─────────────  [  Gateway  ]
                                   |
                                   |
                        +----------+----------+
                        |          |          |
                        |          |          |
                     [Agent]    [Tools]    [Memory]
                        |                     |
                        |               SQLite + vectors
                        |
                   [ Provider ]
                        |
          Claude / GPT-4o / Gemini / Ollama
```

```bash
paw connect telegram    # guided setup, validates token live
paw connect discord     # auto-generates bot invite URL
paw start               # launch ALL channels at once
```

---

## Quick Start

**Terminal only:**
```bash
pip install micropaw
paw setup && paw chat
```

**Multi-channel:**
```bash
pip install "micropaw[telegram,discord]"
paw setup
paw connect telegram
paw connect discord
paw start
```

**Fully local (no internet):**
```bash
ollama pull llama3.1          # or phi3 / gemma2:2b on lighter hardware
pip install micropaw
paw setup    # select Ollama
paw chat
```

> `micropaw` also works as a command name — both are identical.

---

## Tools

| Tool | What it does |
|------|-------------|
| `web_search` | Brave -> SearXNG -> DuckDuckGo fallback chain |
| `web_browse` | Browse, click, type, scroll, screenshot — interactive Playwright sessions |
| `http_request` | HTTP requests with SSRF protection per hop |
| `file_read` | Read files with path traversal protection |
| `file_write` | Create and modify files |
| `shell` | Execute commands (40+ dangerous patterns blocked) |
| `memory` | Save, search, list, delete facts across sessions |
| `contacts` | Store and search contacts — auto-resolves names for email/calendar |
| `files` | Save, read, list, delete user files in `~/.micropaw/files/` |
| `vision` | Analyze images via provider vision API |
| `pdf_read` | Extract text from PDFs with page ranges |
| `sessions` | List and export conversation history |
| `delegate` | Spin up sub-agents for parallel tasks |
| `cron` | Schedule recurring or one-shot tasks via LLM |
| `email` | Gmail inbox, read, send, search via IMAP/SMTP |
| `channel_send` | Send messages to any active channel — cross-channel forwarding and notifications |
| `calendar` | Google Calendar list, create, delete, search events |

Disable any tool:
```json
{ "permissions": { "disabled_tools": ["shell", "file_write"] } }
```

---

## Providers

| Provider | Type | Streaming | Tools | Vision |
|----------|------|-----------|-------|--------|
| **Claude** | Cloud | Yes | Yes | Yes |
| **GPT-4o** | Cloud | Yes | Yes | Yes |
| **Gemini** | Cloud | Yes | Yes | Yes |
| **Ollama** | Local | Yes | Yes | Yes |

Override models: `CLAUDE_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`, `OLLAMA_MODEL`

---

## RAG — Read Once, Ask Anywhere

### Read a file, PDF, or YouTube video on any channel. Ask about it later from any other channel. Zero config.

```
┌─ Telegram (Day 1) ─────────────────────────────────────────────────┐
│                                                                      │
│  You:        "Read /home/report.pdf"                                │
│  MicroPaw:   ✓ PDF read → chunked → embedded → stored              │
│              "Here's a summary of your report..."                   │
│                                                                      │
│  You:        "https://youtube.com/watch?v=abc123"                   │
│  MicroPaw:   ✓ transcript fetched → chunked → embedded → stored    │
│              "This is a 21-min talk about body language..."         │
│                                                                      │
├─ Discord (Next day) ───────────────────────────────────────────────┤
│                                                                      │
│  You:        "What did the report say about revenue?"               │
│  MicroPaw:  "Revenue grew 23% in Q4, with expenses down 10%..."   │
│                                                                      │
│  You:        "What was that YouTube video about?"                   │
│  MicroPaw:  "The talk was by Amy Cuddy about how posture affects   │
│               confidence and hormone levels..."                     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

### YouTube Transcripts — Zero Config

- Supports `youtube.com/watch`, `youtu.be`, and `youtube.com/shorts` URLs
- Prefers manual English captions, falls back to auto-generated
- If transcript unavailable, falls back to normal Playwright page rendering

**How it works — fully automatic, zero config:**

| Step | What happens |
|------|-------------|
| **Auto-ingest** | File, PDF, or web page read (>500 chars) → recursive chunking (~2k chars, 200 overlap) → embed → store |
| **Auto-retrieve** | Every user message → embed query → ANN vector search against all stored chunks |
| **Auto-inject** | Top 5 relevant chunks injected into system prompt as LLM context |

No extra tools, no commands, no user action needed. Just read, browse, or paste a link — and ask later.

### Under the hood

| Feature | Detail |
|---------|--------|
| **sqlite-vec ANN search** | [sqlite-vec](https://github.com/asg017/sqlite-vec) for fast approximate nearest neighbor search — no separate vector DB |
| **Three-path search** | sqlite-vec ANN → brute-force cosine → text LIKE fallback |
| **BLOB embeddings** | float32 binary storage, 5-10x faster than JSON serialization |
| **Dual-write ingest** | Every chunk written to both `document_chunks` and `vec_chunks` atomically |
| **Cascade delete** | Re-reading a file replaces all old chunks cleanly (both tables) |
| **Dimension tracking** | `kv_meta` table tracks embedding dimensions across model changes |
| **Safe model switching** | Change embedding model → embeddings cleared, text preserved, background re-index |
| **No row caps** | Memory and document search scan all entries — no artificial limits |
| **Cross-user isolation** | Each user's documents and memories are strictly separated |

> **Graceful fallback:** If you remove `sqlite-vec`, MicroPaw falls back to brute-force cosine similarity in pure Python. Slower on large collections, but fully functional.

### Embeddings (auto-detected, zero config)

```
Ollama (local, free) → OpenAI (if key exists) → fastembed/ONNX (offline) → text search fallback
```

Default model: `nomic-embed-text` (768 dims, 8k token context). Override in config:

```json
{ "embedding": { "provider": "ollama", "model": "snowflake-arctic-embed" } }
```

### Memory

- **SQLite + sqlite-vec** — single file, persists across restarts, shared across all channels
- **Document chunks** — BLOB embeddings indexed by sqlite-vec for ANN search
- **User memories** — explicit key-value facts via the `memory` tool, separate from document chunks

---

## Multi-Agent Delegation

```
You > Research quantum computing AND summarize today's AI news

MicroPaw > [delegate] "Research quantum computing breakthroughs"
            [delegate] "Summarize today's top AI news"

            Here's what I found on both fronts...
```

- **Full tool access** — sub-agents inherit all tools (search, browse, files, etc.)
- **No recursive spawning** — `delegate` is excluded from sub-agents, preventing infinite loops
- **Inline or background** — block for result, or fire-and-forget via message bus
- **Configurable** — max 25 LLM rounds (adjust via `context.max_subagent_iterations`)

---

## Security

- **Sub-agent isolation** — inherits tools but `delegate` always excluded (no recursive spawning)
- **Shell blocking** — 40+ patterns block reverse shells, `rm -rf /`, env leaks, eval/exec
- **Path security** — blocks `/proc`, `/sys`, `.ssh/`, `.env`, symlinks, path traversal
- **SSRF protection** — blocks localhost, private IPs, metadata endpoints, DNS rebinding
- **Auth** — timing-safe `secrets.compare_digest()` for all token comparisons
- **WebSocket limits** — 1MB max frame, 50 max headers, outbound truncation
- **Rate limiting** — per-user sliding window + burst detection
- **Config protection** — 0600 permissions, world-readable warning on load
- **Cross-origin safety** — strips Authorization headers on host change
- **No telemetry** — nothing phones home, ever

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `paw setup` | Interactive wizard — provider, API key, persona, permissions |
| `paw chat` | Terminal chat session |
| `paw start` | Launch all enabled channels |
| `paw connect <channel>` | Guided channel setup with live token validation |
| `paw test` | Validate provider + all channel tokens |
| `paw config` | Show configuration (secrets redacted) |
| `paw status` | System status overview |
| `paw doctor` | Diagnostics — Python, deps, disk, keys |
| `paw cron add "task" --every 6h` | Schedule a recurring task |
| `paw cron add "task" --once 2h` | Schedule a one-shot reminder |
| `paw cron list` | List all scheduled tasks |
| `paw cron remove <id>` | Remove a scheduled task |
| `paw cron enable <id>` | Turn on a task the assistant scheduled (`--off` to pause) |
| `paw allow <channel> <id>` | Let one of your accounts use the assistant (`--remove` to revoke) |
| `paw export` | Export conversations (JSON/Markdown) |
| `paw reset` | Delete all config and data |

Terminal commands: `/help`, `/new` (fresh conversation, this channel), `/rejoin` (back to shared), `/new all` (fresh everywhere), `/tools`, `/usage`, `/clear`, `/config`, `!cmd` (shell escape), `quit`

---

## Configuration

Config file: `~/.micropaw/config.json` (0600 permissions).

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `MICROPAW_PROVIDER` | Primary provider (`claude`/`openai`/`gemini`/`ollama`) |
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` | Gemini API key |
| `OLLAMA_HOST` | Ollama server URL |
| `BRAVE_API_KEY` | Brave Search API key |
| `SEARXNG_URL` | Self-hosted SearXNG URL |
| `TELEGRAM_BOT_TOKEN` | Telegram token |
| `DISCORD_BOT_TOKEN` | Discord token |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack tokens |

### Permissions

```json
{
  "permissions": {
    "allow_shell": false,
    "allow_file_write": false,
    "allow_web_browse": true,
    "confirm_dangerous": true,
    "disabled_tools": []
  }
}
```

### Persona

```json
{
  "persona": {
    "name": "MicroPaw",
    "personality": "friendly, helpful, and clever",
    "location": "San Francisco, CA",
    "system_prompt_extra": "Always respond in Spanish."
  }
}
```

Or use persona files for richer customization:
- `~/.micropaw/SOUL.md` — personality, values, behavioral guidelines (overrides `personality` field)
- `~/.micropaw/USER.md` — user preferences, timezone, language, personal context
- `~/.micropaw/SKILLS.md` — multi-step workflow hints (e.g., "when asked to email someone by name, search contacts first")

### Custom API Base URLs

Point any provider at a compatible server (vLLM, LM Studio, Azure OpenAI):
```json
{
  "provider_config": {
    "openai": {
      "base_url": "http://localhost:8000",
      "api_key": "not-needed",
      "model": "my-local-model"
    }
  }
}
```

---

## Installation Options

```bash
pip install micropaw
```

**From source:**
```bash
git clone https://github.com/manu-chauhan/micropaw.git
cd micropaw
pip install .
```

Install only what you need:
```bash
pip install "micropaw[telegram]"      # Telegram bot
pip install "micropaw[discord]"       # Discord bot
pip install "micropaw[slack]"         # Slack (Socket Mode)
pip install "micropaw[whatsapp]"      # WhatsApp (aiohttp webhook)
pip install "micropaw[browser]"       # Playwright browser automation
pip install "micropaw[pdf]"           # PDF reading
pip install "micropaw[embeddings]"    # Offline embeddings without Ollama (fastembed/ONNX, ~175 MB)
pip install "micropaw[all]"           # Everything
```

Docker:
```bash
docker compose run --rm micropaw chat              # Basic
docker compose --profile browser up                  # + Playwright
docker compose --profile full up                     # Everything
```

---

## Architecture

```
src/micropaw/          ~9,200 LOC source + ~5,150 LOC tests
 cli.py                 Click CLI (14 commands)
 config.py              Single Pydantic config model
 connector.py           Guided channel setup + live validation
 gateway.py             Central coordinator + rate limiter + bus + scheduler
 scheduler.py           Cron jobs + HEARTBEAT.md proactive mode
 bus.py                 Async message bus (bounded queues)
 security.py            SSRF + path security + DNS checks
 core/
   agent.py             Agent loop (streaming, tools, caching, persona files)
   subagent.py          Sub-agent loop (tools minus delegate, configurable cap)
   context.py           Context compaction + auto-summarization
   conversation.py      History + session management
 providers/             4 providers + failover (raw httpx, retry)
 channels/              6 channels
 tools/                 17 tools (cron, email, calendar, delegation, etc.)
 memory/                SQLite + vectors + embeddings + RAG chunker
```

Full documentation: [GitHub](https://github.com/manu-chauhan/micropaw)

---

## Development

```bash
pip install ".[dev]"
pytest                 # 389 tests
ruff check src/
mypy src/
```

Shell completion:
```bash
eval "$(_PAW_COMPLETE=bash_source paw)"    # Bash
eval "$(_PAW_COMPLETE=zsh_source paw)"     # Zsh
```

---

## License

MIT
