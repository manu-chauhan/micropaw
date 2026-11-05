"""MicroPaw configuration: one dataclass tree, env var overrides, redaction."""

import dataclasses
import json
import logging
import os
import re
import types
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args, get_origin
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

CONFIG_DIR = Path(os.environ.get("MICROPAW_HOME") or Path.home() / ".micropaw")
load_dotenv(CONFIG_DIR / ".env")  # keys for a service live here; values already in the environment win
CONFIG_FILE = CONFIG_DIR / "config.json"
DB_FILE = CONFIG_DIR / "data.db"
SCHEMA_VERSION = 1  # bump when a migration is added to _migrate()


class ConfigError(ValueError):
    """The config file or an environment override holds a value that cannot be used. The text is for the user."""


# ---------------------------------------------------------------------------
# Typed server addresses
# ---------------------------------------------------------------------------

OLLAMA_PORT = 11434


def server_url(text: str, kind: str = "openai") -> str:
    """One rule for a model server address typed by a person or set in OLLAMA_HOST.

    No scheme means http. No scheme and no port means 11434 for kind "ollama" (nothing is added for
    "openai", where `paw models add` finds the port by probing). 0.0.0.0 and [::] are bind addresses,
    so they become loopback. For kind "ollama" a trailing /api or /v1 is dropped, because that client
    adds its own paths. Raises ValueError, in plain words, when the text is not an address.
    """
    text = text or ""
    raw = text.strip()
    bad = ValueError(f"{'Ollama host' if kind == 'ollama' else 'Server address'} '{text}' is not an address. "
                     "Example: 192.168.1.50 or http://gpu-box.local:11434")
    bare = "://" not in raw
    if not raw or raw.endswith("://") or any(c.isspace() for c in raw):
        raise bad
    raw = raw.rstrip("/")
    try:
        parsed = urlparse(f"http://{raw}" if bare else raw)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise bad from None
    if not host or parsed.scheme not in ("http", "https"):
        raise bad
    netloc = parsed.netloc
    if host in ("0.0.0.0", "::"):
        netloc = netloc.replace("[::]", "[::1]") if host == "::" else netloc.replace("0.0.0.0", "127.0.0.1")
    if bare and port is None and kind == "ollama":
        netloc += f":{OLLAMA_PORT}"
    path = parsed.path.rstrip("/")
    if kind == "ollama":
        for tail in ("/api", "/v1"):
            if path.endswith(tail):
                path = path[: -len(tail)]
    return f"{parsed.scheme}://{netloc}{path}"


# ---------------------------------------------------------------------------
# Model base: reads a JSON-style dict into typed fields, with the checks the config has always had
# ---------------------------------------------------------------------------

class ConfigValueError(ValueError):
    """A value in a config dict cannot be used. loc is the dotted path to it, why is in plain words."""

    def __init__(self, loc: list, why: str):
        super().__init__(why)
        self.loc, self.why = loc, why

    def __str__(self) -> str:
        return f"{'.'.join(map(str, self.loc))}: {self.why}" if self.loc else self.why


_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})
_WHOLE = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)*(?:\.0+)?")  # ASCII only: "5" and "5.0" are whole numbers, "5." is not


