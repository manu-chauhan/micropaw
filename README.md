# MicroClaw

**Your AI. Your machine. Your keys.**

A personal AI assistant that runs on your machine, talks to any LLM, works across **6 messaging platforms simultaneously**, and does it all in ~5,800 lines of Python. No SDK bloat, no cloud lock-in, no telemetry.

```bash
pip install .
microclaw setup     # pick your AI provider, paste your key
microclaw chat      # start talking
```

That's it. You're chatting with an AI that can search the web, browse pages, read/write files, execute shell commands, analyze images, read PDFs, and remember things across conversations — all from your terminal.

Want the same AI on Telegram? Discord? Slack? All at once?

```bash
microclaw connect telegram    # guided setup, validates your token live
microclaw connect discord     # generates bot invite URL for you
microclaw start               # launch ALL channels simultaneously
```

**One AI. Every platform. Shared memory.** Your assistant remembers everything — whether you talked to it from your terminal, Telegram, or Discord. Same brain, everywhere.

---

## Why MicroClaw?

Most AI assistants are either **too bloated** (200K+ lines, dozens of SDKs, framework hell) or **too simple** (a wrapper around one API with no tools). MicroClaw is neither.

| | MicroClaw | Typical AI Wrapper | Enterprise AI Platform |
|---|---|---|---|
| **Lines of code** | ~5,800 | ~500 | 200,000+ |
| **AI providers** | 4 (Claude, GPT, Gemini, Ollama) | 1 | 2-3 |
| **Channels** | 6 (Terminal, Telegram, Discord, Slack, WhatsApp, WebChat) | 1 | 3-5 |
| **Tools** | 10 (web, files, shell, memory, vision, PDF) | 0-2 | 10+ |
| **SDK dependencies** | 0 (raw HTTP to every provider) | 1-3 | 10+ |
| **Local/offline mode** | Yes (Ollama) | No | No |
| **Your keys stay local** | Yes | Maybe | No |
| **Failover between providers** | Yes | No | Sometimes |
| **Multi-platform at once** | Yes (all 6) | No | Sometimes |
| **Setup time** | 2 minutes | 2 minutes | 2 weeks |

### Zero SDK Philosophy

Every AI provider talks raw HTTP via `httpx`. No `anthropic` package. No `openai` package. No `google-genai` package. This means:
- **Fewer dependencies** — less to install, less to break
- **Faster startup** — no heavy SDK initialization
- **Full control** — you see exactly what goes over the wire
- **Provider-agnostic** — switching providers is a config change, not a rewrite

---

## One AI, Every Platform

This is MicroClaw's superpower. Set up once — your AI runs on **all your platforms simultaneously** with shared memory and conversation history.

```
┌──────────┐    ┌──────────┐    ┌──────────┐
│ Terminal  │    │ Telegram  │    │ Discord  │
│ (You)     │    │ (Mobile)  │    │ (Server) │
└────┬──────┘    └─────┬─────┘    └─────┬────┘
     │                 │                │
     └────────┬────────┴────────┬───────┘
              │                 │
         ┌────▼─────┐    ┌─────▼──────┐
         │ Gateway  │───→│   Agent    │
         │ (Router) │    │  (Brain)   │
         └──────────┘    └─────┬──────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
        ┌─────▼─────┐  ┌──────▼──────┐  ┌──────▼──────┐
        │ Provider  │  │   Tools     │  │  Memory     │
        │ (LLM)    │  │ (10 tools)  │  │ (SQLite)    │
        └───────────┘  └─────────────┘  └─────────────┘
```

### How It Works

1. **You set up your AI once** — pick a provider, paste your API key
2. **Connect channels** — `microclaw connect telegram`, `microclaw connect discord`, etc.
3. **Launch everything** — `microclaw start` boots all enabled channels
4. **Talk from anywhere** — your AI responds on every platform with the same context

Your assistant remembers a conversation you had on Telegram when you later talk to it from Discord. Memory is shared. Conversations persist across restarts.

### Supported Channels

| Channel | Setup | How It Connects | Key Feature |
|---------|-------|-----------------|-------------|
| **Terminal** | Zero config | Built-in REPL | Streaming, slash commands, markdown, tool visibility |
| **Telegram** | `microclaw connect telegram` | BotFather token, live validated | Works on phone + desktop, user whitelist |
| **Discord** | `microclaw connect discord` | Auto-generates invite URL | Per-server bot, user whitelist |
| **Slack** | `microclaw connect slack` | Socket Mode — no public URL | Works behind firewalls, multi-workspace |
| **WhatsApp** | `microclaw connect whatsapp` | Meta Business API webhook | End-to-end on WhatsApp, phone whitelist |
| **WebChat** | `microclaw connect webchat` | Raw WebSocket (RFC 6455) | Connect any web frontend, real-time streaming |

