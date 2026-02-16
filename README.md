# MicroClaw

**Your AI. Your machine. Your keys.**

Feature-rich personal AI assistant in ~4,500 lines of Python. No SDK bloat — all 4 AI providers talk raw HTTP. Runs anywhere Python runs.

## Features

- **4 AI Providers** — Claude, OpenAI, Gemini, Ollama — all via raw `httpx` (zero SDK dependencies)
- **6 Channels** — Terminal, WebChat (raw WebSocket), Telegram, Discord, Slack, WhatsApp
- **11 Tools** — File read/write, shell, web search, browser automation (Playwright), HTTP requests, memory, vision, PDF, sessions
- **Failover Chain** — Primary provider fails? Automatically tries the next one
- **Long-term Memory** — SQLite + vector search with optional embeddings
- **Context Compaction** — Auto-summarizes old conversation when context grows large
- **SSRF Protection** — URL validation, DNS rebinding checks, path traversal prevention
- **BYOK** — Bring your own API keys. Your keys stay on your machine
- **Local-first** — Ollama support means fully offline operation with local models

## Quickstart

```bash
# Install
pip install .

# Interactive setup
microclaw setup

# Start chatting
microclaw chat
```

## Installation

### From source

```bash
git clone https://gitlab.com/MANU-CHAUHAN/microclaw.git
cd microclaw
pip install .
```

### With extras

```bash
# Telegram bot
pip install ".[telegram]"

# Discord bot
pip install ".[discord]"

# Slack bot
pip install ".[slack]"

# Browser automation (Playwright)
pip install ".[browser]"
playwright install chromium

# PDF reading
pip install ".[pdf]"

# Everything
pip install ".[all]"
playwright install chromium
```

### Docker

```bash
# Basic (terminal + webchat)
docker compose run --rm microclaw chat

# With browser automation
docker compose --profile browser up

# All extras
docker compose --profile full up
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `microclaw setup` | Interactive setup wizard |
| `microclaw chat` | Terminal-only chat session |
| `microclaw start` | Start gateway with all enabled channels |
| `microclaw config` | Show current configuration (redacted) |
| `microclaw status` | Show system status |
| `microclaw doctor` | Run diagnostics |
| `microclaw reset` | Delete all config and data |

## Configuration

Config lives at `~/.microclaw/config.json` (created by `microclaw setup`).

Environment variables override config values:

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `OLLAMA_HOST` | Ollama server URL (default: `http://localhost:11434`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `SLACK_BOT_TOKEN` | Slack bot token |
| `SLACK_APP_TOKEN` | Slack app-level token (Socket Mode) |
| `WHATSAPP_TOKEN` | WhatsApp Cloud API token |

See `.env.example` for the full list.

## Architecture

```
microclaw/
├── cli.py              # Click CLI entry point
├── config.py           # Single Pydantic config model
├── gateway.py          # Central coordinator
├── bus.py              # Async message bus
├── security.py         # SSRF + path security
├── core/
│   ├── agent.py        # Agent loop (streaming + tool calls)
│   ├── context.py      # Context compaction
│   └── conversation.py # Conversation history
├── providers/
│   ├── claude.py       # Anthropic via httpx
│   ├── openai.py       # OpenAI via httpx
│   ├── gemini.py       # Gemini via httpx
│   ├── ollama.py       # Ollama via httpx
│   └── failover.py     # Failover chain
├── channels/
│   ├── terminal.py     # CLI REPL
│   ├── webchat.py      # Raw WebSocket (RFC 6455)
│   ├── telegram.py     # Telegram bot
│   ├── discord.py      # Discord bot
│   ├── slack.py        # Slack bot (Socket Mode)
│   └── whatsapp.py     # WhatsApp Cloud API
├── tools/              # 11 tools (file, shell, web, memory, vision, pdf, sessions)
└── memory/
    ├── store.py        # SQLite + vector memory
    └── embeddings.py   # OpenAI/Ollama embeddings
```

**Data flow:** User → Channel → Gateway → Agent → Provider (LLM) → Tool calls → Response → Channel → User

**Key design decisions:**
- All providers use raw `httpx` POST + SSE/NDJSON streaming — no `anthropic`, `openai`, or `google-genai` SDKs
- WebChat uses raw TCP + RFC 6455 framing — no `aiohttp` or `websockets` dependency
- Single Pydantic config replaces hundreds of scattered config files
- Failover fallbacks use non-streaming to prevent partial content leaks
- Channel dependencies are optional extras — install only what you need

## Shell Completion

```bash
# Bash
eval "$(_MICROCLAW_COMPLETE=bash_source microclaw)"

# Zsh
eval "$(_MICROCLAW_COMPLETE=zsh_source microclaw)"

# Fish
eval "$(_MICROCLAW_COMPLETE=fish_source microclaw)"
```

## Development

```bash
pip install ".[dev]"
pytest
ruff check src/
mypy src/
```

## License

MIT