def _coerce(tp: Any, v: Any) -> Any:
    """Check v against a field type and convert it the way a JSON config needs ("5" is 5, "yes" is True)."""
    origin = get_origin(tp)
    if origin is types.UnionType:  # X | None
        return None if v is None else _coerce(next(a for a in get_args(tp) if a is not type(None)), v)
    if origin is Literal:
        if isinstance(v, str) and v in get_args(tp):
            return v
        choices = [repr(a) for a in get_args(tp)]
        head = ", ".join(choices[:-1]) + " or " if len(choices) > 1 else ""
        raise ConfigValueError([], "Input should be " + head + choices[-1])
    if origin is list:
        if not isinstance(v, (list, tuple)):
            raise ConfigValueError([], "Input should be a valid list")
        out = []
        for i, item in enumerate(v):
            try:
                out.append(_coerce(get_args(tp)[0], item))
            except ConfigValueError as e:
                e.loc.insert(0, i)
                raise
        return out
    if origin is dict:
        if not isinstance(v, dict):
            raise ConfigValueError([], "Input should be a valid dictionary")
        key_type, value_type = get_args(tp)
        out = {}
        for k, item in v.items():
            try:
                out[_coerce(key_type, k)] = _coerce(value_type, item)
            except ConfigValueError as e:
                e.loc.insert(0, k)
                raise
        return out
    if dataclasses.is_dataclass(tp):
        return v if isinstance(v, tp) else tp.from_dict(v)
    if tp is str:
        if isinstance(v, str):
            return v
        raise ConfigValueError([], "Input should be a valid string")
    if tp is bool:
        if isinstance(v, bool):
            return v
        word = v.lower() if isinstance(v, str) else v if isinstance(v, (int, float)) else None
        if word in _TRUE or word == 1:
            return True
        if word in _FALSE or word == 0:
            return False
        tail = ", unable to interpret input" if word is not None else ""
        raise ConfigValueError([], "Input should be a valid boolean" + tail)
    if tp is int:
        if isinstance(v, str):  # "5" and "5.0" are fine, "1e3" is not
            text = v.strip()
            if not _WHOLE.fullmatch(text):
                raise ConfigValueError([], "Input should be a valid integer, unable to parse string as an integer")
            return int(text.split(".")[0].replace("_", ""))
        if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
            return int(v)
        raise ConfigValueError([], "Input should be a valid integer" + (
            ", got a number with a fractional part" if isinstance(v, float) else ""))
    if tp is float:
        if isinstance(v, str):
            try:
                if v.isascii():
                    return float(v.strip())
            except ValueError:
                pass
            raise ConfigValueError([], "Input should be a valid number, unable to parse string as a number")
        if isinstance(v, (int, float)):
            return float(v)
        raise ConfigValueError([], "Input should be a valid number")
    raise TypeError(f"config field type {tp!r} is not supported")


@dataclass
class _Model:
    """Base of every config section. Subclasses may set _pre and _post (field name to function, run before
    and after the type check), _forbid_extra, and define _check() for a rule that spans fields."""

    _pre = {}
    _post = {}
    _forbid_extra = False

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            v = getattr(self, f.name)
            try:
                if f.name in self._pre:
                    v = self._pre[f.name](v)
                v = _coerce(f.type, v)
                if f.name in self._post:
                    v = self._post[f.name](v)
            except ConfigValueError as e:
                e.loc.insert(0, f.name)
                raise
            except ValueError as e:
                raise ConfigValueError([f.name], str(e)) from e
            setattr(self, f.name, v)
        try:
            self._check()
        except ConfigValueError:
            raise
        except ValueError as e:
            raise ConfigValueError([], str(e)) from e

    def _check(self) -> None:
        pass

    @classmethod
    def from_dict(cls, raw: Any):
        """Build from a dict. Unknown keys are ignored (an agent entry rejects them); a missing required key fails."""
        if not isinstance(raw, dict):
            raise ConfigValueError([], f"Input should be a valid dictionary or instance of {cls.__name__}")
        names = {f.name for f in dataclasses.fields(cls)}
        extra = [k for k in raw if k not in names] if cls._forbid_extra else []
        for f in dataclasses.fields(cls):
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING and f.name not in raw:
                raise ConfigValueError([f.name], "Field required")
        try:
            obj = cls(**{k: v for k, v in raw.items() if k in names})
        except ConfigValueError as e:
            if extra and not e.loc:  # a rule across fields: an unknown key is reported first, a bad field before it
                raise ConfigValueError([extra[0]], "Extra inputs are not permitted") from None
            raise
        if extra:
            raise ConfigValueError([extra[0]], "Extra inputs are not permitted")
        return obj


def changed_fields(obj: _Model) -> dict:
    """The fields of a section that differ from their defaults."""
    return {f.name: v for f in dataclasses.fields(obj) if (v := getattr(obj, f.name)) !=
            (f.default if f.default is not dataclasses.MISSING else f.default_factory())}


def _sub(cls: type) -> Any:
    return field(default_factory=cls)


def _list() -> Any:
    return field(default_factory=list)


def _dict() -> Any:
    return field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider configs
