"""Secret redaction: exact values MicroPaw knows, then regexes for common key formats."""

from __future__ import annotations

import os
import re
from typing import Any

# Names that usually hold credentials (env vars and config fields)
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE", re.IGNORECASE)
MIN_LENGTH = 8

# (name, quick substring check, regex). The group named "s" or group 1 is replaced.
# All from the gitleaks default config (research/10 section 6) except telegram-bot-token, which is ours.
_Q = r"(?:[\x60'\"\s;]|\\[nr]|$)"
_PATTERNS: list[tuple[str, tuple[str, ...], re.Pattern[str]]] = [(n, h, re.compile(p)) for n, h, p in [
    ("openai-api-key", ("sk-",),
     r"\b(sk-(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{74}|[A-Za-z0-9_-]{58})T3BlbkFJ(?:[A-Za-z0-9_-]{74}|"
     r"[A-Za-z0-9_-]{58})\b|sk-[a-zA-Z0-9]{20}T3BlbkFJ[a-zA-Z0-9]{20})" + _Q),
    ("anthropic-api-key", ("sk-ant-",), r"\b(sk-ant-api03-[a-zA-Z0-9_\-]{93}AA)" + _Q),
    ("anthropic-admin-api-key", ("sk-ant-",), r"\b(sk-ant-admin01-[a-zA-Z0-9_\-]{93}AA)" + _Q),
    ("aws-access-token", ("A3T", "AKIA", "ASIA", "ABIA", "ACCA"),
     r"\b((?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16})\b"),
    ("github-pat", ("ghp_",), r"(ghp_[0-9a-zA-Z]{36})"),
    ("github-fine-grained-pat", ("github_pat_",), r"(github_pat_\w{82})"),
    ("github-oauth", ("gho_",), r"(gho_[0-9a-zA-Z]{36})"),
    ("slack-bot-token", ("xoxb-",), r"(xoxb-[0-9]{10,13}-[0-9]{10,13}[a-zA-Z0-9-]*)"),
    ("slack-user-token", ("xoxp-", "xoxe-"), r"(xox[pe](?:-[0-9]{10,13}){3}-[a-zA-Z0-9-]{28,34})"),
    ("gcp-api-key", ("AIza",), r"\b(AIza[\w-]{35})" + _Q),
    ("private-key", ("PRIVATE KEY", "private key"),
     r"(?i)(-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----[\s\S-]{64,}?KEY(?: BLOCK)?-----)"),
    ("jwt", ("ey",), r"\b(ey[a-zA-Z0-9]{17,}\.ey[a-zA-Z0-9\/\\_-]{17,}\.(?:[a-zA-Z0-9\/\\_-]{10,}={0,2})?)" + _Q),
    ("telegram-bot-token", (":",), r"\b(\d{8,10}:[A-Za-z0-9_-]{35})\b"),
]]

_known: dict[str, str] = {}     # name -> value, from set_known_secrets and add_known_secret
_exact: list[tuple[str, str]] = []  # (value, placeholder), longest first; None until built
_built = False


def _rebuild() -> None:
    global _built
    values: dict[str, str] = {}
    for k, v in os.environ.items():
        if SECRET_NAME.search(k) and "AUTHOR" not in k.upper() and len(v) >= MIN_LENGTH:  # GIT_AUTHOR_NAME is no secret
            values[v] = k
    for name, v in _known.items():
        if len(v) >= MIN_LENGTH:
            values[v] = name
    _exact[:] = [(v, f"[redacted:{n}]") for v, n in sorted(values.items(), key=lambda kv: -len(kv[0]))]
    _built = True


def set_known_secrets(values: dict[str, str]) -> None:
    """Replace the registered secrets (name -> value) and rescan the environment."""
    _known.clear()
    _known.update(values)
    _rebuild()


def add_known_secret(name: str, value: str) -> None:
    """Register one more value, e.g. a key read from an env var named in the config."""
    _known[name] = value
    _rebuild()


def config_secrets(config: Any) -> dict[str, str]:
    """Every secret in a Config: string fields with secret-like names, and all mcp server env values."""
    found: dict[str, str] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, str) and node and SECRET_NAME.search(path.rsplit(".", 1)[-1]) \
                and not path.endswith("_env"):  # api_key_env holds the NAME of a variable, not a secret
            found[path] = node

    data = config.model_dump()
    servers = data.pop("mcp_servers", {})
    walk(data, "")
    for name, server in servers.items():
        for var, value in server.get("env", {}).items():
            found[f"mcp.{name}.{var}"] = value
    return found


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def secret_env_names(config: Any) -> set[str]:
    """Env var names the config points at for secrets: provider api_key_env and ${NAME} in MCP headers.
    Names like GPUBOX_PASS do not look secret, so they are listed here instead of guessed."""
    names = {p.api_key_env for p in config.providers.values() if p.api_key_env}
    for server in config.mcp_servers.values():
        for value in server.headers.values():
            names.update(_ENV_REF.findall(value))
    return names


def redact(text: str) -> str:
    """Replace known secret values and common key formats with [redacted:<name>]."""
    if not text:
        return text
    if not _built:
        _rebuild()
    for value, placeholder in _exact:
        if value in text:
            text = text.replace(value, placeholder)
    for name, hints, rx in _PATTERNS:
        if any(h in text for h in hints):
            text = rx.sub(lambda m, n=name: _sub(m, n), text)
    return text


def _sub(m: re.Match[str], name: str) -> str:
    whole = m.group(0)
    secret = m.group(1)
    return whole.replace(secret, f"[redacted:{name}]", 1)


def redact_obj(value: Any) -> Any:
    """redact() applied to every string inside nested dicts and lists."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
