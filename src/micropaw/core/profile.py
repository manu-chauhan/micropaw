"""Agent profiles: config + inheritance + the private-agent rule, and the per-agent tool view."""

from __future__ import annotations

import copy
import fnmatch
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from micropaw.config import AgentConfig, Config
from micropaw.providers.registry import is_local_url
from micropaw.tools.base import Tool, ToolRegistry

if TYPE_CHECKING:
    from micropaw.providers.base import AIProvider
    from micropaw.providers.registry import Registry


logger = logging.getLogger(__name__)


class ProfileError(ValueError):
    """The agents in the config cannot be built. The message says what to change."""


@dataclass
class AgentProfile:
    name: str
    main: str                       # spec of the main model
    fast: str = ""                  # "" = the main model
    vision: str = ""
    fallbacks: list[str] = field(default_factory=list)
    personality: str = ""           # own persona fields; "" = the top-level persona
    persona_name: str = ""
    prompt_extra: str = ""
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    workspace: str = ""
    separate: bool = False          # own conversation ids
    private: bool = False           # every model it can reach is local
    dropped: list[str] = field(default_factory=list)  # inherited cloud entries a local agent did not take

    @property
    def prefix(self) -> str:
        return f"agent:{self.name}:" if self.separate else ""

    @property
    def memory_user(self) -> str:
        """Memories and documents follow the history: a separate (or private) agent keeps its own."""
        return f"agent:{self.name}" if self.separate else "_global"

    @property
    def filters_tools(self) -> bool:
        return bool(self.allow or self.deny)


@dataclass
class AgentRuntime:
    profile: AgentProfile
    config: Config                  # the config this agent sees (own persona, workspace, voice)
    provider: AIProvider
    fast: AIProvider | None
    vision: AIProvider | None


def _inherit(role: str, name: str, explicit: str | None, top: str, registry: Registry,
             restrict: bool, dropped: list[str]) -> str:
    """One role's spec. A local agent that did not ask for it itself does not take a cloud one."""
    if explicit is not None:
        if restrict and explicit and not registry.is_local(explicit):
            raise ProfileError(
                f"Agent '{name}' uses a local model, so its {role} model '{explicit}' must be local too. "
                "Set allow_cloud to true on the agent if it may use the cloud.")
        return explicit
    if restrict and top and not registry.is_local(top):
        dropped.append(top)
        return ""
    return top


def resolve_profiles(config: Config, registry: Registry) -> dict[str, AgentProfile]:
    """One profile per agent. With no `agents` in the config there is one, "main", from the top-level fields."""
    agents: dict[str, AgentConfig] = dict(config.agents)
    agents.setdefault("main", AgentConfig())
    profiles: dict[str, AgentProfile] = {}
    for name in sorted(agents, key=lambda n: n != "main"):  # main first: the others depend on it
        ac = agents[name]
        is_main = name == "main"
        main = ac.model or config.provider
        restrict = not is_main and registry.is_local(main) and not ac.allow_cloud
        dropped: list[str] = []
        fast = _inherit("fast", name, ac.fast, config.models.fast, registry, restrict, dropped)
        vision = _inherit("vision", name, ac.vision, config.models.vision, registry, restrict, dropped)
        if is_main and registry.is_local(main) and not config.privacy.allow_cloud_helpers:
            # Compaction, delegates and photos run on these: a local main model keeps them local unless allowed
            if fast and not registry.is_local(fast):
                dropped.append(fast)
                fast = ""
            if vision and not registry.is_local(vision):
                dropped.append(vision)
                vision = ""
        if ac.fallbacks is not None:
            fallbacks = list(ac.fallbacks)
            if restrict and (bad := [s for s in fallbacks if not registry.is_local(s)]):
                raise ProfileError(
                    f"Agent '{name}' uses a local model, so its fallback '{bad[0]}' must be local too. "
                    "Set allow_cloud to true on the agent if it may use the cloud.")
        else:
            fallbacks = [s for s in config.fallback_providers if not restrict or registry.is_local(s)]
            dropped += [s for s in config.fallback_providers if s not in fallbacks]
        specs = [s for s in (main, fast, vision, *fallbacks) if s]
        private = all(registry.is_local(s) for s in specs)
        main_private = name != "main" and profiles["main"].private
        profiles[name] = AgentProfile(
            name=name, main=main, fast=fast, vision=vision, fallbacks=fallbacks,
            personality=ac.persona.personality, persona_name=ac.persona.name,
            prompt_extra=ac.persona.system_prompt_extra,
            allow=list(ac.tools.allow), deny=list(ac.tools.deny), workspace=ac.workspace,
            # History stays shared (today's keys) unless the agent asks, is private, or main is private
            # (a cloud agent must not read a local-only main agent's conversation).
            separate=not is_main and (ac.history == "separate" or private or main_private),
            private=private, dropped=dropped,
        )
    return profiles


