# MicroClaw

**Your AI. Your machine. Your keys.**

A personal AI assistant that runs on your machine, talks to any LLM, works across 6 messaging platforms, and does it all in ~5,500 lines of Python. No SDK bloat, no cloud lock-in, no telemetry.

```bash
pip install .
microclaw setup     # pick your AI provider, paste your key
microclaw chat      # start talking
```

That's it. You're chatting with an AI that can search the web, browse pages, read/write files, execute shell commands, analyze images, read PDFs, and remember things across conversations — all from your terminal.

---

## Why MicroClaw?

Most AI assistants are either **too bloated** (200K+ lines, dozens of SDKs, framework hell) or **too simple** (a wrapper around one API with no tools). MicroClaw is neither.

| | MicroClaw | Typical AI Wrapper | Enterprise AI Platform |
|---|---|---|---|
| **Lines of code** | ~5,500 | ~500 | 200,000+ |
| **AI providers** | 4 (Claude, GPT, Gemini, Ollama) | 1 | 2-3 |
| **Channels** | 6 (Terminal, Telegram, Discord, Slack, WhatsApp, WebChat) | 1 | 3-5 |
| **Tools** | 10 (web, files, shell, memory, vision, PDF) | 0-2 | 10+ |
| **SDK dependencies** | 0 (raw HTTP to every provider) | 1-3 | 10+ |
| **Local/offline mode** | Yes (Ollama) | No | No |
| **Your keys stay local** | Yes | Maybe | No |
| **Failover between providers** | Yes | No | Sometimes |
| **Setup time** | 2 minutes | 2 minutes | 2 weeks |

### Zero SDK Philosophy

Every AI provider talks raw HTTP via `httpx`. No `anthropic` package. No `openai` package. No `google-genai` package. This means:
- **Fewer dependencies** — less to install, less to break
- **Faster startup** — no heavy SDK initialization
- **Full control** — you see exactly what goes over the wire
- **Provider-agnostic** — switching providers is a config change, not a rewrite

---

## What Can It Do?

MicroClaw is a tool-using AI agent. It doesn't just chat — it acts.

### Tools

| Tool | Description |
|------|-------------|
| `web_search` | Search the web via DuckDuckGo |
| `web_browse` | Browse any URL and extract content (Playwright) |
| `http_request` | Make HTTP requests with SSRF protection |
| `file_read` | Read any file on your system (with path security) |
| `file_write` | Write and create files |
| `shell` | Execute shell commands (sandboxed, dangerous patterns blocked) |
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

- **Long-term memory** with SQLite + vector search
- **Embeddings**: local sentence-transformers (no API needed), OpenAI, or Ollama
- **Auto-summarizes** old conversation when context grows large
- **Persistent conversations** across restarts, per-user and per-platform

### Media

- **Image analysis** via provider vision APIs (Claude, OpenAI, Gemini, Ollama)
- **PDF extraction** — read and summarize PDF documents

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

## Channels

Talk to your AI from anywhere:

| Channel | Setup Difficulty | Key Feature |
|---------|-----------------|-------------|
| **Terminal** | Zero config | Slash commands, streaming, markdown, tool visibility |
| **Telegram** | Easy (3 steps) | BotFather token, live validation |
| **Discord** | Medium (5 steps) | Auto-generated invite URL |
| **Slack** | Medium (6 steps) | Socket Mode — no public URL needed |
| **WhatsApp** | Hard (8+ steps) | Meta Business API, needs public webhook |
| **WebChat** | Easy (1 step) | Raw WebSocket, any web frontend can connect |

Connect a channel in seconds:
```bash
microclaw connect telegram    # step-by-step guide, validates your token live
microclaw connect discord     # generates bot invite URL for you
microclaw test                # verify all channels work before starting
microclaw start               # launch everything
```

### Terminal Commands

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

## CLI Reference

| Command | Description |
|---------|-------------|
| `microclaw setup` | Interactive wizard — provider, API key, persona, permissions, embeddings |
| `microclaw chat` | Terminal-only chat session (with slash commands) |
| `microclaw start` | Start gateway with all enabled channels |
| `microclaw connect [channel]` | Guided channel setup with live token validation |
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
User ──→ Channel ──→ Gateway ──→ Agent ──→ Provider (LLM)
                        │          │              │
                     Rate Limit    ├── Tools ←────┘ (tool calls)
                                   └── Memory (SQLite + vector)
```

```
src/microclaw/          46 files, ~5,500 LOC
├── cli.py              # Click CLI (9 commands)
├── config.py           # Single Pydantic config model
├── connector.py        # Guided channel setup + test
├── gateway.py          # Central coordinator + rate limiter
├── bus.py              # Async message bus
├── security.py         # SSRF + path security + DNS resolution checks
├── setup_wizard.py     # Interactive setup with Ollama validation + model picker
├── core/
│   ├── agent.py        # Agent loop (streaming + tool calls + error classification)
│   ├── context.py      # Context compaction + auto-summarization
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

Full decision log: [`DECISIONS.md`](DECISIONS.md)

---

## Security

MicroClaw takes security seriously:

- **Shell command blocking** — 40+ regex patterns block `rm -rf /`, reverse shells, env var leaks, `eval`/`source`/`exec`, download-and-execute, data exfiltration, and more
- **Dangerous command confirmation** — `rm`, `sudo`, `git push`, `pip install`, etc. require explicit user confirmation when `confirm_dangerous` is enabled
- **Path security** — blocks `/proc`, `/sys`, `/etc/shadow`, `.ssh/`, `.env`, path traversal (`../`)
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
pytest                 # 101 tests
ruff check src/        # Linting
mypy src/              # Type checking
```

---

## License

MIT