# ---------------------------------------------------------------------------

@dataclass
class ClaudeConfig(_Model):
    api_key: str = ""
    model: str = "claude-sonnet-5-5"
    max_tokens: int = 16000  # room for thinking plus the reply
    base_url: str = "https://api.anthropic.com"
    effort: str = "medium"  # low | medium | high | xhigh | max; "" = model default (older models reject effort)


@dataclass
class OpenAIConfig(_Model):
    api_key: str = ""
    model: str = "gpt-4o"
    max_tokens: int = 4096
    base_url: str = "https://api.openai.com"


@dataclass
class OllamaConfig(_Model):
    host: str = "http://localhost:11434"
    model: str = "llama3.1"
    max_tokens: int = 4096
    num_ctx: int = 0          # 0 = by RAM: 8192 up to 16 GB, else 16384. Ollama cuts longer prompts silently.
    keep_alive: str = "30m"   # how long a model stays loaded after the last call

    _post = {"host": lambda v: server_url(v, "ollama")}


@dataclass
class GeminiConfig(_Model):
    api_key: str = ""
    model: str = "gemini-2.5-flash"
    max_tokens: int = 4096
    base_url: str = "https://generativelanguage.googleapis.com"


@dataclass
class ModelsConfig(_Model):
    """Extra models by job, each "provider:model" and local or cloud; empty = use the main model.

    provider is claude, openai, gemini, ollama, a preset (providers/registry.py) or one of your own (providers).
    Examples: "ollama:gemma4:e4b", "claude:claude-haiku-5-5", "groq:llama-3.3-70b-versatile".
    """

    fast: str = ""    # summaries and sub-agents: cheap and quick is fine
    vision: str = ""  # images, for when the main model can't see


@dataclass
class CustomProviderConfig(_Model):
    """Your own OpenAI-style server: `"providers": {"myvllm": {"base_url": "http://gpu-box:8000/v1", ...}}`."""

    base_url: str
    api_key_env: str = ""  # name of the environment variable that holds the key; "" = no key
    kind: Literal["openai", "ollama"] = "openai"  # ollama: the native Ollama API, so num_ctx and keep_alive work
    num_ctx: int = 0       # kind ollama: context size; 0 = 8192 for a server on another machine
    keep_alive: str = ""   # kind ollama: how long the model stays loaded; "" = the ollama.keep_alive setting

    _post = {"base_url": server_url}


@dataclass
class AgentPersona(_Model):
    name: str = ""
    personality: str = ""
    system_prompt_extra: str = ""


@dataclass
class ToolFilter(_Model):
    """Tool names or patterns ("mcp_*"). A non-empty allow list is all the agent gets; deny removes after that."""

    allow: list[str] = _list()
    deny: list[str] = _list()


@dataclass
class AgentConfig(_Model):
    """A named agent. Every field is optional and inherits from the top-level config.

    model, fast, vision, fallbacks are specs ("provider:model"). fast = "" means the main model.
    fallbacks = [] means strict (no fallback); leaving it out inherits the top-level list.
    """

    _forbid_extra = True  # a typo or an unsupported key (such as "users") must not pass silently

    model: str = ""
    fast: str | None = None
    vision: str | None = None
    fallbacks: list[str] | None = None
    persona: AgentPersona = _sub(AgentPersona)
    tools: ToolFilter = _sub(ToolFilter)
    workspace: str = ""
    history: Literal["shared", "separate"] = "shared"  # separate = own conversation ids
    # A local agent never inherits cloud fallbacks or roles. This lets it use the ones it lists itself.
    allow_cloud: bool = False