Every `microclaw connect` command walks you through setup step-by-step and **validates your tokens live** before saving — no guessing if things work.

### Run All Channels at Once

```bash
microclaw start
```

```
INFO  Gateway running with 4 channel(s) using claude provider
INFO  Terminal channel ready
INFO  Telegram bot @YourBot started
INFO  Discord bot MyAssistant#1234 connected
INFO  WebChat listening on ws://127.0.0.1:8765
```

If a channel fails to start (bad token, network issue), the gateway **reports the failure and keeps running** with the remaining channels. No single broken channel takes down your whole setup.

### Verify Before Launch

```bash
microclaw test    # validates provider + ALL channel tokens without starting
```

```
✓ Claude API key valid (claude-sonnet-4-20250514)
✓ Telegram bot @YourBot reachable
✓ Discord bot MyAssistant#1234 reachable
✗ Slack — invalid bot token
```

---

## What Can It Do?

MicroClaw is a tool-using AI agent. It doesn't just chat — it acts.

### Tools

| Tool | Description |
|------|-------------|
| `web_search` | Search the web (SearXNG → DuckDuckGo fallback, or Brave) |
| `web_browse` | Browse any URL and extract content (Playwright) |
| `http_request` | Make HTTP requests with SSRF protection |
| `file_read` | Read any file on your system (with path security) |
| `file_write` | Write and create files |
| `shell` | Execute shell commands (sandboxed, 40+ dangerous patterns blocked) |
| `memory` | Long-term memory — save, search, list, delete facts |
| `vision` | Analyze images via your AI provider's vision API |
| `pdf_read` | Read and extract text from PDFs |
| `sessions` | Manage conversation sessions |

All tools can be individually disabled via config:
```json
{
  "permissions": {
    "disabled_tools": ["shell", "file_write"]
  }
}
```

### Memory & Context

- **Long-term memory** with SQLite + vector search — persists across restarts
- **Shared across all channels** — remember something on Telegram, recall it from Discord
- **Embeddings**: local sentence-transformers (no API needed), OpenAI, or Ollama
- **Auto-summarizes** old conversation when context grows large (never deletes history)
- **Per-user, per-platform** conversation tracking

### Media

- **Image analysis** via provider vision APIs (Claude, OpenAI, Gemini, Ollama)
- **PDF extraction** — read and summarize PDF documents

### Web Search — Smart Fallback Chain

MicroClaw uses a multi-backend search system with automatic failover:

```
Brave (if API key set) → SearXNG (3 public instances) → DuckDuckGo (last resort)
```

**Zero config** — works out of the box using free SearXNG public instances. No API key needed.

| Backend | Quality | Cost | API Key | Notes |
|---------|---------|------|---------|-------|
| **Brave Search** | Best | ~1,000 free/month | Yes | Independent 30B+ page index |
| **SearXNG** | High | Free, unlimited | No | Aggregates Google/Bing/DDG results |
| **DuckDuckGo** | Low | Free, unlimited | No | Instant answers only, not full results |

**Upgrade to Brave** (optional, best results):
```json
{
  "search": {
    "brave_api_key": "BSA..."
  }
}
```

**Self-host SearXNG** (optional, full control):
```bash
docker run -d -p 8888:8080 searxng/searxng
```
```json
{
  "search": {
    "searxng_url": "http://localhost:8888"
  }
}
```

### Example

```
You > Search for the latest Python 3.13 features and summarize them

MicroClaw > [web_search] query="Python 3.13 new features"
            [web_browse] url="https://docs.python.org/3.13/whatsnew/3.13.html"

            Python 3.13 brings several notable features:

            - **Free-threaded mode** — experimental support for running
              without the GIL (PEP 703)
            - **JIT compiler** — experimental copy-and-patch JIT (PEP 744)
            - **Improved error messages** — better tracebacks with color
            ...

You > Save that to a file called python313.md

MicroClaw > [file_write] path="python313.md"
            Done! Saved to python313.md (847 bytes)

You > Remember that I'm interested in Python release notes

MicroClaw > [memory] action="save"
            Got it — I'll remember that.
```

Tool calls are visible in the terminal so you always know what your agent is doing.

---

## Supported Providers

