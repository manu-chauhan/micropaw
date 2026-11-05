"""MicroPaw configuration — single Pydantic model, env var overrides, redaction."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from micropaw.providers.registry import BUILTIN, PRESETS

load_dotenv()

CONFIG_DIR = Path(os.environ.get("MICROPAW_HOME") or Path.home() / ".micropaw")
load_dotenv(CONFIG_DIR / ".env")  # keys for a service live here; values already in the environment win
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = CONFIG_DIR / "data.db"
SCHEMA_VERSION = 1  # bump when a migration is added to _migrate()


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
    provider: Literal["auto", "searxng", "brave", "duckduckgo"] = "auto"
    brave_api_key: str = ""
    searxng_url: str = ""  # self-hosted SearXNG URL, e.g. "http://localhost:8888"


class PrivacyConfig(BaseModel):
    """Where data may go. airgapped: nothing leaves this machine (and the LAN, if allowed).
    local: models, embeddings and speech only on this machine or the LAN; web search only through your own servers.
    normal: cloud allowed where you configure it, with labels saying where each reply went.
    """

    mode: Literal["airgapped", "local", "normal"] = "normal"
    allow_lan: bool = True          # private ranges and Tailscale count as "this side" in airgapped and local
    lan_hosts: list[str] = []       # extra host names or CIDRs that count as local (names are never resolved to decide)
    allow_cloud_fallback: bool = False  # a local main model may fail over to a cloud model


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


# ---------------------------------------------------------------------------
# Env var mapping
# ---------------------------------------------------------------------------

_ENV_OVERRIDES: list[tuple[str, list[str]]] = [
    # Primary provider selection
    ("MICROPAW_PROVIDER", ["provider"]),
    # Provider keys
    ("ANTHROPIC_API_KEY", ["provider_config", "claude", "api_key"]),
    ("OPENAI_API_KEY", ["provider_config", "openai", "api_key"]),
    ("GEMINI_API_KEY", ["provider_config", "gemini", "api_key"]),
    ("OLLAMA_HOST", ["provider_config", "ollama", "host"]),
    # Model overrides
    ("CLAUDE_MODEL", ["provider_config", "claude", "model"]),
    ("OPENAI_MODEL", ["provider_config", "openai", "model"]),
    ("GEMINI_MODEL", ["provider_config", "gemini", "model"]),
    ("OLLAMA_MODEL", ["provider_config", "ollama", "model"]),
    # Channel tokens
    ("TELEGRAM_BOT_TOKEN", ["channels", "telegram", "token"]),
    ("DISCORD_BOT_TOKEN", ["channels", "discord", "token"]),
    ("SLACK_BOT_TOKEN", ["channels", "slack", "bot_token"]),
    ("SLACK_APP_TOKEN", ["channels", "slack", "app_token"]),
    ("SLACK_SIGNING_SECRET", ["channels", "slack", "signing_secret"]),
    ("WHATSAPP_TOKEN", ["channels", "whatsapp", "token"]),
    ("WHATSAPP_PHONE_ID", ["channels", "whatsapp", "phone_id"]),
    ("WHATSAPP_VERIFY_TOKEN", ["channels", "whatsapp", "verify_token"]),
    ("WHATSAPP_APP_SECRET", ["channels", "whatsapp", "app_secret"]),
    ("WEBCHAT_AUTH_TOKEN", ["channels", "webchat", "auth_token"]),
    # Models for specific jobs ("provider:model")
    ("MICROPAW_FAST_MODEL", ["models", "fast"]),
    ("MICROPAW_VISION_MODEL", ["models", "vision"]),
    # Shell sandbox (Docker sets "off": the container is the boundary)
    ("MICROPAW_SANDBOX", ["permissions", "sandbox"]),
    # Privacy mode (the env var can only make it stricter; see micropaw.privacy)
    ("MICROPAW_PRIVACY", ["privacy", "mode"]),
    # Location
    ("MICROPAW_LOCATION", ["persona", "location"]),
    # Search
    ("BRAVE_API_KEY", ["search", "brave_api_key"]),
    ("SEARXNG_URL", ["search", "searxng_url"]),
    # Google (Gmail + Calendar)
    ("GOOGLE_CLIENT_ID", ["google", "client_id"]),
    ("GOOGLE_CLIENT_SECRET", ["google", "client_secret"]),
]


def _apply_env_overrides(raw: dict) -> dict:
    """Apply environment variable overrides to raw config dict."""
    for env_var, path in _ENV_OVERRIDES:
        value = os.getenv(env_var)
        if value is None:
            continue
        # OLLAMA_HOST=0.0.0.0[:port] is a bind address, not a connect address
        if env_var == "OLLAMA_HOST" and value.lstrip("htps:/").startswith("0.0.0.0"):
            value = value.replace("0.0.0.0", "127.0.0.1")
        d = raw
        for key in path[:-1]:
            d = d.setdefault(key, {})
        if env_var == "MICROPAW_PRIVACY":  # can only make the mode stricter, never looser
            from micropaw.privacy import _strictest
            value = _strictest(str(d.get(path[-1], "normal")), value.strip().lower())
        d[path[-1]] = value
    return raw


# ---------------------------------------------------------------------------
# Load / save / display
# ---------------------------------------------------------------------------

def config_exists() -> bool:
    return CONFIG_FILE.exists()


def load_config() -> Config:
    """Load config from ~/.micropaw/config.json with env var overrides."""
    raw: dict = {}
    on_disk = False  # raw came from a file that parsed
    _logger = logging.getLogger(__name__)

    if CONFIG_FILE.exists():
        # Warn if config is world-readable (contains API keys)
        # Skip on Windows/WSL-NTFS where all files show 0o666
        import platform
        if platform.system() != "Windows" and not str(CONFIG_FILE).startswith("/mnt/"):
            try:
                mode = CONFIG_FILE.stat().st_mode
                if mode & 0o044:  # group or other readable
                    _logger.warning(
                        "Config file %s is world-readable (mode %o). Run: chmod 600 %s",
                        CONFIG_FILE, mode & 0o777, CONFIG_FILE,
                    )
            except OSError:
                pass

        try:
            raw = json.loads(CONFIG_FILE.read_text())
            on_disk = True
        except json.JSONDecodeError as e:
            _logger.warning("Config file corrupt, using defaults: %s", e)
        except OSError as e:
            _logger.warning("Config file unreadable, using defaults: %s", e)

    if on_disk and raw.get("schema_version", 0) < SCHEMA_VERSION:
        raw = _migrate_file(raw)
    _apply_env_overrides(raw)
    raw.pop("config_version", None)
    return Config(**raw)


def _migrate(raw: dict) -> dict:
    """Bring an old config dict up to SCHEMA_VERSION. Pure: no file access."""
    # v0 -> v1: email + calendar became one google block; config_version became schema_version
    if ("calendar" in raw or "email" in raw) and "google" not in raw:
        cal = raw.pop("calendar", {})
        email_cfg = raw.pop("email", {})
        raw["google"] = {
            "enabled": cal.get("enabled", False) or email_cfg.get("enabled", False),
            "client_id": cal.get("client_id", ""),
            "client_secret": cal.get("client_secret", ""),
            "gmail_enabled": email_cfg.get("enabled", False),
            "calendar_enabled": cal.get("enabled", False),
        }
    raw.pop("calendar", None)
    raw.pop("email", None)
    raw.pop("config_version", None)
    raw["schema_version"] = SCHEMA_VERSION
    return raw


def _migrate_file(raw: dict) -> dict:
    """Migrate, keeping a copy of the old file first. Files are written 0600. Read-only is fine: stay in memory."""
    old_version = raw.get("schema_version", 0)
    migrated = _migrate(raw)
    backup = CONFIG_FILE.with_name(f"{CONFIG_FILE.name}.v{old_version}.bak")
    try:
        if not backup.exists():
            _write_bytes(backup, CONFIG_FILE.read_bytes())
        _write_private(migrated)
        logging.getLogger(__name__).info("Config migrated to schema %d (old copy: %s)", SCHEMA_VERSION, backup)
    except OSError as e:
        logging.getLogger(__name__).warning("Could not write the migrated config: %s", e)
    return migrated


def save_config(config: Config) -> None:
    """Save config to ~/.micropaw/config.json with restrictive permissions."""
    _write_private(config.model_dump())


def private_dir(path: Path) -> None:
    """Create a folder; on POSIX every folder from it up to ~/.micropaw is made owner-only (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        for d in (path, *path.parents):
            if d != CONFIG_DIR and CONFIG_DIR not in d.parents:
                break
            try:
                d.chmod(0o700)
            except OSError:
                break


def _write_bytes(path: Path, data: bytes) -> None:
    """Write 0600 from creation (no world-readable window) through a temp file, then replace."""
    private_dir(path.parent)
    tmp = path.with_name(f"{path.name}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # the mode above only applies when the file is new
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _write_private(data: dict) -> None:
    _write_bytes(CONFIG_FILE, json.dumps(data, indent=2).encode())


def update_config_file(change: Callable[[dict], None], validate: Callable[[Config], None] | None = None) -> dict:
    """Edit config.json on disk (never the environment's keys), check the result, then write it 0600.

    Raises ValueError, writing nothing, when the edited config is not valid.
    """
    raw: dict = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    if CONFIG_FILE.exists() and raw.get("schema_version", 0) < SCHEMA_VERSION:
        raw = _migrate_file(raw)
    change(raw)
    config = Config(**{k: v for k, v in raw.items() if k != "config_version"})
    if validate:
        validate(config)
    _write_private(raw)
    return raw


def save_mcp_server(name: str, entry: dict) -> None:
    """Write one mcp_servers entry (replacing any old one). Edits the JSON on disk, like set_allowed_user."""
    raw: dict = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    raw.setdefault("mcp_servers", {})[name] = entry
    _write_private(raw)


def pin_mcp_tools(name: str, digest: str) -> bool:
    """Store the approved tool-list hash of an existing mcp_servers entry. False if there is no such entry."""
    if not CONFIG_FILE.exists():
        return False
    raw = json.loads(CONFIG_FILE.read_text())
    entry = raw.get("mcp_servers", {}).get(name)
    if entry is None:
        return False
    entry["tools_hash"] = digest
    _write_private(raw)
    return True


def set_allowed_user(platform: str, user_id: str, remove: bool = False) -> list[str]:
    """Add or remove one ID in channels.<platform>.allowed_users and return the new list.

    Edits the JSON on disk directly, so keys that came from environment variables
    are never copied into the file.
    """
    raw: dict = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    channel = raw.setdefault("channels", {}).setdefault(platform, {})
    ids = [str(u) for u in channel.get("allowed_users", [])]
    if remove:
        ids = [u for u in ids if u != user_id]
    elif user_id not in ids:
        ids.append(user_id)
    channel["allowed_users"] = ids
    _write_private(raw)
    return ids


def _redact(value: str) -> str:
    """Redact a secret value for display."""
    if len(value) > 12:
        return value[:8] + "..." + value[-4:]
    return "***" if value else ""


def display_config(config: Config) -> dict:
    """Return a redacted copy of the config for display."""
    data = config.model_dump()

    # Redact provider API keys
    for provider in ("claude", "openai", "gemini"):
        key = data.get("provider_config", {}).get(provider, {}).get("api_key", "")
        if key:
            data["provider_config"][provider]["api_key"] = _redact(key)

    # Redact channel tokens
    secret_fields = {
        "telegram": ["token"],
        "discord": ["token"],
        "slack": ["bot_token", "app_token", "signing_secret"],
        "whatsapp": ["token", "app_secret", "verify_token"],
        "webchat": ["auth_token"],
    }
    for channel, fields in secret_fields.items():
        ch = data.get("channels", {}).get(channel, {})
        for field in fields:
            if ch.get(field):
                ch[field] = _redact(ch[field])

    # Redact search API key
    search_key = data.get("search", {}).get("brave_api_key", "")
    if search_key:
        data["search"]["brave_api_key"] = _redact(search_key)

    # Redact Google credentials
    google = data.get("google", {})
    for gkey in ("client_id", "client_secret"):
        if google.get(gkey):
            google[gkey] = _redact(google[gkey])

    return data