@dataclass
class MCPServerConfig(_Model):
    """An MCP server MicroPaw starts as a child process. Pin versions in args (pkg@1.2.3)."""

    command: str = ""         # e.g. "npx", "uvx", "python"; empty when `url` is set
    url: str = ""             # streamable HTTP server instead of a child process
    headers: dict[str, str] = _dict()  # sent with every request; ${ENV_NAME} is read from the environment
    args: list[str] = _list()
    env: dict[str, str] = _dict()  # only these are added; MicroPaw's own keys are never passed on
    allow: list[str] = _list()     # tool names that run without asking (until outside content is read)
    timeout: float = 300.0    # seconds per call; decompiling can be slow
    enabled: bool = True
    tools_hash: str = ""      # sha256 of the tool list the owner approved; a change blocks the server
    network: bool = False     # local mode only: the child may use the network (ignored in airgapped)
    paths: list[str] = _list()     # folders the child may read when it is isolated (privacy local or airgapped)
    write_paths: list[str] = _list()  # folders it may also write

    def _check(self) -> None:
        if not self.command and not self.url:
            raise ValueError("an MCP server needs a command or a url")


@dataclass
class ProviderConfig(_Model):
    claude: ClaudeConfig = _sub(ClaudeConfig)
    openai: OpenAIConfig = _sub(OpenAIConfig)
    ollama: OllamaConfig = _sub(OllamaConfig)
    gemini: GeminiConfig = _sub(GeminiConfig)


# ---------------------------------------------------------------------------
# Channel configs
# ---------------------------------------------------------------------------

@dataclass
class TerminalChannelConfig(_Model):
    enabled: bool = True


@dataclass
class TelegramChannelConfig(_Model):
    enabled: bool = False
    token: str = ""
    allowed_users: list[str] = _list()


@dataclass
class DiscordChannelConfig(_Model):
    enabled: bool = False
    token: str = ""
    allowed_users: list[str] = _list()


@dataclass
class SlackChannelConfig(_Model):
    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    signing_secret: str = ""
    allowed_users: list[str] = _list()


@dataclass
class WhatsAppChannelConfig(_Model):
    enabled: bool = False
    token: str = ""
    phone_id: str = ""
    verify_token: str = ""
    app_secret: str = ""
    allowed_users: list[str] = _list()


@dataclass
class WebChatChannelConfig(_Model):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    auth_token: str = ""
    allowed_origins: list[str] = _list()


@dataclass
class ChannelsConfig(_Model):
    terminal: TerminalChannelConfig = _sub(TerminalChannelConfig)
    telegram: TelegramChannelConfig = _sub(TelegramChannelConfig)
    discord: DiscordChannelConfig = _sub(DiscordChannelConfig)
    slack: SlackChannelConfig = _sub(SlackChannelConfig)
    whatsapp: WhatsAppChannelConfig = _sub(WhatsAppChannelConfig)
    webchat: WebChatChannelConfig = _sub(WebChatChannelConfig)


# ---------------------------------------------------------------------------
# Other configs
# ---------------------------------------------------------------------------

@dataclass
class PermissionsConfig(_Model):
    allow_shell: bool = False
    allow_file_write: bool = False
    allow_web_browse: bool = True
    # Ask before every host shell command (False: only once outside content was read)
    confirm_dangerous: bool = True
    disabled_tools: list[str] = _list()  # tool names to disable, e.g. ["web_search", "http_request"]
    workspace: str = "~/micropaw"  # files written here, and shell runs here, without asking
    # Where shell commands run: auto, off, bwrap, seatbelt, docker or podman
    sandbox: Literal["auto", "off", "bwrap", "seatbelt", "docker", "podman"] = "auto"
    sandbox_network: bool = False  # let sandboxed commands use the network
    sandbox_image: str = "python:3.13-slim"  # image for docker and podman


@dataclass
class PersonaConfig(_Model):
    name: str = "MicroPaw"
    personality: str = "friendly, helpful, and clever"
    location: str = ""  # e.g. "San Francisco, CA" — used for "near me" queries
    system_prompt_extra: str = ""


@dataclass
class RateLimitConfig(_Model):
    messages_per_minute: int = 20
    burst_limit: int = 5


@dataclass
class EmbeddingConfig(_Model):
    enabled: str = "auto"  # "auto", "true", "false"
    provider: Literal["auto", "local", "openai", "ollama"] = "auto"
    model: str = ""  # empty = auto-select best model for provider
    base_url: str = ""  # an embedding server on another machine; "" = the Ollama host, or OpenAI's own address
    api_key_env: str = ""  # name of the environment variable that holds the key for base_url; "" = none
    keep_alive: str = "1m"  # how long Ollama keeps the embedding model loaded after the last message

    _pre = {"enabled": lambda v: ("true" if v else "false") if isinstance(v, bool) else str(v).lower()}


