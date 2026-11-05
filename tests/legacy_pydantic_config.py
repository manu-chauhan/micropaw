"""Frozen copy of the pydantic config models from MicroPaw 0.3 (before they became dataclasses).

Only tests/test_config_golden.py imports this. It needs pydantic (a dev dependency) and must never change:
it is the reference the new loader is compared with.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from micropaw.config import SCHEMA_VERSION, server_url
from micropaw.providers.registry import BUILTIN, PRESETS

# ---------------------------------------------------------------------------
# Provider configs
# ---------------------------------------------------------------------------

class ClaudeConfig(BaseModel):
    api_key: str = ""
    model: str = "claude-sonnet-5-5"
    max_tokens: int = 16000  # room for thinking plus the reply
    base_url: str = "https://api.anthropic.com"
    effort: str = "medium"  # low | medium | high | xhigh | max; "" = model default (older models reject effort)


class OpenAIConfig(BaseModel):
    api_key: str = ""
    model: str = "gpt-4o"
    max_tokens: int = 4096
    base_url: str = "https://api.openai.com"


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    model: str = "llama3.1"
    max_tokens: int = 4096
    num_ctx: int = 0          # 0 = by RAM: 8192 up to 16 GB, else 16384. Ollama cuts longer prompts silently.
    keep_alive: str = "30m"   # how long a model stays loaded after the last call

    @field_validator("host")
    @classmethod
    def _host_is_an_address(cls, v: str) -> str:
        return server_url(v, "ollama")


class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "gemini-2.5-flash"
    max_tokens: int = 4096
    base_url: str = "https://generativelanguage.googleapis.com"


class ModelsConfig(BaseModel):
    """Extra models by job, each "provider:model" and local or cloud; empty = use the main model.

    provider is claude, openai, gemini, ollama, a preset (providers/registry.py) or one of your own (providers).
    Examples: "ollama:gemma4:e4b", "claude:claude-haiku-5-5", "groq:llama-3.3-70b-versatile".
    """

    fast: str = ""    # summaries and sub-agents: cheap and quick is fine
    vision: str = ""  # images, for when the main model can't see


class CustomProviderConfig(BaseModel):
    """Your own OpenAI-style server: `"providers": {"myvllm": {"base_url": "http://gpu-box:8000/v1", ...}}`."""

    base_url: str
    api_key_env: str = ""  # name of the environment variable that holds the key; "" = no key
    kind: Literal["openai", "ollama"] = "openai"  # ollama: the native Ollama API, so num_ctx and keep_alive work
    num_ctx: int = 0       # kind ollama: context size; 0 = 8192 for a server on another machine
    keep_alive: str = ""   # kind ollama: how long the model stays loaded; "" = the ollama.keep_alive setting

    @field_validator("base_url")
    @classmethod
    def _base_url_is_an_address(cls, v: str) -> str:
        return server_url(v)


class AgentPersona(BaseModel):
    name: str = ""
    personality: str = ""
    system_prompt_extra: str = ""


class ToolFilter(BaseModel):
    """Tool names or patterns ("mcp_*"). A non-empty allow list is all the agent gets; deny removes after that."""

    allow: list[str] = []
    deny: list[str] = []


class AgentConfig(BaseModel):
    """A named agent. Every field is optional and inherits from the top-level config.

    model, fast, vision, fallbacks are specs ("provider:model"). fast = "" means the main model.
    fallbacks = [] means strict (no fallback); leaving it out inherits the top-level list.
    """

    model_config = ConfigDict(extra="forbid")  # a typo or an unsupported key (such as "users") must not pass silently

    model: str = ""
    fast: str | None = None
    vision: str | None = None
    fallbacks: list[str] | None = None
    persona: AgentPersona = AgentPersona()
    tools: ToolFilter = ToolFilter()
    workspace: str = ""
    history: Literal["shared", "separate"] = "shared"  # separate = own conversation ids
    # A local agent never inherits cloud fallbacks or roles. This lets it use the ones it lists itself.
    allow_cloud: bool = False


class MCPServerConfig(BaseModel):
    """An MCP server MicroPaw starts as a child process. Pin versions in args (pkg@1.2.3)."""

    command: str = ""         # e.g. "npx", "uvx", "python"; empty when `url` is set
    url: str = ""             # streamable HTTP server instead of a child process
    headers: dict[str, str] = {}  # sent with every request; ${ENV_NAME} is read from the environment
    args: list[str] = []
    env: dict[str, str] = {}  # only these are added; MicroPaw's own keys are never passed on
    allow: list[str] = []     # tool names that run without asking (until outside content is read)
    timeout: float = 300.0    # seconds per call; decompiling can be slow
    enabled: bool = True
    tools_hash: str = ""      # sha256 of the tool list the owner approved; a change blocks the server
    network: bool = False     # local mode only: the child may use the network (ignored in airgapped)
    paths: list[str] = []     # folders the child may read when it is isolated (privacy local or airgapped)
    write_paths: list[str] = []  # folders it may also write

    @model_validator(mode="after")
    def _needs_command_or_url(self) -> MCPServerConfig:
        if not self.command and not self.url:
            raise ValueError("an MCP server needs a command or a url")
        return self


class ProviderConfig(BaseModel):
    claude: ClaudeConfig = ClaudeConfig()
    openai: OpenAIConfig = OpenAIConfig()
    ollama: OllamaConfig = OllamaConfig()
    gemini: GeminiConfig = GeminiConfig()


# ---------------------------------------------------------------------------
# Channel configs
# ---------------------------------------------------------------------------

class TerminalChannelConfig(BaseModel):
    enabled: bool = True


class TelegramChannelConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    allowed_users: list[str] = []


class DiscordChannelConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    allowed_users: list[str] = []


class SlackChannelConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""
    allowed_users: list[str] = []


class WhatsAppChannelConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    phone_id: str = ""
    verify_token: str = ""
    app_secret: str = ""
    allowed_users: list[str] = []


class WebChatChannelConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""
    allowed_origins: list[str] = []


class ChannelsConfig(BaseModel):
    terminal: TerminalChannelConfig = TerminalChannelConfig()
    telegram: TelegramChannelConfig = TelegramChannelConfig()
    discord: DiscordChannelConfig = DiscordChannelConfig()
    slack: SlackChannelConfig = SlackChannelConfig()
    whatsapp: WhatsAppChannelConfig = WhatsAppChannelConfig()
    webchat: WebChatChannelConfig = WebChatChannelConfig()


# ---------------------------------------------------------------------------
# Other configs
# ---------------------------------------------------------------------------

class PermissionsConfig(BaseModel):
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_web_browse: bool = True
    # Ask before every host shell command (False: only once outside content was read)
    confirm_dangerous: bool = True
    disabled_tools: list[str] = []  # tool names to disable, e.g. ["web_search", "http_request"]
    workspace: str = "~/micropaw"  # files written here, and shell runs here, without asking
    # Where shell commands run: auto, off, bwrap, seatbelt, docker or podman
    sandbox: Literal["auto", "off", "bwrap", "seatbelt", "docker", "podman"] = "auto"
    sandbox_network: bool = False  # let sandboxed commands use the network
    sandbox_image: str = "python:3.13-slim"  # image for docker and podman


class PersonaConfig(BaseModel):
    name: str = "MicroPaw"
    personality: str = "friendly, helpful, and clever"
    location: str = ""  # e.g. "San Francisco, CA" — used for "near me" queries
    system_prompt_extra: str = ""


class RateLimitConfig(BaseModel):
    messages_per_minute: int = 20
    burst_limit: int = 5


class EmbeddingConfig(BaseModel):
    enabled: str = "auto"  # "auto", "true", "false"
    provider: Literal["auto", "local", "openai", "ollama"] = "auto"
    model: str = ""  # empty = auto-select best model for provider
    base_url: str = ""  # an embedding server on another machine; "" = the Ollama host, or OpenAI's own address
    api_key_env: str = ""  # name of the environment variable that holds the key for base_url; "" = none
    keep_alive: str = "1m"  # how long Ollama keeps the embedding model loaded after the last message

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_enabled(cls, v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v).lower()


class SearchConfig(BaseModel):
    """off: no search. offline: Kiwix and your own documents, no internet. searxng: your own server.
    auto: your own SearXNG or Brave if set, else public instances (never in airgapped or local mode).
    """

    provider: Literal["auto", "off", "offline", "kiwix", "docs", "searxng", "brave", "duckduckgo"] = "auto"
    brave_api_key: str = ""
    searxng_url: str = ""  # self-hosted SearXNG URL, e.g. "http://localhost:8888"
    kiwix_url: str = ""    # kiwix-serve on this computer or your network, e.g. "http://localhost:8080"
    kiwix_books: list[str] = []  # book names to search (up to 5), e.g. ["wikipedia_en_all_nopic_2026-06"]
    proxy: str = ""        # socks5h:// or socks5:// proxy for your own SearXNG (needs: pip install micropaw[tor])


class DisplayConfig(BaseModel):
    show_provider: bool = True  # end each reply with a line naming the model server that answered


class PrivacyConfig(BaseModel):
    """Where data may go. airgapped: nothing leaves this machine (and the LAN, if allowed).
    local: models, embeddings and speech only on this machine or the LAN; web search only through your own servers.
    normal: cloud allowed where you configure it, with labels saying where each reply went.
    """

    mode: Literal["airgapped", "local", "normal"] = "normal"

    @field_validator("mode", mode="before")
    @classmethod
    def _spelling(cls, value):
        from micropaw.privacy import PrivacyError, parse_mode
        try:
            return (parse_mode(value) or "normal") if isinstance(value, str) else value
        except PrivacyError as e:
            raise ValueError(str(e)) from e

    allow_lan: bool = True          # private ranges and Tailscale count as "this side" in airgapped and local
    lan_hosts: list[str] = []       # extra host names or CIDRs that count as local (names are never resolved to decide)
    allow_cloud_fallback: bool = False  # a local main model may fail over to a cloud model
    # a local main model may use a cloud fast or vision model (normal mode); each delegate task asks first
    allow_cloud_helpers: bool = False
    share_with_cloud: bool = True       # USER.md and the location go to a cloud model (paw setup turns this off)
    # Local-mode exceptions: telegram, discord, slack, whatsapp, google, web_fetch, web_browse, shell_network
    allow: list[str] = []
    # Airgapped: MCP servers over HTTP that you vouch for. MicroPaw cannot see where they send data.
    trusted_local_services: list[str] = []


class StorageConfig(BaseModel):
    """What MicroPaw keeps on disk, for how long, and what it writes to logs."""

    retention_days: int = 0         # delete messages, summaries, chunks and email records older than this (0 = keep)
    secure_delete: bool = True      # overwrite deleted rows instead of leaving them in free database pages
    auto_ingest_max_chunks: int = 20    # chunks kept from one file or page the agent read (0 = all of it)
    max_chunks: int = 10000         # most document chunks kept; the oldest go first (0 = no limit)
    log_content: bool = False       # allow message text in logs, even at DEBUG
    audit: Literal["full", "names"] = "full"  # names: the audit log keeps tool names and outcomes, no arguments


class VoiceConfig(BaseModel):
    """Speech-to-text for voice notes, via any OpenAI-style /v1/audio/transcriptions endpoint.

    stt is "openai:<model>", a hosted preset ("groq:whisper-large-v3-turbo"), or "local:<model>"
    for a server on your machine at stt_url (e.g. a whisper.cpp or faster-whisper server).
    Empty = voice notes are not transcribed.

    tts is the same kind of spec for spoken replies ("openai:gpt-4o-mini-tts", or "local:<model>" for an
    OpenAI-style /v1/audio/speech server at tts_url, such as Kokoro-FastAPI). With reply_with_voice on,
    a voice note from the owner gets a voice reply. tts_voice is the voice name
    ("" = "alloy" for OpenAI, "af_heart" for a local server).
    """

    stt: str = ""
    stt_url: str = "http://localhost:8000"
    tts: str = ""
    tts_url: str = "http://localhost:8880"
    tts_voice: str = ""
    reply_with_voice: bool = False


class SchedulerConfig(BaseModel):
    heartbeat_interval: int = 1800  # seconds (30 minutes)
    max_jobs: int = 50
    email_poll_seconds: int = 300   # how often Gmail is checked for email-triggered tasks


class GoogleConfig(BaseModel):
    enabled: bool = False
    client_id: str = ""       # Google Cloud OAuth2 client ID
    client_secret: str = ""   # Google Cloud OAuth2 client secret
    gmail_enabled: bool = True
    calendar_enabled: bool = True


class ContextConfig(BaseModel):
    max_history_messages: int = 50
    compaction_threshold: int = 30
    max_tool_iterations: int = 15
    max_subagent_iterations: int = 25
    max_message_size: int = 100_000  # max input message size in characters
    window_tokens: int = 0  # model context size; 0 = auto (8192 for local servers, else 100000)


class BudgetConfig(BaseModel):
    daily_usd: float = 0.0          # 0 = no dollar cap
    daily_tokens: int = 2_000_000   # cap for cloud models with no known price, used when a dollar cap is set
    warn_at: float = 0.8            # one-line heads-up when this share of the cap is used


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class Config(BaseModel):
    config_version: str = "1.0"
    provider: Literal["claude", "openai", "ollama", "gemini"] = "claude"
    provider_config: ProviderConfig = ProviderConfig()
    channels: ChannelsConfig = ChannelsConfig()
    permissions: PermissionsConfig = PermissionsConfig()
    persona: PersonaConfig = PersonaConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    search: SearchConfig = SearchConfig()
    context: ContextConfig = ContextConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    google: GoogleConfig = GoogleConfig()
    models: ModelsConfig = ModelsConfig()
    voice: VoiceConfig = VoiceConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    storage: StorageConfig = StorageConfig()
    mcp_servers: dict[str, MCPServerConfig] = {}
    fallback_providers: list[str] = []
    budget: BudgetConfig = BudgetConfig()
    schema_version: int = SCHEMA_VERSION
    agents: dict[str, AgentConfig] = {}  # absent = one agent "main" built from the fields above
    routes: dict[str, str] = {}          # "telegram:-100123" or "discord:*" -> agent name
    providers: dict[str, CustomProviderConfig] = {}
    # Tools shown to the model: auto = a short list for a local model and all of them for a cloud one
    toolset: Literal["auto", "lean", "all"] = "auto"
    display: DisplayConfig = DisplayConfig()

    @model_validator(mode="after")
    def _check_agents(self) -> Config:
        known = {*BUILTIN, *PRESETS, *(n.lower() for n in self.providers)}

        def check(where: str, spec: str) -> None:
            name = spec.partition(":")[0].strip().lower()
            if spec and name not in known:
                raise ValueError(f"{where}: unknown provider '{name}' in '{spec}'. Known: {', '.join(sorted(known))}")

        for name, agent in self.agents.items():
            if not _AGENT_NAME.match(name):
                raise ValueError(f"Agent name '{name}': use lowercase letters, digits, - and _ (at most 32).")
            for role in ("model", "fast", "vision"):
                check(f"agents.{name}.{role}", getattr(agent, role) or "")
            for spec in agent.fallbacks or []:
                check(f"agents.{name}.fallbacks", spec)
        for key, target in self.routes.items():
            if ":" not in key:
                raise ValueError(f"Route '{key}': write it as platform:chat (telegram:-100123) or platform:* .")
            if target != "main" and target not in self.agents:
                raise ValueError(f"Route '{key}' points to agent '{target}', which is not in agents.")
        return self


def config_secrets(config) -> dict[str, str]:
    """utils/redact.config_secrets as it was in 0.3 (it called model_dump)."""
    from micropaw.utils.redact import SECRET_NAME

    found: dict[str, str] = {}

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, str) and node and SECRET_NAME.search(path.rsplit(".", 1)[-1]) \
                and not path.endswith("_env"):
            found[path] = node

    data = config.model_dump()
    servers = data.pop("mcp_servers", {})
    walk(data, "")
    for name, server in servers.items():
        for var, value in server.get("env", {}).items():
            found[f"mcp.{name}.{var}"] = value
    return found
