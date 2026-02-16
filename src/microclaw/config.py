"""MicroClaw configuration — single Pydantic model, env var overrides, redaction."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

CONFIG_DIR = Path.home() / ".microclaw"
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = CONFIG_DIR / "data.db"


# ---------------------------------------------------------------------------
# Provider configs
# ---------------------------------------------------------------------------

class ClaudeConfig(BaseModel):
    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    base_url: str = "https://api.anthropic.com"


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


class WhatsAppChannelConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    phone_id: str = ""
    verify_token: str = ""
    app_secret: str = ""


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
    confirm_dangerous: bool = True
    disabled_tools: list[str] = []  # tool names to disable, e.g. ["web_search", "http_request"]


class PersonaConfig(BaseModel):
    name: str = "MicroClaw"
    personality: str = "friendly, helpful, and clever"
    system_prompt_extra: str = ""


class RateLimitConfig(BaseModel):
    messages_per_minute: int = 20
    burst_limit: int = 5


class EmbeddingConfig(BaseModel):
    enabled: bool = False
    provider: Literal["local", "openai", "ollama"] = "local"
    model: str = "BAAI/bge-small-en-v1.5"


class ContextConfig(BaseModel):
    max_history_messages: int = 50
    compaction_threshold: int = 30
    max_tool_iterations: int = 15
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
    context: ContextConfig = ContextConfig()
    fallback_providers: list[str] = []


# ---------------------------------------------------------------------------
# Env var mapping
# ---------------------------------------------------------------------------

_ENV_OVERRIDES: list[tuple[str, list[str]]] = [
    # Primary provider selection
    ("MICROCLAW_PROVIDER", ["provider"]),
    # Provider keys
    ("ANTHROPIC_API_KEY", ["provider_config", "claude", "api_key"]),
    ("OPENAI_API_KEY", ["provider_config", "openai", "api_key"]),
    ("GEMINI_API_KEY", ["provider_config", "gemini", "api_key"]),
    ("OLLAMA_HOST", ["provider_config", "ollama", "host"]),
    # Channel tokens
    ("TELEGRAM_BOT_TOKEN", ["channels", "telegram", "token"]),
    ("DISCORD_BOT_TOKEN", ["channels", "discord", "token"]),
    ("SLACK_BOT_TOKEN", ["channels", "slack", "bot_token"]),
    ("SLACK_APP_TOKEN", ["channels", "slack", "app_token"]),
    ("SLACK_SIGNING_SECRET", ["channels", "slack", "signing_secret"]),
    ("WHATSAPP_TOKEN", ["channels", "whatsapp", "token"]),
    ("WHATSAPP_PHONE_ID", ["channels", "whatsapp", "phone_id"]),
    ("WHATSAPP_APP_SECRET", ["channels", "whatsapp", "app_secret"]),
    ("WEBCHAT_AUTH_TOKEN", ["channels", "webchat", "auth_token"]),
]


def _apply_env_overrides(raw: dict) -> dict:
    """Apply environment variable overrides to raw config dict."""
    for env_var, path in _ENV_OVERRIDES:
        value = os.getenv(env_var)
        if value is None:
            continue
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
    """Load config from ~/.microclaw/config.json with env var overrides."""
    raw: dict = {}

    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    _apply_env_overrides(raw)
    raw.pop("config_version", None)
    return Config(**raw)


def save_config(config: Config) -> None:
    """Save config to ~/.microclaw/config.json with restrictive permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config.model_dump(), indent=2))
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


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
        "whatsapp": ["token", "app_secret"],
        "webchat": ["auth_token"],
    }
    for channel, fields in secret_fields.items():
        ch = data.get("channels", {}).get(channel, {})
        for field in fields:
            if ch.get(field):
                ch[field] = _redact(ch[field])

    return data
