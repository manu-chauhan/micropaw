<div align="center">

<img src="https://raw.githubusercontent.com/manu-chauhan/just_images_extras/main/puppy.png" width="120" alt="MicroPaw">

# 🐾 MicroPaw

### A local-first AI agent that runs on your machine, talks on every platform, and keeps your keys to yourself.

**Your AI. Your machine. Your keys.**

6 channels. 4 providers. 11 tools. Shared memory everywhere. ~6,900 lines of Python. Zero SDK dependencies.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-268%20passed-brightgreen)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=flat&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/manuchauhan)

</div>

---

```bash
pip install .
paw setup     # pick provider, paste key
paw chat      # start talking
```

---

## Highlights

### Intelligence

- **Same brain, everywhere** — Start a conversation on Telegram, pick it up on Discord, ask from WhatsApp. Everything stays on your machine.

- **RAG out of the box** — Read a PDF, paste a YouTube URL, or browse a web page. Documents are auto-chunked, embedded, and searchable across all channels. Ask about them days later from any platform.

- **YouTube transcripts** — Paste a YouTube link and MicroPaw extracts the full transcript automatically. No API key, no extra dependency. Auto-indexed for RAG.

- **sqlite-vec ANN search** — Vector search powered by [sqlite-vec](https://github.com/asg017/sqlite-vec) directly inside SQLite. Scales to tens of thousands of chunks. Falls back to brute-force cosine if removed.

- **Multi-agent delegation** — The main agent spawns sub-agents for parallel tasks. Sub-agents inherit all tools except `delegate` (no recursive spawning). Max 25 iterations, configurable.

- **Context compaction** — Conversations never hit a wall. When context fills up, older messages are summarized by the LLM while the last 10 are kept verbatim. Unbounded conversations.

### Reliability

- **Automatic failover** — Provider chain with exponential backoff and Retry-After support. Claude down? Switches to GPT-4o, then Ollama. Keeps working.

- **Self-healing channels** — If a channel crashes, MicroPaw detects it, waits 5 seconds, and restarts. Other channels keep running.

- **Streaming everywhere** — Real-time token-by-token output on Terminal, Telegram (0.5s), Discord (0.8s), Slack (1.0s). No waiting for full responses.

- **Rate limiting** — Per-user sliding window + burst detection at the gateway. Protects your API keys from runaway requests.

### Security

- **Shell hardening** — 40+ blocked patterns: reverse shells, fork bombs, data exfiltration, `rm -rf /`, process substitution. Dangerous command confirmation toggle.

- **SSRF protection** — URL scheme whitelist, private IP range blocking, DNS rebinding detection, cloud metadata service blocking.

- **Path traversal prevention** — Symlink resolution, `..` detection, sensitive file blocking (`/etc/shadow`, `~/.ssh`, API keys).

- **WebSocket hardening** — 1MB frame limit, 50 max headers, origin validation, timing-safe authentication.

### Architecture

- **Zero SDK dependencies** — All 4 AI providers via raw httpx. No `anthropic`, `openai`, or `google-genai` packages. Switching providers is a config change.

- **11 built-in tools** — Web search (Brave → SearXNG → DuckDuckGo), browser automation, file I/O, shell, HTTP requests, memory, vision, PDF, sessions, delegation — all security-hardened.

- **Raw WebSocket** — WebChat channel implements RFC 6455 from scratch. No aiohttp. ~100 LOC.

- **Tool result caching** — Read-only tool calls cached per-message (LRU-32). Same query twice? Instant response, zero wasted tokens.

- **Fully local option** — Run entirely offline with Ollama. No data leaves your machine. No telemetry. No phone home. Ever.

- **Docker ready** — Multi-stage Dockerfile (base / browser / full). `docker compose up` with profiles. Named volume for persistence.

---

## Why MicroPaw?

| | **MicroPaw** | **OpenClaw** | **NanoClaw** | **PicoClaw** | **MimiClaw** |
|---|---|---|---|---|---|
| **Language** | Python | TypeScript | TypeScript | Go | C |
| **Codebase** | ~6,900 LOC | ~430k LOC | ~500 LOC | ~4k LOC | ~15k LOC |
| **Channels** | 6 | 15+ | 1 (WhatsApp) | 5 | 3 |
| **Tools** | 11 | 25 + 53 skills | SDK-dependent | 6 | 6 |
| **AI SDKs** | None — raw httpx | Internal framework | Anthropic SDK | None — raw HTTP | None — raw HTTP |
| **RAG / Vectors** | sqlite-vec ANN + fallback | sqlite-vec + BM25 | No | No | No |
| **Telemetry** | None, ever | Local JSONL (opt-out) | None | None | None |
| **License** | MIT | MIT | MIT | MIT | MIT |

**MicroPaw sits in the sweet spot** — full-featured like OpenClaw (RAG, 6 channels, 11 tools, multi-agent delegation) but lightweight like PicoClaw (~6,900 LOC, zero SDK dependencies). No telemetry, no bloat, no wrappers.

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
pip install .
paw setup && paw chat
```

**Multi-channel:**
```bash
pip install ".[telegram,discord]"
paw setup
paw connect telegram
paw connect discord
paw start
```

**Fully local (no internet):**
```bash
ollama pull llama3.1
pip install .
paw setup    # select Ollama
paw chat
```

> `micropaw` also works as a command name — both are identical.

---

## Tools

| Tool | What it does |
|------|-------------|
| `web_search` | Brave -> SearXNG -> DuckDuckGo fallback chain |
| `web_browse` | Full browser automation via Playwright |
| `http_request` | HTTP requests with SSRF protection per hop |
| `file_read` | Read files with path traversal protection |
| `file_write` | Create and modify files |
| `shell` | Execute commands (40+ dangerous patterns blocked) |
| `memory` | Save, search, list, delete facts across sessions |
| `vision` | Analyze images via provider vision API |
| `pdf_read` | Extract text from PDFs with page ranges |
| `sessions` | List and export conversation history |
| `delegate` | Spin up sub-agents for parallel tasks |

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

```
Primary: Claude -> Fallback: GPT-4o -> Fallback: Ollama
```

Override models: `CLAUDE_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`, `OLLAMA_MODEL`

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
Ollama (local, free) → OpenAI (if key exists) → sentence-transformers (offline) → text search fallback
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
| `paw export` | Export conversations (JSON/Markdown) |
| `paw reset` | Delete all config and data |

Terminal slash commands: `/help`, `/new`, `/tools`, `/usage`, `/clear`, `/config`, `quit`

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
    "system_prompt_extra": "Always respond in Spanish."
  }
}
```

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

```bash
git clone https://github.com/manu-chauhan/micropaw.git
cd micropaw
pip install .
```

Install only what you need:
```bash
pip install ".[telegram]"      # Telegram bot
pip install ".[discord]"       # Discord bot
pip install ".[slack]"         # Slack (Socket Mode)
pip install ".[whatsapp]"      # WhatsApp (aiohttp webhook)
pip install ".[browser]"       # Playwright browser automation
pip install ".[pdf]"           # PDF reading
pip install ".[embeddings]"    # Local sentence-transformers (if no Ollama/OpenAI)
pip install ".[all]"           # Everything
```

Docker:
```bash
docker compose run --rm micropaw chat              # Basic
docker compose --profile browser up                  # + Playwright
docker compose --profile full up                     # Everything
```

</details>

<details>
<summary><strong>Architecture</strong></summary>

```
src/micropaw/          6,873 LOC source + 3,101 LOC tests
 cli.py                 Click CLI (11 commands)
 config.py              Single Pydantic config model
 connector.py           Guided channel setup + live validation
 gateway.py             Central coordinator + rate limiter + bus
 bus.py                 Async message bus (bounded queues)
 security.py            SSRF + path security + DNS checks
 core/
   agent.py             Agent loop (streaming, tools, caching, errors)
   subagent.py          Sub-agent loop (tools minus delegate, configurable cap)
   context.py           Context compaction + auto-summarization
   conversation.py      History + session management
 providers/             4 providers + failover (raw httpx, retry)
 channels/              6 channels
 tools/                 11 tools
 memory/                SQLite + vectors + embeddings + RAG chunker
```

Full decision log: [`DECISIONS.md`](DECISIONS.md)

</details>

---

## Development

```bash
pip install ".[dev]"
pytest                 # 268 tests, ~1s
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