def agent_config(config: Config, profile: AgentProfile, registry: Registry) -> Config:
    """The config one agent runs with: its own persona and workspace, and no cloud voice services if private."""
    out = copy.deepcopy(config)
    if profile.persona_name:
        out.persona.name = profile.persona_name
    if profile.personality:
        out.persona.personality = profile.personality
    if profile.prompt_extra:
        out.persona.system_prompt_extra = profile.prompt_extra
    if profile.workspace:
        out.permissions.workspace = profile.workspace
    if profile.private and out.voice.stt:
        from micropaw.utils.voice import _endpoint
        endpoint = _endpoint(out)
        if endpoint is None or not is_local_url(endpoint[0]):
            out.voice.stt = ""
    if profile.private and out.voice.tts:
        from micropaw.utils.voice import tts_endpoint
        endpoint = tts_endpoint(out)
        if endpoint is None or not is_local_url(endpoint[0]):
            out.voice.tts = ""
    return out


def build_runtimes(config: Config, registry: Registry) -> dict[str, AgentRuntime]:
    """Providers for every agent, from the registry. Raises ProfileError when one has no usable main model."""
    runtimes: dict[str, AgentRuntime] = {}
    for name, profile in resolve_profiles(config, registry).items():
        if profile.dropped:
            logger.warning("Agent '%s' uses a local model, so these cloud models are not used: %s. "
                           "Set privacy.allow_cloud_helpers (or allow_cloud on the agent) to allow them.",
                           name, ", ".join(profile.dropped))
        provider = registry.role(name, "chat", [profile.main, *profile.fallbacks])
        if provider is None:
            why = registry.problems[-1] if registry.problems else "no provider"
            raise ProfileError(f"No usable model for agent '{name}' ({profile.main}). {why}")
        runtimes[name] = AgentRuntime(
            profile, agent_config(config, profile, registry), provider,
            registry.role(name, "fast", [profile.fast]) if profile.fast else None,
            registry.role(name, "vision", [profile.vision]) if profile.vision else None,
        )
    return runtimes


class ToolView(ToolRegistry):
    """One agent's window onto the shared tool registry. Rules are checked on every read, so tools that
    register later (MCP servers start in the background) show up for the agents that may use them."""

    def __init__(self, base: ToolRegistry, allow: list[str], deny: list[str],
                 bound: dict[str, Tool] | None = None) -> None:
        self._base = base
        self._allow = allow
        self._deny = deny
        self.bound = bound or {}  # per-agent copies of tools that depend on the agent's models

    def permits(self, name: str) -> bool:
        if self._allow and not any(fnmatch.fnmatchcase(name, p) for p in self._allow):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in self._deny)

    def register(self, tool: Tool) -> None:
        self._base.register(tool)

    def get(self, name: str) -> Tool | None:
        tool = self._base.get(name)
        return (self.bound.get(name) or tool) if tool is not None and self.permits(name) else None

    def all(self) -> list[Tool]:
        return [self.bound.get(t.name) or t for t in self._base.all() if self.permits(t.name)]

    def to_tool_defs(self):
        return [t.to_tool_def() for t in self.all()]

    @property
    def names(self) -> list[str]:
        return [t.name for t in self.all()]


def unknown_tool_names(patterns: list[str], names: list[str]) -> list[str]:
    """Entries that match no tool. Patterns with * pass: MCP tools may not be registered yet."""
    return [p for p in patterns if "*" not in p and not p.startswith("mcp_") and p not in names]


def build_view(base: ToolRegistry, runtime: AgentRuntime) -> ToolRegistry:
    """The registry an agent uses: the shared one for an unfiltered main agent, else a view with its own
    delegate (its fast model, its tools) and vision (its vision model) tools."""
    from micropaw.core.toolsets import default_allow
    p = runtime.profile
    allow = p.allow or default_allow(runtime)  # an agent's own list wins; else lean for a local model
    if p.name == "main" and not (allow or p.deny):
        return base
    view = ToolView(base, allow, p.deny)
    delegate, vision = base.get("delegate"), base.get("vision")
    if delegate is not None and hasattr(delegate, "bound"):
        view.bound["delegate"] = delegate.bound(runtime.fast or runtime.provider, view)
    if vision is not None and hasattr(vision, "bound"):
        view.bound["vision"] = vision.bound(runtime.vision or runtime.provider)
    return view


def providers_in(provider: AIProvider | None) -> list[AIProvider]:
    """Every provider inside a chain or wrapper (for the startup probes and the listing)."""
    if provider is None:
        return []
    out = [provider]
    attrs = getattr(provider, "__dict__", {})
    for key in ("providers", "_inner", "inner"):
        inner = attrs.get(key)
        for p in inner if isinstance(inner, list) else [] if inner is None else [inner]:
            out += providers_in(p)
    return out


def primary_provider(provider: AIProvider) -> AIProvider:
    """The first real provider of a chain, under any metering or gate wrappers."""
    while True:
        attrs = getattr(provider, "__dict__", {})
        inner = (attrs.get("providers") or [None])[0] or attrs.get("_inner") or attrs.get("inner")
        if inner is None:
            return provider
        provider = inner