@dataclass
class SearchConfig(_Model):
    """off: no search. offline: Kiwix and your own documents, no internet. searxng: your own server.
    auto: your own SearXNG or Brave if set, else public instances (never in airgapped or local mode).
    """

    provider: Literal["auto", "off", "offline", "kiwix", "docs", "searxng", "brave", "duckduckgo"] = "auto"
    brave_api_key: str = ""
    searxng_url: str = ""  # self-hosted SearXNG URL, e.g. "http://localhost:8888"
    kiwix_url: str = ""    # kiwix-serve on this computer or your network, e.g. "http://localhost:8080"
    kiwix_books: list[str] = _list()  # book names to search (up to 5), e.g. ["wikipedia_en_all_nopic_2026-06"]
    proxy: str = ""        # socks5h:// or socks5:// proxy for your own SearXNG (needs: pip install micropaw[tor])


@dataclass
class DisplayConfig(_Model):
    show_provider: bool = True  # end each reply with a line naming the model server that answered


def _mode_spelling(value: Any) -> Any:
    from micropaw.privacy import PrivacyError, parse_mode
    try:
        return (parse_mode(value) or "normal") if isinstance(value, str) else value
    except PrivacyError as e:
        raise ValueError(str(e)) from e


@dataclass
class PrivacyConfig(_Model):
    """Where data may go. airgapped: nothing leaves this machine (and the LAN, if allowed).
    local: models, embeddings and speech only on this machine or the LAN; web search only through your own servers.
    normal: cloud allowed where you configure it, with labels saying where each reply went.
    """

    mode: Literal["airgapped", "local", "normal"] = "normal"

    _pre = {"mode": lambda v: _mode_spelling(v)}

    allow_lan: bool = True          # private ranges and Tailscale count as "this side" in airgapped and local
    lan_hosts: list[str] = _list()  # extra host names or CIDRs that count as local (never resolved to decide)
    allow_cloud_fallback: bool = False  # a local main model may fail over to a cloud model
    # a local main model may use a cloud fast or vision model (normal mode); each delegate task asks first
    allow_cloud_helpers: bool = False
    share_with_cloud: bool = True       # USER.md and the location go to a cloud model (paw setup turns this off)
    # Local-mode exceptions: telegram, discord, slack, whatsapp, google, web_fetch, web_browse, shell_network
    allow: list[str] = _list()
    # Airgapped: MCP servers over HTTP that you vouch for. MicroPaw cannot see where they send data.
    trusted_local_services: list[str] = _list()


@dataclass
class StorageConfig(_Model):
    """What MicroPaw keeps on disk, for how long, and what it writes to logs."""

    retention_days: int = 0         # delete messages, summaries, chunks and email records older than this (0 = keep)
    secure_delete: bool = True      # overwrite deleted rows instead of leaving them in free database pages
    auto_ingest_max_chunks: int = 20    # chunks kept from one file or page the agent read (0 = all of it)
    max_chunks: int = 10000         # most document chunks kept; the oldest go first (0 = no limit)
    log_content: bool = False       # allow message text in logs, even at DEBUG
    audit: Literal["full", "names"] = "full"  # names: the audit log keeps tool names and outcomes, no arguments


@dataclass
class VoiceConfig(_Model):
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


@dataclass
class SchedulerConfig(_Model):
    heartbeat_interval: int = 1800  # seconds (30 minutes)
    max_jobs: int = 50
    email_poll_seconds: int = 300   # how often Gmail is checked for email-triggered tasks


@dataclass
class GoogleConfig(_Model):
    enabled: bool = False
    client_id: str = ""       # Google Cloud OAuth2 client ID
    client_secret: str = ""   # Google Cloud OAuth2 client secret
    gmail_enabled: bool = True
    calendar_enabled: bool = True


@dataclass
class ContextConfig(_Model):
    max_history_messages: int = 50
    compaction_threshold: int = 30
    max_tool_iterations: int = 15
    max_subagent_iterations: int = 25
    max_message_size: int = 100_000  # max input message size in characters
    window_tokens: int = 0  # model context size; 0 = auto (8192 for local servers, else 100000)


