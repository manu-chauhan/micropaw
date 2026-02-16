# MicroClaw

**Your AI. Your machine. Your keys.**

A personal AI assistant that runs on your machine, talks to any LLM, works across 6 messaging platforms, and does it all in ~5,000 lines of Python. No SDK bloat, no cloud lock-in, no telemetry.

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
| **Lines of code** | ~5,000 | ~500 | 200,000+ |
| **AI providers** | 4 (Claude, GPT, Gemini, Ollama) | 1 | 2-3 |
| **Channels** | 6 (Terminal, Telegram, Discord, Slack, WhatsApp, WebChat) | 1 | 3-5 |
| **Tools** | 11 (web, files, shell, memory, vision, PDF) | 0-2 | 10+ |
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

**Web & Research**
- Search the web via DuckDuckGo
- Browse any URL and extract content (Playwright)
- Make HTTP requests with SSRF protection

**Files & Code**
- Read any file on your system (with path security)
- Write and create files
- Execute shell commands (sandboxed, with blocked dangerous patterns)

**Memory & Context**
- Long-term memory with SQLite + vector search
- Local embeddings via sentence-transformers (no API needed)
- Auto-summarizes old conversation when context grows large

**Media**
- Analyze images via your AI provider's vision capabilities
- Read and extract text from PDFs

**Sessions**
- Persistent conversations across restarts
- Per-user, per-platform conversation isolation

### Example: What a Conversation Looks Like

```
You > Search for the latest Python 3.13 features and summarize them

MicroClaw > [uses web_search tool]
            [uses web_browse tool to read the changelog]

            Python 3.13 brings several notable features:

            - **Free-threaded mode** — experimental support for running
              without the GIL (PEP 703)
            - **JIT compiler** — experimental copy-and-patch JIT (PEP 744)
            - **Improved error messages** — better tracebacks with color
            - **typing improvements** — new type parameter defaults
            ...

You > Save that to a file called python313.md

MicroClaw > [uses file_write tool]
            Done! Saved to python313.md (847 bytes)

You > Remember that I'm interested in Python release notes

MicroClaw > [uses memory tool]
            Got it — I'll remember that you're interested in Python release notes.
```

---

## Supported Providers

| Provider | Type | Model | Streaming | Tool Use |
|----------|------|-------|-----------|----------|
| **Claude** (Anthropic) | Cloud | claude-sonnet-4-20250514 | Yes | Yes |
| **GPT-4o** (OpenAI) | Cloud | gpt-4o | Yes | Yes |
| **Gemini** (Google) | Cloud | gemini-2.5-flash | Yes | Yes |
| **Ollama** (Local) | Local | llama3.1 (any model) | Yes | Yes |

All models are configurable. Failover chain lets you set backup providers:
```
Primary: Claude → Fallback 1: GPT-4o → Fallback 2: Ollama (local)
```

If Claude is down, your assistant keeps working.

---

## Channels

Talk to your AI from anywhere:

| Channel | Setup Difficulty | Key Feature |
|---------|-----------------|-------------|
| **Terminal** | Zero config | Color-coded, markdown rendering, streaming |
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
| `microclaw setup` | Interactive wizard — provider, API key, persona, permissions |
| `microclaw chat` | Terminal-only chat session |
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

Environment variables override config:

| Variable | Maps to |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` | Google Gemini API key |
| `OLLAMA_HOST` | Ollama server URL |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `DISCORD_BOT_TOKEN` | Discord bot token |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack tokens |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_ID` | WhatsApp credentials |

See `.env.example` for the full list.

---

## Architecture

```
User ──→ Channel ──→ Gateway ──→ Agent ──→ Provider (LLM)
                                   │              │
                                   ├── Tools ←────┘ (tool calls)
                                   └── Memory (SQLite + vector)
```

```
src/microclaw/
├── cli.py              # Click CLI (9 commands)
├── config.py           # Single Pydantic config model
├── connector.py        # Guided channel setup + test
├── gateway.py          # Central coordinator
├── bus.py              # Async message bus
├── security.py         # SSRF + path security
├── core/
│   ├── agent.py        # Agent loop (streaming + tool calls)
│   ├── context.py      # Context compaction
│   └── conversation.py # Conversation history
├── providers/          # 4 providers + failover (all raw httpx)
├── channels/           # 6 channels (terminal, web, telegram, discord, slack, whatsapp)
├── tools/              # 11 tools (web, files, shell, memory, vision, pdf, sessions)
└── memory/             # SQLite + vector search + embeddings
```

**Key design decisions:**
- All providers use raw `httpx` — no AI SDKs
- WebChat uses raw TCP + RFC 6455 framing — no web framework
- Failover fallbacks use non-streaming to prevent partial content leaks
- Channel dependencies are optional extras — install only what you need
- Security: shell command blocking, path traversal prevention, SSRF protection, timing-safe auth

---

## Security

MicroClaw takes security seriously:

- **Shell blocking** — regex patterns block `rm -rf /`, reverse shells, env var leaks, `eval`/`source`/`exec`, download-and-execute, and more
- **Path security** — blocks `/proc`, `/sys`, `/etc/shadow`, `.ssh/`, `.env`, symlink following
- **SSRF protection** — blocks `localhost`, `169.254.x.x`, private IPs, `.internal`/`.local` domains
- **Timing-safe auth** — `secrets.compare_digest()` for all token comparisons
- **WebSocket limits** — 1MB max frame size, 50 max headers to prevent DoS
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
pytest                 # 89 tests
ruff check src/        # Linting
mypy src/              # Type checking
```

---

## License

MIT
