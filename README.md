# MicroClaw

**Your AI. Your machine. Your keys.**

A personal AI assistant in ~6,200 lines of Python. No SDK bloat, no cloud lock-in, no telemetry.

```
4 AI providers    Claude, GPT-4o, Gemini, Ollama (local)
6 channels        Terminal, Telegram, Discord, Slack, WhatsApp, WebChat
11 tools          Search, browse, files, shell, memory, vision, PDF, delegation
0 SDK deps        Raw HTTP to every provider via httpx
227 tests         <1 second
```

```bash
pip install .
microclaw setup     # pick your provider, paste your key
microclaw chat      # start talking
```

---

## Why MicroClaw

| | MicroClaw | Typical AI Wrapper | Enterprise Platform |
|---|---|---|---|
| **Lines of code** | ~6,200 | ~500 | 200,000+ |
| **AI providers** | 4 (any model) | 1 | 2-3 |
| **Channels** | 6 simultaneous | 1 | 3-5 |
| **Tools** | 11 | 0-2 | 10+ |
| **SDK dependencies** | 0 | 1-3 | 10+ |
| **Local/offline** | Yes (Ollama) | No | No |
| **Multi-agent** | Yes | No | Sometimes |
| **Failover** | Automatic | No | Sometimes |
| **Self-healing** | Auto-restart on crash | No | Sometimes |
| **Setup time** | 2 minutes | 2 minutes | 2 weeks |

---

## Key Features

### Zero SDK Philosophy

Every AI provider talks raw HTTP via `httpx`. No `anthropic` package. No `openai` package. No `google-genai` package.

- Fewer dependencies, fewer breakages
- Full control over what goes over the wire
- Switching providers is a config change, not a rewrite

### One AI, Every Platform

Set up once. Your AI runs on **all platforms simultaneously** with shared memory.

```
Terminal  Telegram  Discord  Slack  WhatsApp  WebChat
    \        |        |       |       |        /
     \       |        |       |       |       /
      +------+--------+-------+-------+------+
                       |
                   [Gateway]
                       |
              +--------+--------+
              |        |        |
           [Agent]  [Tools]  [Memory]
              |                  |
         [Provider]         [SQLite + vectors]
```

```bash
microclaw connect telegram    # guided setup, validates token live
microclaw connect discord     # auto-generates bot invite URL
microclaw start               # launch ALL channels at once
```

Your assistant remembers a conversation from Telegram when you talk to it from Discord. Same brain, everywhere.

| Channel | Setup | Key Feature |
|---------|-------|-------------|
| **Terminal** | Built-in | Streaming, slash commands, tool visibility |
| **Telegram** | `connect telegram` | Phone + desktop, user whitelist |
| **Discord** | `connect discord` | Per-server bot, auto invite URL |
| **Slack** | `connect slack` | Socket Mode, works behind firewalls |
| **WhatsApp** | `connect whatsapp` | Meta Business API webhook |
| **WebChat** | `connect webchat` | Raw RFC 6455 WebSocket, real-time streaming |

### 11 Built-In Tools

| Tool | What it does |
|------|-------------|
| `web_search` | Brave -> SearXNG -> DuckDuckGo fallback chain |
| `web_browse` | Full browser automation via Playwright |
| `http_request` | HTTP requests with SSRF protection per hop |
| `file_read` | Read files with path security (blocks `/proc`, `.ssh/`, `.env`) |
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

### Multi-Agent Delegation

The main agent can delegate sub-tasks to independent sub-agents:

```
You > Research quantum computing AND summarize today's AI news

MicroClaw > [delegate] "Research quantum computing breakthroughs"
            [delegate] "Summarize today's top AI news"

            Here's what I found on both fronts...
```

- **Full tool access** -- sub-agents inherit all tools (search, browse, files, etc.)
- **No recursive spawning** -- `delegate` tool is excluded from sub-agents, preventing infinite loops
- **Inline** -- blocks until done, returns result
- **Background** -- fire-and-forget via message bus, result delivered asynchronously
- **Configurable** -- max 25 LLM rounds per sub-agent (adjustable via `context.max_subagent_iterations`)

### 4 Providers with Automatic Failover

| Provider | Type | Streaming | Tools | Vision |
|----------|------|-----------|-------|--------|
| **Claude** (Anthropic) | Cloud | Yes | Yes | Yes |
| **GPT-4o** (OpenAI) | Cloud | Yes | Yes | Yes |
| **Gemini** (Google) | Cloud | Yes | Yes | Yes |
| **Ollama** | Local | Yes | Yes | Yes |

```
Primary: Claude -> Fallback: GPT-4o -> Fallback: Ollama (local)
```

Retries with exponential backoff on 429/503 before switching. If Claude is down, your assistant keeps working.

Override models via env vars: `CLAUDE_MODEL`, `OPENAI_MODEL`, `GEMINI_MODEL`, `OLLAMA_MODEL`.

### Memory & Vector Search