@dataclass
class BudgetConfig(_Model):
    daily_usd: float = 0.0          # 0 = no dollar cap
    daily_tokens: int = 2_000_000   # cap for cloud models with no known price, used when a dollar cap is set
    warn_at: float = 0.8            # one-line heads-up when this share of the cap is used


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------

_AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass
class Config(_Model):
    config_version: str = "1.0"
    provider: Literal["claude", "openai", "ollama", "gemini"] = "claude"
    provider_config: ProviderConfig = _sub(ProviderConfig)
    channels: ChannelsConfig = _sub(ChannelsConfig)
    permissions: PermissionsConfig = _sub(PermissionsConfig)
    persona: PersonaConfig = _sub(PersonaConfig)
    rate_limit: RateLimitConfig = _sub(RateLimitConfig)
    embedding: EmbeddingConfig = _sub(EmbeddingConfig)
    search: SearchConfig = _sub(SearchConfig)
    context: ContextConfig = _sub(ContextConfig)
    scheduler: SchedulerConfig = _sub(SchedulerConfig)
    google: GoogleConfig = _sub(GoogleConfig)
    models: ModelsConfig = _sub(ModelsConfig)
    voice: VoiceConfig = _sub(VoiceConfig)
    privacy: PrivacyConfig = _sub(PrivacyConfig)
    storage: StorageConfig = _sub(StorageConfig)
    mcp_servers: dict[str, MCPServerConfig] = _dict()
    fallback_providers: list[str] = _list()
    budget: BudgetConfig = _sub(BudgetConfig)
    schema_version: int = SCHEMA_VERSION
    agents: dict[str, AgentConfig] = _dict()  # absent = one agent "main" built from the fields above
    routes: dict[str, str] = _dict()          # "telegram:-100123" or "discord:*" -> agent name
    providers: dict[str, CustomProviderConfig] = _dict()
    # Tools shown to the model: auto = a short list for a local model and all of them for a cloud one
    toolset: Literal["auto", "lean", "all"] = "auto"
    display: DisplayConfig = _sub(DisplayConfig)

    def _check(self) -> None:
        if not (self.agents or self.routes):
            return
        from micropaw.providers.registry import BUILTIN, PRESETS  # loads httpx; only needed to check agents
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
        if env_var == "OLLAMA_HOST":  # Ollama's own format (0.0.0.0:11434, no scheme) becomes a full URL
            try:
                value = server_url(value, "ollama")
            except ValueError:
                pass  # kept as typed; validating the config then reports it in plain words
        d = raw
        for key in path[:-1]:
            d = d.setdefault(key, {})
        if env_var == "MICROPAW_PRIVACY":  # can only make the mode stricter, never looser
            from micropaw.privacy import _strictest, parse_mode
            value = _strictest(parse_mode(d.get(path[-1]), "privacy.mode") or "normal",
                               parse_mode(value, "MICROPAW_PRIVACY"))
        d[path[-1]] = value
    return raw


# ---------------------------------------------------------------------------
# Load / save / display
# ---------------------------------------------------------------------------

def config_exists() -> bool:
    return CONFIG_FILE.exists()


def load_config(hold: bool = True) -> Config:
    """Load config from ~/.micropaw/config.json with env var overrides. hold=False skips the privacy settings
    that a strict run is holding back (the owner chose to use them, at a terminal)."""
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
    try:
        cfg = Config.from_dict(raw)
    except ConfigValueError as e:
        raise ConfigError(f"The config file {CONFIG_FILE} has a problem at '{'.'.join(map(str, e.loc))}': "
                          f"{e.why.rstrip('.')}. Fix it, or run: paw setup --reset") from None
    from micropaw import privacy
    if hold:
        privacy.apply_floor(cfg)  # settings a strict run is holding back stay held in later loads
    return cfg


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
    _write_private(asdict(config))


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
    config = Config.from_dict({k: v for k, v in raw.items() if k != "config_version"})
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
    data = asdict(config)

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
        for name in fields:
            if ch.get(name):
                ch[name] = _redact(ch[name])

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
