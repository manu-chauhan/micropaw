<div align="center">

<img src="https://raw.githubusercontent.com/manu-chauhan/just_images_extras/main/puppy.png" width="180" alt="MicroPaw">

# 🐾 MicroPaw

### A local-first AI agent that runs on your machine, talks on every platform, and keeps your keys to yourself.

<h4>Your AI. Your model. Your machine.</h4>

6 channels. 4 providers. 17 tools. Cross-channel conversations. Shared memory everywhere. ~9,200 lines of Python. Zero SDK dependencies.

[![PyPI](https://img.shields.io/pypi/v/micropaw?style=for-the-badge)](https://pypi.org/project/micropaw/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-393%20passed-brightgreen?style=for-the-badge)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=for-the-badge)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/manuchauhan)

</div>

---

> **100% free with Ollama** — Run MicroPaw entirely on your machine with zero API keys, zero cost, zero data leaving your network. Just `ollama pull llama3.1` (or `phi3` / `gemma2:2b` on lighter hardware) and go. Cloud providers (Claude, GPT-4o, Gemini) are optional.

```bash
curl -LsSf https://raw.githubusercontent.com/manu-chauhan/micropaw/v0.4.0/install.sh | sh   # Linux, macOS, WSL
paw chat      # start talking (the installer already ran 'paw setup')
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/manu-chauhan/micropaw/v0.4.0/install.ps1 | iex"
```

Both scripts install [uv](https://docs.astral.sh/uv/) if it is missing, then `uv tool install micropaw`. No sudo, no admin rights. `pip install micropaw` also works. All routes are listed under Installation Options below.

---

## What Can You Do?

```
┌─ 🔍 Search & Browse ──────┐  ┌─ 📁 Files & Knowledge ────┐  ┌─ 📬 Communicate ──────────┐
│ Web search (3 engines)     │  │ Read files & PDFs          │  │ Gmail (read/send/search)   │
│ Browse pages interactively │  │ YouTube video Q&A          │  │ Google Calendar            │
│ Click, type, scroll        │  │ Save/manage user files     │  │ Scheduled reminders        │
│ Take & analyze screenshots │  │ RAG — ask later, anywhere  │  │ Proactive heartbeat tasks  │
└────────────────────────────┘  └────────────────────────────┘  └────────────────────────────┘

┌─ 🧠 Memory & Context ─────┐  ┌─ 🛠️ System ────────────────┐  ┌─ 🤖 Intelligence ─────────┐
│ Unified cross-channel chat │  │ Shell commands (hardened)   │  │ Multi-agent delegation     │
│ Cross-channel messaging    │  │ HTTP requests (SSRF-safe)  │  │ Context compaction         │
│ Vector search (sqlite-vec) │  │ Vision (image analysis)    │  │ Streaming on all channels  │
│ Auto-embed everything      │  │ Location-aware ("near me") │  │ Provider failover chain    │
└────────────────────────────┘  └────────────────────────────┘  └────────────────────────────┘
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

- **Custom persona & skills** — Create `~/.micropaw/SOUL.md` for personality, `~/.micropaw/USER.md` for user context, and skills in `~/.micropaw/skills/<name>/SKILL.md` for multi-step how-tos (bundled ones ship with MicroPaw; `/skills` lists them). Your assistant, your rules.

### Reliability

- **Automatic failover** — Provider chain with exponential backoff and Retry-After support. Claude down? Switches to GPT-4o, then Ollama. Keeps working.

- **Self-healing channels** — If a channel crashes, MicroPaw detects it, waits 5 seconds, and restarts. Other channels keep running.

- **Streaming everywhere** — Real-time token-by-token output on Terminal, Telegram (0.5s), Discord (0.8s), Slack (1.0s). No waiting for full responses.

- **Rate limiting** — Per-user sliding window + burst detection at the gateway. Protects your API keys from runaway requests.

### Security

- **Owner-only by default**: only accounts you list can talk to it. A stranger is told their ID once and refused. Add your own accounts with `paw allow telegram <id>`.

- **Asks before it acts**: shell commands, sending email or messages, calendar changes, HTTP calls that change something, and writes outside your workspace (`~/micropaw`) wait for your yes. On Telegram, Discord, Slack and WhatsApp you reply `yes <id>`; the terminal asks inline. `always <id>` remembers that exact action.

- **Knows when it has read something risky**: once a conversation has read a web page, an email, a file, command output or an MCP tool result, saved "always" rules stop applying and every outgoing action asks again, so a page can't quietly make it send your data somewhere. Only `/new` clears this.

- **Audit log**: every tool call and every approval goes to `~/.micropaw/audit.jsonl`. See it with `paw log`.

- **No hidden standing tasks**: recurring tasks the assistant schedules start paused until you reply `/cron enable <id>`.

- **Shell hardening**: commands run without your API keys or tokens in their environment, and 40+ patterns (reverse shells, fork bombs, `rm -rf /`) are blocked. The blocklist is a speed bump, not a sandbox, so shell stays off unless you turn it on.

- **SSRF protection** — URL scheme whitelist, private IP range blocking, DNS rebinding detection, cloud metadata service blocking.

- **Path traversal prevention** — Symlink resolution, `..` detection, sensitive file blocking (`/etc/shadow`, `~/.ssh`, API keys).

- **WebSocket hardening** — 1MB frame limit, 50 max headers, origin validation, timing-safe authentication.

### Architecture

- **Zero SDK dependencies** — All 4 AI providers via raw httpx. No `anthropic`, `openai`, or `google-genai` packages. Switching providers is a config change.

- **16 built-in tools** — Web search (Brave → SearXNG → DuckDuckGo), browser automation, file I/O, shell, HTTP requests, memory, contacts, user files, vision, PDF, sessions, delegation, cron scheduling, email (Gmail), calendar (Google Calendar) — all security-hardened.

- **Raw WebSocket** — WebChat channel implements RFC 6455 from scratch. No aiohttp. ~100 LOC.

- **Tool result caching** — Read-only tool calls cached per-message (LRU-32). Same query twice? Instant response, zero wasted tokens.

- **Fully local option** — Run entirely offline with Ollama. No data leaves your machine. No telemetry. No phone home. Ever.

- **Docker ready**: uv-built slim image, non-root, health check. `docker compose up` with an optional Ollama profile. Named volume for persistence.

---

## Why MicroPaw?

| | **MicroPaw** | **OpenClaw** | **NanoClaw** | **PicoClaw** | **MimiClaw** |
|---|---|---|---|---|---|
| **Language** | Python | TypeScript | TypeScript | Go | C |
| **Codebase** | ~9,200 LOC | ~430k LOC | ~500 LOC | ~4k LOC | ~15k LOC |
| **Channels** | 6 | 15+ | 1 (WhatsApp) | 5 | 3 |
| **Tools** | 17 (50 actions) | 25 + 53 prompt templates | SDK-dependent | 6 | 6 |
| **AI SDKs** | None — raw httpx | Internal framework | Anthropic SDK | None — raw HTTP | None — raw HTTP |
| **RAG / Vectors** | sqlite-vec ANN + fallback | sqlite-vec + BM25 | No | No | No |
| **Telemetry** | None, ever | Local JSONL (opt-out) | None | None | None |
| **License** | MIT | MIT | MIT | MIT | MIT |

**MicroPaw sits in the sweet spot** — full-featured like OpenClaw (RAG, 6 channels, 17 tools, scheduling, email, calendar, multi-agent delegation) but lightweight like PicoClaw (~9,200 LOC, zero SDK dependencies). No telemetry, no bloat, no wrappers.

> Started at **4,667 LOC** in the first commit. Grew to **~9,200 source + ~5,150 test LOC** while adding 6 channels, 17 tools, RAG, multi-agent delegation, scheduling, email, calendar — and 389 tests. Every feature earned its lines.

---

## One AI, Every Platform

<div align="center">

<br>

![Terminal](https://img.shields.io/badge/Terminal-%23121011?style=for-the-badge&logo=gnubash&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)
![Discord](https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-4A154B?style=for-the-badge&logo=slack&logoColor=white)
![WhatsApp](https://img.shields.io/badge/WhatsApp-25D366?style=for-the-badge&logo=whatsapp&logoColor=white)
![WebChat](https://img.shields.io/badge/WebChat-0088CC?style=for-the-badge&logo=websocket&logoColor=white)

<br>

### Talk on Telegram during the day. Switch to Discord at night. Ask from WhatsApp on the go.

**Your assistant carries the full conversation and memory across every channel — same personality, same knowledge, same context. Set up once, run everywhere.**

<br>

> **Zero telemetry. Zero data collection. MicroPaw never phones home — your conversations, API keys, and data stay on your machine. Always.**

</div>

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
| **OpenAI** (and any OpenAI-compatible server) | Cloud or local | Yes | Yes | Yes |
| **Gemini** | Cloud | Yes | Yes | Yes |
| **Ollama** | Local | Yes | Yes | Yes |

Override models: `CLAUDE_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`, `OLLAMA_MODEL`

---

## Any Model, Local or Cloud

Every feature works with a fully local setup, a cloud model, or a mix you choose.

| Job | Setting | Local example | Cloud example |
|-----|---------|---------------|---------------|
| Chat and tools | `provider` | `ollama` | `claude` (default model `claude-sonnet-5-5`) |
| Summaries, sub-agents | `models.fast` | `ollama:qwen3.6` | `claude:claude-haiku-5-5` |
| Photos | `models.vision` | `ollama:gemma4:e4b` | `openai:<model>` |
| Voice notes | `voice.stt` | `local:<model>` (a whisper.cpp or faster-whisper server at `voice.stt_url`) | `groq:whisper-large-v3-turbo` |
| Search memory | `embedding` | Ollama `nomic-embed-text` | OpenAI embeddings |

Model settings are `provider:model`. Besides `claude`, `openai`, `gemini` and `ollama`, these names work without extra setup: `lmstudio`, `llamacpp`, `vllm` (local, no key) and `openrouter`, `groq`, `deepseek`, `mistral`, `together` (key from `<NAME>_API_KEY`). A text-only local model can still use photos: set `models.vision` and the vision model describes them first.

```json
{
  "provider": "ollama",
  "models": { "fast": "ollama:qwen3.6", "vision": "ollama:gemma4:e4b" },
  "voice": { "stt": "local:whisper-small", "stt_url": "http://localhost:8000" }
}
```

---

## MCP Servers

Add any [Model Context Protocol](https://modelcontextprotocol.io) server that runs over stdio, and its tools become MicroPaw tools. This is how you plug in reverse-engineering tools (REA, GhidraMCP, jadx), game engines (Godot, Unity) or Blender without MicroPaw carrying that code.

```json
{
  "mcp_servers": {
    "blender": { "command": "uvx", "args": ["blender-mcp"], "allow": ["get_scene_info"] }
  }
}
```

- Each server gets your environment minus API keys, tokens and secrets, plus only the `env` you list.
- Every call asks you first, except tool names in `allow`, and even those ask once the conversation has read outside content.
- Pin versions in `args` (for example `package@1.2.3`) and install servers only from their official repos.

---

## RAG — Read Once, Ask Anywhere

<h3 align="center">
  Read a file, PDF, or YouTube video on any channel.<br>
  Ask about it later from any other channel. Zero config.
</h3>

```
┌─ 📱 Telegram (Day 1) ───────────────────────────────────────────────┐
│                                                                      │
│  You:        "Read /home/report.pdf"                                │
│  MicroPaw:   ✓ PDF read → chunked → embedded → stored              │
│              "Here's a summary of your report..."                   │
│                                                                      │
│  You:        "https://youtube.com/watch?v=abc123"                   │
│  MicroPaw:   ✓ transcript fetched → chunked → embedded → stored    │
│              "This is a 21-min talk about body language..."         │
│                                                                      │
├─ 💬 Discord (Next day) ─────────────────────────────────────────────┤
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
- **SSRF protection**: blocks every non-public address (including 100.64.0.0/10), checks the address actually connected to, so DNS rebinding fails, and re-checks the browser after redirects
- **Auth** — timing-safe `secrets.compare_digest()` for all token comparisons
- **WebSocket limits** — 1MB max frame, 50 max headers, outbound truncation
- **Rate limiting** — per-user sliding window + burst detection
- **Config protection** — 0600 permissions, world-readable warning on load
- **Cross-origin safety**: a redirect to another site never gets your headers, and a POST is never re-sent to another site
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
| `paw log` | Recent tool calls and approvals (audit log) |
| `paw cron enable <id>` | Turn on a task the assistant scheduled (`--off` to pause) |
| `paw allow <channel> <id>` | Let one of your accounts use the assistant (`--remove` to revoke) |
| `paw export` | Export conversations (JSON/Markdown) |
| `paw goal "<task>" --check "<cmd>"` | Work on a task in rounds until the check passes (`--max`, `--budget`, `--agent`) |
| `paw reset` | Delete all config and data |

Terminal commands: `/help`, `/new` (fresh conversation, this channel), `/rejoin` (back to shared), `/new all` (fresh everywhere), `/tools`, `/usage`, `/clear`, `/config`, `/memory` (what it remembers), `/forget <key>`, `/cron enable <id>`, `/search <words>` (past conversations), `/skills`, `/goal <task> [--check "<cmd>"] [--max N] [--budget USD]` (also `/goal status`, `/goal stop`), `!cmd` (shell escape), `quit`

Goals: each round starts fresh from the goal, a progress file and the last check output. Only the check command's exit code 0 ends it as done. It also stops at `--max` rounds (default 20), 2 hours, `--budget`, the same tool call three times in a row, or three rounds with no change. A goal that uses a cloud model needs `--budget`. If any round reads outside content, every later round asks before side effects.

---

<details>
<summary><strong>Configuration</strong></summary>

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
- `~/.micropaw/skills/<name>/SKILL.md` — a skill: front matter with `name` and `description`, then the steps (e.g., "when asked to email someone by name, search contacts first"). The prompt lists names and one-line descriptions; the agent loads a body when it fits. The agent can propose a skill; it only takes effect after you approve the full text.

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

</details>

<details>
<summary><strong>Installation Options</strong></summary>

**Installer scripts.** The one-liners above point at a release tag. To read a script before you run it:

```bash
curl -LsSfO https://raw.githubusercontent.com/manu-chauhan/micropaw/v0.4.0/install.sh
less install.sh
sh install.sh --extras telegram,discord        # also: --version 0.4.0, --no-setup, --modify-path
```

```powershell
irm https://raw.githubusercontent.com/manu-chauhan/micropaw/v0.4.0/install.ps1 -OutFile install.ps1
notepad install.ps1
.\install.ps1 -Extras telegram,discord          # also: -Version 0.4.0, -NoSetup
```

Each release lists the SHA256 of both scripts in its notes (and in `SHA256SUMS`). The scripts only change your user directory. `install.sh` prints the `PATH` line to add; it edits your shell files only with `--modify-path`.

**Other routes.**
```bash
uv tool install micropaw             # uv (recommended: its own environment, 'paw' on PATH)
pipx install micropaw                # pipx
python -m venv .venv && .venv/bin/pip install micropaw   # pip in a virtual environment
git clone https://github.com/manu-chauhan/micropaw.git && cd micropaw && pip install -e .   # from source
```

**Add features later.** Do not run `pip` inside a uv tool or pipx install. Use:
```bash
paw extras add telegram discord      # slack whatsapp browser pdf embeddings
```
It detects how MicroPaw was installed and runs the matching command (`uv tool install --reinstall "micropaw[...]"`, `pipx inject`, or `pip install`).

**Update.** `paw update` shows the installed and latest version and the exact command, and runs it after you confirm (`--yes` skips the question). It stops the service first and starts it again after. Nothing updates on its own.

**Run in the background.**
```bash
paw service install      # systemd user unit (Linux), LaunchAgent (macOS), Task Scheduler task (Windows)
paw service status       # also: start, stop, logs, uninstall
```
The service runs `paw start --headless`: no terminal channel, restarted after a crash (5 s, backing off to 60 s, at most 10 restarts per hour), one copy per machine. It never holds your keys. It reads `~/.micropaw/config.json` and `~/.micropaw/.env`.
- Linux: needs systemd. `install` also turns on linger so the service survives logout; if that needs root it prints the command. Without systemd (containers, WSL without it) use Docker or a crontab `@reboot` line.
- macOS: launchd does not see variables exported in `.zshrc`. Put keys in `~/.micropaw/.env`. The install-time `PATH` is written into the agent so `ollama`, `npx` and `uvx` resolve.
- Windows: a hidden task that starts at logon, keeps running on battery, and has no time limit. `paw service stop` stops it cleanly.
- `paw doctor` checks the service, linger, config file mode and open ports. `paw doctor --quiet` prints nothing and exits 0 when the gateway runs and the database opens.

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

**Docker.** The image is built with uv on `python:3.13-slim-trixie`, runs as a non-root user (uid 10001), keeps everything in one volume (`/home/paw/.micropaw`), and has a health check (`paw doctor --quiet`).
```bash
docker compose run --rm micropaw paw setup          # once: provider and keys, saved in the volume
docker compose up -d                                # run in the background
docker compose run --rm micropaw paw chat           # chat in a terminal
docker compose --profile ollama up -d               # add a local model server (Ollama host: http://ollama:11434)
docker build --build-arg EXTRAS=telegram,pdf,browser .   # choose the extras in the image
```
Compose reads keys from a `.env` file at run time. They are not copied into the image. Ollama's port is not published. WebChat is published on `127.0.0.1` only. The container is the boundary for shell commands, so the image and compose file set `MICROPAW_SANDBOX=off`.

**Hosting.** MicroPaw needs a disk that survives restarts, because the database is SQLite in WAL mode, which does not work on a network filesystem. Check prices on each provider's page before you choose.
- Fly.io: no free tier for new organizations. Needs a volume mounted at `/home/paw/.micropaw`.
- Render: free web services have no persistent disk, so they do not suit MicroPaw. A paid service with a disk does.
- Any VPS (for example Hetzner Cloud): install with the script and run `paw service install`, or use compose.
- Oracle Cloud Always Free (Arm, 2 OCPU, 12 GB): works, but idle instances can be reclaimed.
- Raspberry Pi 5 with a small local model: speed varies a lot with the model and cooling. Measure with `ollama run <model> --verbose`.

</details>

<details>
<summary><strong>Architecture</strong></summary>

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

Full decision log: [`DECISIONS.md`](DECISIONS.md)

</details>

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