- **SQLite + auto-detected vector search** -- persists across restarts
- **Shared across all channels** -- remember on Telegram, recall from Discord
- **Auto-summarizes** old context when conversations grow large
- **Export**: `microclaw export` dumps history as JSON or Markdown

Embedding auto-detect priority (zero config):

```
Ollama (local, free) -> OpenAI (if key exists) -> sentence-transformers (offline) -> text search fallback
```

Default model: `nomic-embed-text` (768 dims, 8192 token context, MTEB 59.4). Override in config:

```json
{ "embedding": { "provider": "ollama", "model": "snowflake-arctic-embed" } }
```

### Web Search -- Zero Config

```
Brave (if API key) -> SearXNG (3 public instances) -> DuckDuckGo (last resort)
```

Works out of the box. No API key needed. Upgrade to Brave for best quality: set `BRAVE_API_KEY`.

### Self-Healing Channels

Channels run as background tasks. If one crashes, MicroClaw detects and restarts it. Other channels keep running.

```
ERROR Slack task crashed: ConnectionError -- restarting in 5s
INFO  Slack channel connected.
```

---

## Quick Start

**Terminal only:**
```bash
pip install .
microclaw setup && microclaw chat
```

**Multi-channel:**
```bash
pip install ".[telegram,discord]"
microclaw setup
microclaw connect telegram
microclaw connect discord
microclaw start               # all channels at once
```

**Fully local (no internet):**
```bash
ollama pull llama3.1
pip install .
microclaw setup               # select Ollama
microclaw chat
```

---

## Installation

```bash
git clone https://gitlab.com/MANU-CHAUHAN/microclaw.git
cd microclaw
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
pip install ".[embeddings]"    # Local sentence-transformer embeddings (auto-detected if Ollama/OpenAI unavailable)
pip install ".[all]"           # Everything
```

**Docker:**
```bash
docker compose run --rm microclaw chat              # Basic
docker compose --profile browser up                  # + Playwright
docker compose --profile full up                     # Everything
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `microclaw setup` | Interactive wizard -- provider, API key, persona, permissions |
| `microclaw chat` | Terminal chat session |
| `microclaw start` | Launch all enabled channels |
| `microclaw connect [channel]` | Guided channel setup with live token validation |
| `microclaw test` | Validate provider + all channel tokens |
| `microclaw config` | Show configuration (secrets redacted) |
| `microclaw status` | System status overview |
| `microclaw doctor` | Diagnostics -- Python, deps, disk, keys |
| `microclaw export` | Export conversations (JSON/Markdown) |
| `microclaw reset` | Delete all config and data |

**Terminal slash commands:** `/help`, `/new`, `/tools`, `/usage`, `/clear`, `/config`, `quit`

---

## Configuration

Config: `~/.microclaw/config.json` (0600 permissions, warns if world-readable).

### Environment Variables

All config values can be overridden via env vars. See `.env.example` for the full list.

| Variable | Purpose |
|----------|---------|
| `MICROCLAW_PROVIDER` | Primary provider (`claude`/`openai`/`gemini`/`ollama`) |
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` | Gemini API key |
| `OLLAMA_HOST` | Ollama server URL |
| `CLAUDE_MODEL` | Override Claude model |
| `OPENAI_MODEL` | Override OpenAI model |
| `GEMINI_MODEL` | Override Gemini model |
| `OLLAMA_MODEL` | Override Ollama model |
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
    "name": "MicroClaw",
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

---

## Security

- **Sub-agent isolation** -- inherits tools but `delegate` is always excluded (no recursive spawning)
- **Shell blocking** -- 40+ regex patterns block reverse shells, `rm -rf /`, env leaks, eval/exec, download-and-execute
- **Dangerous command confirmation** -- `rm`, `sudo`, `git push` require explicit confirmation
- **Path security** -- blocks `/proc`, `/sys`, `.ssh/`, `.env`, symlinks, path traversal
- **SSRF protection** -- blocks localhost, private IPs, metadata endpoints, DNS rebinding
- **Auth** -- timing-safe `secrets.compare_digest()` for all token comparisons
- **WebSocket limits** -- 1MB max frame, 50 max headers, outbound truncation
- **Rate limiting** -- per-user sliding window + burst detection
- **Config protection** -- 0600 permissions, world-readable warning on load
- **Cross-origin redirect safety** -- strips Authorization headers on host change
- **No telemetry** -- nothing phones home, ever

---

## Architecture

```
src/microclaw/          ~6,240 LOC source + ~2,450 LOC tests
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
 memory/                SQLite + vectors + embeddings
```

Full decision log: [`DECISIONS.md`](DECISIONS.md)

---

## Development

```bash
pip install ".[dev]"
pytest                 # 227 tests, <1s
ruff check src/
mypy src/
```

Shell completion:
```bash
eval "$(_MICROCLAW_COMPLETE=bash_source microclaw)"    # Bash
eval "$(_MICROCLAW_COMPLETE=zsh_source microclaw)"     # Zsh
```

---

## License

MIT
