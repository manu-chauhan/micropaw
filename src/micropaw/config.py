"""MicroPaw configuration — single Pydantic model, env var overrides, redaction."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, field_validator

load_dotenv()

CONFIG_DIR = Path(os.environ.get("MICROPAW_HOME") or Path.home() / ".micropaw")
load_dotenv(CONFIG_DIR / ".env")  # keys for a service live here; values already in the environment win
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = CONFIG_DIR / "data.db"


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


class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "gemini-2.5-flash"
    max_tokens: int = 4096
    base_url: str = "https://generativelanguage.googleapis.com"


# OpenAI-compatible servers usable by name in a model spec ("groq:llama-3.3-70b-versatile").
# name -> (base URL without /v1, env var holding the key; "" = local, no key needed)
OPENAI_COMPATIBLE: dict[str, tuple[str, str]] = {
    "openrouter": ("https://openrouter.ai/api", "OPENROUTER_API_KEY"),
    "groq": ("https://api.groq.com/openai", "GROQ_API_KEY"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    "mistral": ("https://api.mistral.ai", "MISTRAL_API_KEY"),
    "together": ("https://api.together.xyz", "TOGETHER_API_KEY"),
    "lmstudio": ("http://localhost:1234", ""),
    "llamacpp": ("http://localhost:8080", ""),
    "vllm": ("http://localhost:8000", ""),
}


class ModelsConfig(BaseModel):
    """Extra models by job, each "provider:model" and local or cloud; empty = use the main model.

    provider is claude, openai, gemini, ollama, or a name from OPENAI_COMPATIBLE.
    Examples: "ollama:gemma4:e4b", "claude:claude-haiku-5-5", "groq:llama-3.3-70b-versatile".
    """

    fast: str = ""    # summaries and sub-agents: cheap and quick is fine
    vision: str = ""  # images, for when the main model can't see


class MCPServerConfig(BaseModel):
    """An MCP server MicroPaw starts as a child process. Pin versions in args (pkg@1.2.3)."""

    command: str              # e.g. "npx", "uvx", "python"
    args: list[str] = []
    env: dict[str, str] = {}  # only these are added; MicroPaw's own keys are never passed on
    allow: list[str] = []     # tool names that run without asking (until outside content is read)
    timeout: float = 300.0    # seconds per call; decompiling can be slow
    enabled: bool = True


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


class VoiceConfig(BaseModel):
    """Speech-to-text for voice notes, via any OpenAI-style /v1/audio/transcriptions endpoint.

    stt is "openai:<model>", a hosted preset ("groq:whisper-large-v3-turbo"), or "local:<model>"
    for a server on your machine at stt_url (e.g. a whisper.cpp or faster-whisper server).
    Empty = voice notes are not transcribed.
    """

    stt: str = ""
    stt_url: str = "http://localhost:8000"


class SchedulerConfig(BaseModel):
    heartbeat_interval: int = 1800  # seconds (30 minutes)
    max_jobs: int = 50


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


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

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
    mcp_servers: dict[str, MCPServerConfig] = {}
    fallback_providers: list[str] = []


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
        except json.JSONDecodeError as e:
            _logger.warning("Config file corrupt, using defaults: %s", e)
        except OSError as e:
            _logger.warning("Config file unreadable, using defaults: %s", e)

    # Migrate old email+calendar config → unified google config
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
    # Clean stale keys
    raw.pop("calendar", None)
    raw.pop("email", None)

    _apply_env_overrides(raw)
    raw.pop("config_version", None)
    return Config(**raw)


def save_config(config: Config) -> None:
    """Save config to ~/.micropaw/config.json with restrictive permissions."""
    _write_private(config.model_dump())


def _write_private(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write with 0o600 from creation — avoid world-readable TOCTOU window
    fd = os.open(str(CONFIG_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)  # the mode above only applies when the file is new
        os.write(fd, json.dumps(data, indent=2).encode())
    finally:
        os.close(fd)


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
