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

- **16 built-in tools**: web search (offline Kiwix and your own documents, your own SearXNG, Brave, DuckDuckGo or public SearXNG, by `search.provider` and the privacy mode), browser automation, file I/O, shell, HTTP requests, memory, contacts, user files, vision, PDF, sessions, delegation, cron scheduling, email (Gmail) and calendar (Google Calendar), all security-hardened.

- **Raw WebSocket** — WebChat channel implements RFC 6455 from scratch. No aiohttp. ~100 LOC.

- **Tool result caching** — Read-only tool calls cached per-message (LRU-32). Same query twice? Instant response, zero wasted tokens.

- **Privacy modes**: `privacy.mode` is `normal`, `local` or `airgapped`. In air-gapped mode MicroPaw connects only to this computer (and your own network if you allow it), and a test in CI checks that no connection leaves. MicroPaw itself has no telemetry and checks for updates only when you run `paw update`. See [Privacy](#privacy).

- **Docker ready**: uv-built slim image, non-root, health check. `docker compose up` with an optional Ollama profile. Named volume for persistence.

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
| **Telemetry** | None | Local JSONL (opt-out) | None | None | None |
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

> **MicroPaw has no telemetry and does not phone home. Your messages go only where you point it: the model, search and chat services you set up, and every reply says which one answered. Set `privacy.mode` to `local` or `airgapped` to keep everything on your computer or your network.**

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
paw setup    # choose local or air-gapped when it asks how private to be
paw chat
```

> `micropaw` also works as a command name — both are identical.

---

## Tools

| Tool | What it does |
|------|-------------|
| `web_search` | Offline (Kiwix, your documents), your own SearXNG, Brave, DuckDuckGo or public SearXNG instances. Public ones are used only with `search.provider` `auto` and nothing else set up, and never in `local` or `airgapped` mode |
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

Model settings are `provider:model`. Besides `claude`, `openai`, `gemini` and `ollama`, these names work without extra setup: `lmstudio`, `llamacpp`, `vllm` (local; no key by default, though several of these servers accept one) and `openrouter`, `groq`, `deepseek`, `mistral`, `together` (key from `<NAME>_API_KEY`). A text-only local model can still use photos: set `models.vision` and the vision model describes them first.

```json
{
  "provider": "ollama",
  "models": { "fast": "ollama:qwen3.6", "vision": "ollama:gemma4:e4b" },
  "voice": { "stt": "local:whisper-small", "stt_url": "http://localhost:8000" }
}
```

---

## Models on another computer

MicroPaw can use a model server on another machine on your network: a desktop with a GPU, a Mac mini, a Jetson, a NAS, or another Raspberry Pi. You can mix it with cloud APIs and a local Ollama. For example, the Pi runs a small model for quick jobs, the desktop runs the main model, and a cloud model is the fallback when the desktop is asleep.

### Security first

- Every server below listens only on its own computer (127.0.0.1) by default. Opening it to the network means anyone on that Wi-Fi or Ethernet can use it.
- Ollama's local API has no login or key. If you open it to the network, your firewall is the only lock. Allow the port only from the computers that need it, and never forward it on your router to the internet.
- Prefer a server that supports a key (llama.cpp, vLLM, LM Studio, Jan, LocalAI, KoboldCpp) and turn the key on. `paw models add` warns you when a server on another machine answers without a key.
- Tailscale is the simplest safe option: traffic between your devices is encrypted, and it works away from home too.
- MicroPaw's own tools (`http_request`, `web_browse`) still cannot reach machines on your network. Only the model, embedding and speech connections you configure can. This is on purpose.
- Keep WebChat on 127.0.0.1. It has no TLS and its token is in the URL.

### Step 1: open the server to the network (on the server machine)

**Ollama on Linux, a Jetson, or another Pi.** Run `sudo systemctl edit ollama.service`, add these two lines, save, then run `sudo systemctl daemon-reload` and `sudo systemctl restart ollama`:

```
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

On a Jetson, install Ollama with `curl -fsSL https://ollama.com/install.sh | sh` first, then do the same.

**Ollama on macOS (the app).** Run `launchctl setenv OLLAMA_HOST "0.0.0.0:11434"`, then quit and reopen Ollama.

**Ollama on Windows.** Quit Ollama from the taskbar. Open Settings (Windows 11) or Control Panel (Windows 10), search for "environment variables", and edit the variables for your account. Add `OLLAMA_HOST` with the value `0.0.0.0:11434`. Click OK, then start Ollama from the Start menu.

**llama.cpp (llama-server).** Start it with a key: `llama-server -m model.gguf -c 4096 --host 0.0.0.0 --port 8080 --api-key <long-random-key>` (on Windows, `llama-server.exe`). Current builds default to port 9931 and older builds to 8080, so pass `--port` to be sure. Set `-c` on small machines. Do not turn on `--tools`, `--agent` or `--mcp-servers` on a network server: they expose file and shell access over the API.

**vLLM.** `vllm serve <model> --host 0.0.0.0 --port 8000 --api-key <long-random-key>`. The key protects the /v1, /v2 and /inference paths only.

**LM Studio.** In the server settings, turn on the option that lets other devices on your network use the server. Turn on authentication in Developers > Server Settings, then Manage Tokens > Create Token. The token is shown once. Use the port the app shows.

**Jan.** Settings > Local API Server: set Host to 0.0.0.0, set an API key (an empty key means no login), then Start Server. Jan's default address is 127.0.0.1:1337.

**KoboldCpp.** Default port 5001. Use `--host` and `--password` (see `koboldcpp --help`). Do not use `--remotetunnel`: it makes the server public.

**Windows firewall** (on the server, PowerShell as administrator; change the port for other servers):

```
New-NetFirewallRule -DisplayName "MicroPaw model server 11434" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow -Profile Private
```

Make sure the network is set to Private in Windows settings. On Linux and macOS, allow the port only from your client's address in your firewall.

### Step 2: add it to MicroPaw (on the machine that runs MicroPaw)

```bash
paw models add desktop 192.168.1.50            # finds the server port, lists the models, saves it
paw models add desktop 192.168.1.50 --use main # also make it the main model
paw models add gpubox 192.168.1.60:8000 --key-env GPUBOX_KEY   # a server with a key
paw models test desktop:qwen3.6:27b            # one chat and one tool call
paw doctor                                     # checks every model server you use
```

Put keys in `~/.micropaw/.env` (for example `GPUBOX_KEY=...`), never on the command line. A bare address means port 11434 for Ollama. A name such as `gpu-box.local` works when this computer can look it up; if it cannot, use the address. `paw models add` only lists the server's models, it never runs one, and it saves nothing unless the server answers. `paw models remove desktop` forgets a server that nothing uses any more.

What `paw models add` saves (you can also write it by hand in `~/.micropaw/config.json`):

```json
{
  "providers": {
    "desktop": { "base_url": "http://192.168.1.50:11434", "kind": "ollama" },
    "gpubox": { "base_url": "http://192.168.1.60:8000/v1", "api_key_env": "GPUBOX_KEY" }
  },
  "agents": { "main": { "model": "desktop:qwen3.6:27b" } },
  "models": { "fast": "ollama:granite4.1:3b" },
  "fallback_providers": ["claude:claude-haiku-5-5"]
}
```

`kind: "ollama"` uses Ollama's own API, so the context size and keep-alive are sent. For a remote Ollama, set `num_ctx` to what the server machine can hold; otherwise MicroPaw uses 8192 or the model's maximum if lower. If the desktop is asleep, MicroPaw says which machine did not answer and moves to the next model in the list.

**Finding servers.** `paw models scan` checks this computer and every server in your config. In `local` and `airgapped` mode it skips any configured server that is not on this computer or your network, sends it no key, and refuses a `--host` outside your network. A network scan covers at most 512 addresses and says so if the 25 second limit stops it early. `paw models scan --host 192.168.1.50` checks one machine. `paw models scan --lan` checks your own network (the /24 this computer is on) on the usual model ports only, after asking you first. Use `--range 192.168.1.0/24` for another range. Only scan networks you own or may scan. Nothing is saved; the scan prints `paw models add` lines for you to copy.

**Memory search and speech on another machine.** Point `voice.stt` and `voice.tts` at a server you added, for example `"stt": "desktop:whisper-large-v3"`. For memory search, set `embedding.provider` to `ollama` and `embedding.base_url` to the server.

### Tailscale

Install Tailscale on the server and on the MicroPaw machine, then use the server's Tailscale address or name instead of its LAN address. On Linux and Raspberry Pi OS: `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up` and open the link it prints. On Windows and macOS install the Tailscale app. Tailscale addresses are in 100.64.0.0/10 (and fd7a:115c:a1e0::/48 for IPv6). MicroPaw treats them as your own network, so plain http over Tailscale is allowed and private agents may use them. Turn off key expiry for a Raspberry Pi in the Tailscale admin page so it stays connected.

---

## Privacy

MicroPaw has one privacy setting, `privacy.mode`. You choose it in `paw setup` or in `~/.micropaw/config.json`. `paw privacy`, `paw doctor`, `paw status` and `/status` in chat show the current mode and where each part of MicroPaw sends data. MicroPaw itself has no telemetry. It checks for updates only when you run `paw update`.

Your messages go where you point MicroPaw: the model, search and chat services you set up. In every mode each reply ends with a line such as `[via claude (api.anthropic.com)]` or `[via ollama on this machine]`, so you can see which one answered. Turn it off with `display.show_provider`.

### The three modes

- `airgapped`: MicroPaw talks only to this computer, and to your own network if `privacy.allow_lan` is on. Models, embeddings, speech and search must run there. Cloud models, chat apps that relay through a company's servers (Telegram, Discord, Slack, WhatsApp), Google mail and calendar, updates and model downloads are refused with a message that says why. Web search answers from offline sources (Kiwix archives and your own documents) and says so.
- `local`: models, embeddings, speech-to-text and text-to-speech run on this computer or your network. Web search goes only through your own SearXNG (`search.searxng_url`), optionally through a SOCKS proxy such as Tor (`search.proxy`), and you see every query before it is sent, because a query can contain private text. Your Kiwix archives and documents are searched first when you have them. `web_browse` and `http_request` are off unless you name them in `privacy.allow` (`web_browse`, `web_fetch`). `web_browse` shows you each page address for approval and removes tracking parameters from it. `http_request` does not: a plain GET is not asked about and the address is not cleaned, so name `web_fetch` only if you accept that.
- `normal` (the default): you can use cloud models. Even here, a search asks first once the chat has read outside content, and each reply says which provider answered.

MICROPAW_PRIVACY in the environment can make the mode stricter than the config says, never looser. The mode is read once when MicroPaw starts.

### What is checked, and what is not

A local address is not proof that a server keeps data local. A proxy such as LiteLLM or Open WebUI on your own network can forward to a cloud API, and Ollama can serve cloud models through its local daemon (names ending in `-cloud`). MicroPaw cannot see that. It refuses models that Ollama marks as cloud in `local` and `airgapped` mode, and `paw doctor` reminds you to turn Ollama's cloud features off (`"disable_ollama_cloud": true` in `~/.ollama/server.json`, then restart Ollama).

Air-gapped mode isolates MicroPaw, not the computer. The operating system's own sync, backup, crash reports and updates need the machine itself to be offline.

Chat apps are different in kind: with Telegram, Discord, Slack or WhatsApp, every message passes through the company's servers in readable form. `paw connect` says so. MicroPaw turns link previews off on those channels, because the company's server would fetch any link in a reply.

### Offline search with Kiwix and your own documents

`web_search` can answer with no internet. `search.provider` takes `offline` (Kiwix and your documents), `kiwix`, `docs`, `searxng` (your own server), `auto`, `brave`, `duckduckgo` or `off`. In air-gapped mode `auto` means offline and the online providers are refused. With `auto` or `searxng` and a `search.kiwix_url`, Kiwix is searched first in every other mode too. Whatever the search finds, offline results included, is sent to the model that answers, so a cloud model sees it.

kiwix-serve serves ZIM archives and has a full-text search endpoint (`/search` with `pattern`, `books.name` and `format=xml`). Run it on this computer or on your network, then set `search.kiwix_url` and `search.kiwix_books`. kiwix-tools is GPLv3 or later and is installed separately. Sizes from the Kiwix download index:

- Wikipedia English, mini: 13 GB (2026-09)
- Wikipedia English, no pictures: 49 GB (2026-06)
- Wikipedia English, most pictures: 119 GB (2026-08)
- Wiktionary English, no pictures: 8.5 GB (2026-08)
- Super User 3.7 GB, Ask Ubuntu 2.6 GB, Unix and Linux 1.2 GB, Stack Overflow 107 GB
- DevDocs Python 4.6 MB (each DevDocs library is a few MB)

On a Raspberry Pi, keep the larger archives on an external SSD. Results name their source and snapshot date, for example `Wikipedia (Kiwix snapshot 2026-06)`, and carry an id: the model calls `web_search` again with action `open` and that id to read the whole article. Only articles on your own Kiwix server can be opened, never an address the model makes up.

To search your own files, run `paw docs add <folder>`. It indexes `.txt`, `.md` and `.pdf` files (PDF needs `paw extras add pdf`) for keyword search, skips files over 10 MB, and sends nothing anywhere.

### Your own SearXNG (local mode)

In SearXNG's own settings.yml, add `json` to `formats` under `search:` (the default is html only), change `server.secret_key`, and keep `server.limiter` off on a closed network (it needs Valkey). To hide your address from the search engines, set outgoing proxies with `socks5h://` or set `using_tor_proxy` to true. Leave `useragent_suffix` empty, because it is sent to every engine. SearXNG still sends your query text to the engines it uses: it hides who is searching, not what. That is why MicroPaw shows you each query first, and asks again for the next one. To reach your SearXNG through Tor from MicroPaw, set `search.proxy` and install the extra: `pip install "micropaw[tor]"`. `paw doctor --search` runs one test query, after asking.

### Cloud providers (normal mode), as of 2026-11-05

- Anthropic API: Anthropic may not train on customer content. Content is not retained by default, except for models Anthropic lists as Covered Models, which need 30-day retention.
- OpenAI API: not used to train models. Abuse monitoring logs are kept up to 30 days.
- Google Gemini API: on the unpaid tier, Google may use prompts and responses to improve its products and human reviewers may read them. The paid tier does not use prompts to improve products.
- DeepSeek: stores personal data in the People's Republic of China and lists model training as a purpose, with an opt-out.
- Groq: does not retain inference data by default. Logs are kept up to 30 days for abuse investigations.
- OpenRouter: the `data_collection` "deny" option limits routing to providers that do not collect user data.

These terms change. Check each provider's current terms.

### Getting models onto an offline machine

- Ollama listens on 127.0.0.1:11434 by default and downloads models from the internet, so pull them on a connected machine and copy them across. Models live in `~/.ollama/models` on macOS, `/usr/share/ollama/.ollama/models` with the Linux installer, and `C:\Users\<you>\.ollama\models` on Windows. `OLLAMA_MODELS` moves them. Ollama updates itself on macOS and Windows.
- Python packages: uv has an `--offline` flag, and `uv pip install` takes `--no-index` with `--find-links ./wheels` for a folder of wheels you copied in. We have not tested this whole flow on every platform.
- Hugging Face libraries read only cached files when `HF_HUB_OFFLINE=1` is set.

### Data on this computer

Everything lives in `~/.micropaw`. On Linux and macOS its files are readable only by you (0600, folders 0700). `paw privacy` shows the permissions. Use full-disk encryption: BitLocker or device encryption on Windows (Microsoft notes that a device with only local accounts stays unprotected even though the data is encrypted, because the recovery key is not backed up), FileVault on macOS, LUKS on Linux.

---

## Raspberry Pi

MicroPaw runs on a Raspberry Pi. Keep heavy models on another machine or in the cloud: see "Models on another computer" above.

1. **Board and OS.** Use 64-bit Raspberry Pi OS. The current release is Trixie (Debian 13); Legacy Bookworm (Debian 12) is still offered. The 64-bit image supports Pi 3B, 3A+, 4, 5, 500, 500+, CM5 and Zero 2 W. 32-bit Pi OS works for the core only: no sqlite-vec (vector search falls back to plain Python), no local embeddings (fastembed), no browser tool (Playwright) and no Ollama, because none of these ship 32-bit ARM builds. Pi Zero, Zero W and Pi 1 (armv6l) are not supported, because pydantic-core has no armv6l wheel.
2. **Python.** MicroPaw needs Python 3.11 or newer. Trixie ships 3.13 and Bookworm ships 3.11. The test suite passes on both.
3. **Install.** Install uv, then run `uv tool install --compile-bytecode "micropaw[telegram]"` (pick the channel extras you use: `telegram`, `discord`, `slack`, `pdf`, `whatsapp`). Keep `--compile-bytecode`: without it every start recompiles about 440 modules (0.67 s instead of 0.17 s on a desktop, more on a Pi). Do not use `micropaw[all]` on a Pi: it includes embeddings (182 MB installed on 64-bit ARM, plus a model download) and the browser (156 MB plus Chromium). The core alone is about 20 MB on 64-bit ARM.
4. **Local models by RAM (Ollama).** Ollama installs with `curl -fsSL https://ollama.com/install.sh | sh` and is 64-bit only. Download sizes from the Ollama library. 4 GB: `qwen3.5:0.8b` (1.2 GB) or `qwen3.5:2b` (2.7 to 3.1 GB). 8 GB: `granite4.1:3b` (2.1 GB), `ministral-3:3b` (3.0 GB) or `qwen3.5:4b` (3.3 to 4.0 GB). 16 GB: `granite4.1:8b` (5.3 GB), `ministral-3:8b` (6.0 GB) or `qwen3.5:9b` (6.6 to 7.6 GB). All of these list tool support. With 2 GB or less (including the Zero 2 W), use a cloud provider, or run Ollama on another machine and add it with `paw models add`. For memory search on a Pi, use Ollama (`nomic-embed-text`) or turn embeddings off. We have not measured tool-calling speed on any Pi, so there are no tokens-per-second numbers here.
5. **SD card or SSD.** On a desktop (x86, not a Pi) MicroPaw wrote about 54 KB per chat turn with two model calls and a tool call, not counting the log file and the audit log (which adds about 340 bytes per tool call), and up to about 27 KB an hour when idle. At 200 turns a day that is about 11 MB a day, so an SD card is fine, and a USB SSD is faster for starts and database commits. Pi 4 and newer boot from USB when `BOOT_ORDER` includes USB (the default `0xf41` tries SD first, then USB); the Pi 3B+ and Zero 2 W boot from USB mass storage out of the box; a Pi 5 on a 3 A supply needs `usb_max_current_enable=1` in `config.txt` to power a USB drive.
6. **Memory and speed.** The idle gateway with WebChat on used 52 to 56 MB of RAM on a desktop (x86, three runs). `paw --version` took 0.18 to 0.24 s there with the bytecode already compiled. Every time number here comes from a desktop, not from a Pi, and a Pi is several times slower, so measure on your board before you plan around one.
7. **Service.** Run `paw service install`. It creates a systemd user service and turns on lingering so the service starts at boot and keeps running after logout. If that fails it prints the command to run with sudo (`sudo loginctl enable-linger $USER`). Stop it with `paw service stop`.
8. **Shell sandbox.** Install bubblewrap with `sudo apt install bubblewrap` and check it with `bwrap --unshare-all true`. It needs unprivileged user namespaces. If the check fails, MicroPaw falls back to no sandbox and shell commands ask you each time. `paw doctor` shows which sandbox is active.
9. **Network.** Give the Pi a fixed address with a DHCP reservation on your router (the Raspberry Pi docs advise this over a static address set on the Pi). `<hostname>.local` works from many computers; if it does not, use the address. Prefer Ethernet. On Wi-Fi, turn power saving off for the saved connection so the Pi does not drop off the network: `nmcli connection modify <connection-name> 802-11-wireless.powersave 2` (2 means disable), then reconnect. Most Pi boards have no battery-backed clock (the Pi 5 can use a battery on its J5 connector), so check that `timedatectl` shows the clock is synchronized before relying on scheduled jobs after a power cut.
10. **WebChat from a phone.** WebChat listens on 127.0.0.1:8765 and has no TLS. Keep it there and connect over Tailscale, or from a laptop run `ssh -N -L 8765:127.0.0.1:8765 pi@<hostname>.local` and open `http://127.0.0.1:8765`. Do not bind it to 0.0.0.0 on a shared network.

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
- **SSRF protection**: blocks every non-public address (including 100.64.0.0/10), checks the address actually connected to, so DNS rebinding fails, and re-checks the browser after redirects
- **Auth** — timing-safe `secrets.compare_digest()` for all token comparisons
- **WebSocket limits** — 1MB max frame, 50 max headers, outbound truncation
- **Rate limiting** — per-user sliding window + burst detection
- **Config protection** — 0600 permissions, world-readable warning on load
- **Cross-origin safety**: a redirect to another site never gets your headers, and a POST is never re-sent to another site
- **No telemetry**: MicroPaw sends none. [Privacy](#privacy) says where your messages go

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
| `paw doctor` | Diagnostics: Python, deps, disk, keys, every model server you use, privacy checks (`--search` runs one test search) |
| `paw privacy` | Where your data goes: the mode, each model, search, channel and server, file permissions |
| `paw models add <name> <address>` | Test a model server on another computer and save it (`--key-env`, `--use main\|fast\|vision\|fallback`) |
| `paw models remove <name>` | Forget a server added with `paw models add` |
| `paw models scan` | Find model servers: this computer, your config, `--host`, and your network with `--lan` or `--range` |
| `paw docs add <folder>` | Index your own `.txt`, `.md` and `.pdf` files for offline search |
| `paw cron add "task" --every 6h` | Schedule a recurring task |
| `paw cron add "task" --once 2h` | Schedule a one-shot reminder |
| `paw cron list` | List all scheduled tasks |
| `paw cron remove <id>` | Remove a scheduled task |
| `paw log` | Recent tool calls and approvals (audit log) |
| `paw cron enable <id>` | Turn on a task the assistant scheduled (`--off` to pause) |
| `paw allow <channel> <id>` | Let one of your accounts use the assistant (`--remove` to revoke) |
| `paw export` | Export conversations (JSON/Markdown) |
| `paw reset` | Delete all config and data |

Chat commands also include `/privacy` and `/status`. Terminal commands: `/help`, `/new` (fresh conversation, this channel), `/rejoin` (back to shared), `/new all` (fresh everywhere), `/tools`, `/usage`, `/clear`, `/config`, `/memory` (what it remembers), `/forget <key>`, `/cron enable <id>`, `!cmd` (shell escape), `quit`

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
Compose reads keys from a `.env` file at run time. They are not copied into the image. Ollama's port is not published. WebChat is published on `127.0.0.1` only. The container is the boundary for shell commands, so turn MicroPaw's own sandbox off inside it.

**Hosting.** MicroPaw needs a disk that survives restarts, because the database is SQLite in WAL mode, which does not work on a network filesystem. Check prices on each provider's page before you choose.
- Fly.io: no free tier for new organizations. Needs a volume mounted at `/home/paw/.micropaw`.
- Render: free web services have no persistent disk, so they do not suit MicroPaw. A paid service with a disk does.
- Any VPS (for example Hetzner Cloud): install with the script and run `paw service install`, or use compose.
- Oracle Cloud Always Free (Arm, 2 OCPU, 12 GB): works, but idle instances can be reclaimed.
- Raspberry Pi 5 with a small local model: speed varies a lot with the model and cooling. Measure with `ollama run <model> --verbose`.

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
