<div align="center">

# MicroClaw

**Your AI. Your machine. Your keys.**

A personal AI assistant that runs everywhere — Terminal, Telegram, Discord, Slack, WhatsApp, WebChat — with shared memory across all platforms. Built in ~6,400 lines of Python with zero SDK dependencies.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-238%20passed-brightgreen)]()
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

</div>

---

```bash
pip install .
microclaw setup     # pick provider, paste key
microclaw chat      # start talking
```

---

## Highlights

- **Same brain, everywhere** — Talk on Telegram, continue on Discord, ask from WhatsApp. One memory, one personality, six platforms running simultaneously. Your assistant remembers everything across all channels.

- **Zero SDK dependencies** — Every AI provider (Claude, GPT-4o, Gemini, Ollama) talks raw HTTP via `httpx`. No `anthropic` package. No `openai` package. No `google-genai`. Switching providers is a config change, not a rewrite.

- **Vector search, auto-configured** — Embeddings activate automatically: probes Ollama at startup, falls back to OpenAI, then sentence-transformers, then text search. Default model: `nomic-embed-text` (768d, 8k context). Zero config required.

- **Multi-agent delegation** — The main agent spawns sub-agents for parallel tasks. Sub-agents inherit all tools except `delegate` (no recursive spawning). Max 25 LLM rounds per sub-agent, configurable.

- **Automatic failover** — Provider chain with exponential backoff. If Claude is down, your assistant switches to GPT-4o, then Ollama. Keeps working no matter what.

- **Self-healing channels** — If a channel crashes, MicroClaw detects it and restarts automatically. Other channels keep running.

- **11 built-in tools** — Web search (3-backend fallback), browser automation, file I/O, shell execution, memory, vision, PDF reading, HTTP requests, session export, and delegation — all with security guardrails.

- **Fully local option** — Run entirely offline with Ollama. No data leaves your machine. No telemetry, ever.

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

</div>

```
         Telegram  Discord  Slack  WhatsApp  WebChat
              \       |       |       |        /
               +------+-------+-------+------+
                              |
    Terminal  ───────  [  Gateway  ]
                              |
                     +--------+--------+
                     |        |        |
                  [Agent]  [Tools]  [Memory ── SQLite + vectors]
                     |
                [Provider ── Claude / GPT-4o / Gemini / Ollama]
```

```bash
microclaw connect telegram    # guided setup, validates token live
microclaw connect discord     # auto-generates bot invite URL
microclaw start               # launch ALL channels at once
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
microclaw start
```

**Fully local (no internet):**
```bash
ollama pull llama3.1
pip install .
microclaw setup    # select Ollama
microclaw chat
```

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

## Memory & Vector Search

**SQLite + auto-detected vector embeddings.** Shared across all channels. Persists across restarts.

Auto-detect priority (zero config):
```
Ollama (local, free) -> OpenAI (if key exists) -> sentence-transformers (offline) -> text search
```

Default embedding model: `nomic-embed-text` (768 dims, 8192 token context, MTEB 59.4).

Override:
```json
{ "embedding": { "provider": "ollama", "model": "snowflake-arctic-embed" } }
```

---

## Multi-Agent Delegation

```
You > Research quantum computing AND summarize today's AI news

MicroClaw > [delegate] "Research quantum computing breakthroughs"
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
| `microclaw setup` | Interactive wizard — provider, API key, persona, permissions |
| `microclaw chat` | Terminal chat session |
| `microclaw start` | Launch all enabled channels |
| `microclaw connect <channel>` | Guided channel setup with live token validation |
| `microclaw test` | Validate provider + all channel tokens |
| `microclaw config` | Show configuration (secrets redacted) |
| `microclaw status` | System status overview |
| `microclaw doctor` | Diagnostics — Python, deps, disk, keys |
| `microclaw export` | Export conversations (JSON/Markdown) |
| `microclaw reset` | Delete all config and data |

Terminal slash commands: `/help`, `/new`, `/tools`, `/usage`, `/clear`, `/config`, `quit`

---

<details>
<summary><strong>Configuration</strong></summary>

Config file: `~/.microclaw/config.json` (0600 permissions).

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `MICROCLAW_PROVIDER` | Primary provider (`claude`/`openai`/`gemini`/`ollama`) |
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

</details>

<details>
<summary><strong>Installation Options</strong></summary>

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
pip install ".[embeddings]"    # Local sentence-transformers (if no Ollama/OpenAI)
pip install ".[all]"           # Everything
```

Docker:
```bash
docker compose run --rm microclaw chat              # Basic
docker compose --profile browser up                  # + Playwright
docker compose --profile full up                     # Everything
```

</details>

<details>
<summary><strong>Architecture</strong></summary>

```
src/microclaw/          6,392 LOC source + 2,632 LOC tests
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

</details>

---

## Development

```bash
pip install ".[dev]"
pytest                 # 238 tests, <1s
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