| Provider | Type | Model | Streaming | Tool Use | Vision |
|----------|------|-------|-----------|----------|--------|
| **Claude** (Anthropic) | Cloud | claude-sonnet-4-20250514 | Yes | Yes | Yes |
| **GPT-4o** (OpenAI) | Cloud | gpt-4o | Yes | Yes | Yes |
| **Gemini** (Google) | Cloud | gemini-2.5-flash | Yes | Yes | Yes |
| **Ollama** (Local) | Local | llama3.1 (any model) | Yes | Yes | Yes |

All models are configurable. Failover chain lets you set backup providers:
```
Primary: Claude → Fallback 1: GPT-4o → Fallback 2: Ollama (local)
```

If Claude is down, your assistant keeps working. Failover retries with exponential backoff on transient errors (429, 503) before switching providers.

### Custom API Base URLs

Point any cloud provider at a compatible server (vLLM, LM Studio, Azure OpenAI, etc.):
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

## Terminal Commands

During a chat session, these slash commands are available:

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/new` | Start a fresh conversation |
| `/tools` | List available tools and their status |
| `/usage` | Show token usage for this session |
| `/clear` | Clear the screen |
| `/config` | Show current config summary (redacted) |
| `quit` | Exit MicroClaw |

---

## Installation

### From source

```bash
git clone https://gitlab.com/MANU-CHAUHAN/microclaw.git
cd microclaw
pip install .
```

### With extras (install only what you need)

```bash
pip install ".[telegram]"      # Telegram bot
pip install ".[discord]"       # Discord bot
pip install ".[slack]"         # Slack bot (Socket Mode)
pip install ".[whatsapp]"      # WhatsApp (aiohttp webhook)
pip install ".[browser]"       # Playwright browser automation
pip install ".[pdf]"           # PDF reading
pip install ".[embeddings]"    # Local sentence-transformer embeddings
pip install ".[all]"           # Everything
playwright install chromium    # Required if using browser tool
```

### Fully local (no API keys, no internet)

```bash
# Install Ollama: https://ollama.com
ollama pull llama3.1
pip install .
microclaw setup               # Select Ollama as provider
microclaw chat                 # Fully offline AI assistant
```

### Docker

```bash
docker compose run --rm microclaw chat              # Basic
docker compose --profile browser up                  # + Playwright
docker compose --profile full up                     # Everything
```

---

## Quick Start Guide

### Just the terminal (simplest)

```bash
pip install .
microclaw setup       # pick provider, paste API key
microclaw chat        # start chatting
```

### Terminal + Telegram + Discord (multi-channel)

```bash
pip install ".[telegram,discord]"
microclaw setup                   # pick provider, paste API key
microclaw connect telegram        # paste BotFather token — validated live
microclaw connect discord         # paste bot token — invite URL generated
microclaw test                    # verify everything works
microclaw start                   # launch all channels at once
```

### Fully local + all channels

```bash
ollama pull llama3.1
pip install ".[all]"
microclaw setup                   # select Ollama
microclaw connect telegram        # optional
microclaw connect discord         # optional
microclaw start                   # your own private AI, everywhere
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `microclaw setup` | Interactive wizard — provider, API key, persona, permissions, embeddings |
| `microclaw chat` | Terminal-only chat session (with slash commands) |
| `microclaw start` | Start gateway with **all enabled channels simultaneously** |
| `microclaw connect [channel]` | Guided channel setup with **live token validation** |
| `microclaw test` | Validate provider + all channel tokens (without starting) |
| `microclaw config` | Show current configuration (secrets redacted) |
| `microclaw status` | System status overview |
| `microclaw doctor` | Diagnostics — Python version, dependencies, disk, keys |
| `microclaw reset` | Delete all configuration and data |

---

## Configuration

Config lives at `~/.microclaw/config.json` with `0600` permissions.

### Environment Variables

Environment variables override config file values:

| Variable | Maps to |
|----------|---------|
| `MICROCLAW_PROVIDER` | Primary AI provider (`claude`, `openai`, `gemini`, `ollama`) |
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `OLLAMA_HOST` | Ollama server URL |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack tokens |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_ID` | WhatsApp credentials |
| `WEBCHAT_AUTH_TOKEN` | WebChat authentication token |
| `BRAVE_API_KEY` | Brave Search API key (optional, best search quality) |
| `SEARXNG_URL` | Self-hosted SearXNG instance URL |
| `MICROCLAW_FILE_LOG` | Enable file logging (`1`/`true`/`yes`) |
| `MICROCLAW_LOG_FILE` | Custom log file path (enables file logging) |

See `.env.example` for the full list.

### Permissions

Control what the agent is allowed to do:

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

- `allow_shell` — enable shell command execution
- `allow_file_write` — enable file creation/modification
- `allow_web_browse` — enable Playwright browser automation
- `confirm_dangerous` — require user confirmation for dangerous shell commands (rm, sudo, git push, etc.)
- `disabled_tools` — list of tool names to completely disable (e.g. `["shell", "http_request"]`)

### Persona

Customize how your agent identifies itself:

```json
{
  "persona": {
    "name": "MicroClaw",
    "personality": "friendly, helpful, and clever",
    "system_prompt_extra": "Always respond in Spanish."
  }
}
```

### Rate Limiting

Per-user rate limiting with burst detection:

```json
{
  "rate_limit": {
    "messages_per_minute": 20,
    "burst_limit": 5
  }
}
```

---

## Architecture

```
src/microclaw/          46 files, ~5,800 LOC
├── cli.py              # Click CLI (9 commands)
├── config.py           # Single Pydantic config model
├── connector.py        # Guided channel setup + live token validation
├── gateway.py          # Central coordinator + rate limiter + channel failure reporting
├── bus.py              # Async message bus
├── security.py         # SSRF + path security + DNS resolution checks
├── setup_wizard.py     # Interactive setup with Ollama validation + model picker
├── core/
│   ├── agent.py        # Agent loop (streaming + tool calls + error classification)
│   ├── context.py      # Context compaction + auto-summarization (safe — never deletes)
│   └── conversation.py # Conversation history + session management
├── providers/          # 4 providers + failover (all raw httpx, retry w/ backoff)
├── channels/           # 6 channels (terminal, web, telegram, discord, slack, whatsapp)
├── tools/              # 10 tools (web, files, shell, memory, vision, pdf, sessions)
├── memory/             # SQLite + vector search + embeddings (local/OpenAI/Ollama)
└── utils/
    └── logger.py       # Rich console + rotating file logging
```

### Key Design Decisions

- **All providers use raw `httpx`** — no AI SDK packages installed
- **WebChat uses raw TCP + RFC 6455 framing** — no web framework dependency
- **Failover** fallbacks use non-streaming to prevent partial content leaks
- **Retry with exponential backoff** on transient errors (429, 503) with Retry-After header support
- **Channel dependencies are optional extras** — install only what you need
- **Graceful HTTP client cleanup** — all providers implement `close()` for proper connection pool teardown
- **SQLite busy_timeout** — 5-second timeout prevents "database is locked" errors under concurrent access
- **Message size validation** — configurable max message size prevents memory exhaustion
- **Actionable error messages** — provider errors are classified into user-friendly hints (invalid key, rate limited, timeout, etc.)
- **Context compaction is safe** — if summarization fails, all original messages are preserved (never deleted)
- **Channel failure isolation** — one channel crashing doesn't take down the rest

Full decision log: [`DECISIONS.md`](DECISIONS.md)

---

## Security

MicroClaw takes security seriously:

- **Shell command blocking** — 40+ regex patterns block `rm -rf /`, reverse shells, env var leaks, `eval`/`source`/`exec`, download-and-execute, data exfiltration, and more
- **Dangerous command confirmation** — `rm`, `sudo`, `git push`, `pip install`, etc. require explicit user confirmation when `confirm_dangerous` is enabled
- **Path security** — blocks `/proc`, `/sys`, `/etc/shadow`, `/etc/passwd`, `.ssh/`, `.env`, path traversal (`../`)
- **SSRF protection** — blocks `localhost`, `127.0.0.1`, `169.254.x.x`, `10.x.x.x`, `192.168.x.x`, `.internal`/`.local` domains, and DNS resolution failures
- **Timing-safe auth** — `secrets.compare_digest()` for all token comparisons
- **WebSocket limits** — 1MB max frame size, 50 max headers to prevent DoS
- **Rate limiting** — per-user sliding window + burst detection at the gateway level
- **Config permissions** — `~/.microclaw/config.json` is `0600` (owner-only read/write)
- **No telemetry** — nothing phones home, ever

---

## Shell Completion

```bash
eval "$(_MICROCLAW_COMPLETE=bash_source microclaw)"    # Bash
eval "$(_MICROCLAW_COMPLETE=zsh_source microclaw)"     # Zsh
eval "$(_MICROCLAW_COMPLETE=fish_source microclaw)"    # Fish
```

---

## Development

```bash
pip install ".[dev]"
pytest                 # 116 tests
ruff check src/        # Linting
mypy src/              # Type checking
```

---

## License

MIT
