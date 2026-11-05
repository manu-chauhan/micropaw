"""Shell command execution tool with security blocking."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

from micropaw.tools import sandbox
from micropaw.tools.base import Tool, ToolContext
from micropaw.utils.redact import SECRET_NAME, redact

TIMEOUT = 30
MAX_OUTPUT = 10_000
MAX_COMMAND = 2_000

# Commands flagged as risky in the approval prompt
_DANGEROUS_PATTERNS = [
    r"\brm\s+", r"\bmkdir\s+-p\s+/", r"\bchmod\b", r"\bchown\b",
    r"\bkill\b", r"\bpkill\b", r"\bsudo\b",
    r"\bpip\s+install\b", r"\bnpm\s+install\b", r"\bapt\s+install\b",
    r"\bgit\s+push\b", r"\bgit\s+reset\b", r"\bgit\s+checkout\b",
    r"\bcurl\s+.*-[dX]", r"\bwget\b",
    r">\s*/", r">>\s*/",  # redirect to absolute path
    # Windows (cmd and PowerShell)
    r"\bRemove-Item\b.*-Recurse", r"\bdel\s+.*/s\b", r"\bformat\s+[a-z]:", r"\b(rd|rmdir)\s+.*/s\b",
    r"\breg\s+delete\b", r"\bSet-ExecutionPolicy\b",
    r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod|curl|wget)\b.*\|\s*(iex|Invoke-Expression)\b",
]
_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]

BLOCKED_PATTERNS = [
    # Destructive file operations
    r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?(-[a-zA-Z]*r[a-zA-Z]*\s+)?/(\s|$|\*)",
    r"rm\s+(-[a-zA-Z]*r[a-zA-Z]*\s+)?(-[a-zA-Z]*f[a-zA-Z]*\s+)?/(\s|$|\*)",
    r"find\s+.*-delete",
    r"find\s+.*-exec\s+rm\b",
    # Disk destruction
    r"mkfs\b", r"dd\s+.*of=/dev/[a-z]", r">\s*/dev/sd[a-z]", r"shred\b", r"wipefs\b",
    # Fork bomb
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;",
    # Permission escalation
    r"chmod\s+(-[a-zA-Z]+\s+)?777\s+/", r"chmod\s+(-[a-zA-Z]+\s+)?\+s\b",
    # Reverse shells
    r"/dev/tcp/", r"/dev/udp/",
    r"nc\s+(-[a-zA-Z]*e[a-zA-Z]*\s+|.*-e\s+)/bin/",
    r"ncat\s+.*--exec", r"socat\s+.*exec:",
    r"bash\s+-i\s+.*>&\s*/dev/",
    r"python[23]?\s+-c\s+.*socket", r"perl\s+-e\s+.*socket", r"ruby\s+-e\s+.*socket",
    # Data exfiltration
    r"curl\s+.*-d\s+@", r"curl\s+.*--data-binary\s+@", r"wget\s+.*--post-file",
    # Sensitive file access (cat, less, more, head, tail)
    r"(cat|less|more|head|tail)\s+.*\.ssh/",
    r"(cat|less|more|head|tail)\s+.*\.micropaw/",
    r"(cat|less|more|head|tail)\s+.*\.env\b",
    r"(cat|less|more|head|tail)\s+.*/etc/shadow",
    # Code execution
    r"python[23]?\s+-c\s+.*import\s+os", r"python[23]?\s+-c\s+.*subprocess",
    r"python[23]?\s+-c\s+.*exec\(", r"python[23]?\s+-c\s+.*eval\(",
    r"node\s+-e\s+.*child_process", r"ruby\s+-e\s+.*system\(", r"perl\s+-e\s+.*system\(",
    # Login script manipulation
    r">\s*~/?\.\w*(rc|profile|login|logout)\b", r"tee\s+.*~/?\.\w*(rc|profile|login|logout)\b",
    # SSH/cron manipulation
    r">\s*.*\.ssh/authorized_keys", r"tee\s+.*\.ssh/authorized_keys",
    r"crontab\s+-", r">\s*/etc/cron",
    # Download and execute
    r"curl\s+.*\|\s*(ba)?sh", r"wget\s+.*\|\s*(ba)?sh",
    r"curl\s+.*-o\s+.+&&\s*chmod\s+\+x", r"wget\s+.*-O\s+.+&&\s*chmod\s+\+x",
    # Environment variable leaks (API keys live here)
    r"\benv\b", r"\bprintenv\b", r"\bexport\s+-p\b",
    r"echo\s+.*\$[A-Z_]", r"printf\s+.*\$[A-Z_]",
    r"\bset\s*$", r"\bset\s*\|",
    r"\bdeclare\b",  # bash built-in dumps all vars
    r"/proc/(\d+|self)/environ",  # raw environment dump on Linux (any PID)
    r"\bnode\s+-e\b",  # Node.js one-liners can access process.env
    r"__import__",  # Python import bypass (e.g. __import__('os').environ)
    r"\$\(", r"`",  # subshell / backtick execution bypass
    r"base64\b.*\|\s*(ba)?sh",
    r"xargs\s+.*sh\b",
    # Hex/octal decode to shell
    r"xxd\b.*\|\s*(ba)?sh", r"printf\s+.*\\x[0-9a-f].*\|\s*(ba)?sh",
    # Shell builtins that execute arbitrary code
    r"\beval\s+", r"\bsource\s+", r"\bexec\s+",
    # Process substitution
    r"<\(",
]

_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]

# Env var names that usually hold credentials; children never see them
_SECRET_ENV = SECRET_NAME


def _child_env() -> dict[str, str]:
    """The current environment minus API keys, tokens and other secrets."""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


def _powershell() -> str:
    return shutil.which("pwsh") or "powershell"


def shell_dialect() -> str:
    """The language commands are written in on this computer: sh or powershell."""
    return "powershell" if os.name == "nt" else "sh"


class ShellTool(Tool):
    name = "shell"
    marks = True
    description = "Execute a shell command and return its output (stdout + stderr)."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
        },
        "required": ["command"],
    }

    def __init__(self) -> None:
        dialect = "PowerShell" if shell_dialect() == "powershell" else "sh"
        self.description = f"{type(self).description} Commands are written for {dialect}."

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        """Ask before a command runs. Skipped only when confirm_dangerous is off, nothing outside was read,
        and the command runs in a sandbox that may skip it (never sandbox-exec) or on a POSIX host."""
        command = args.get("command", "")
        if not context.allow_shell or len(command) > MAX_COMMAND or any(p.search(command) for p in _BLOCKED_RE):
            return None  # execute() refuses these anyway
        sb = sandbox.resolve()
        if sb.error:
            return None  # execute() refuses
        if not context.confirm_dangerous and not context.marked:
            host_always_asks = not sb.active and (os.name == "nt" or sb.host_asks)
            if sb.may_skip_ask and not host_always_asks:
                return None
        risky = any(p.search(command) for p in _DANGEROUS_RE)
        warning = "Careful: this can delete or change things.\n" if risky else ""
        return f"{sb.label} run in {context.workspace or 'the current folder'}\n{warning}{command}"

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if not context.allow_shell:
            return "Error: Shell execution is not enabled. Ask the user to enable it in settings."

        command = args["command"]
        # Too long to show the owner in full in an approval, so never run it
        if len(command) > MAX_COMMAND:
            return f"Error: Command too long ({len(command):,} chars, max {MAX_COMMAND:,})."
        if any(pat.search(command) for pat in _BLOCKED_RE):
            return "Error: This command has been blocked for safety reasons."

        sb = sandbox.resolve()
        if sb.error:
            return f"Error: {sb.error} Not run."
        cwd = Path(context.workspace).expanduser() if context.workspace else None
        if cwd:
            cwd.mkdir(parents=True, exist_ok=True)
        note = ""
        wrapped = None
        try:
            if sb.active:
                note = await sandbox.prepare(sb)
                wrapped = sandbox.wrap(sb, command, cwd or Path.cwd(), _child_env())
                proc = await asyncio.create_subprocess_exec(
                    *wrapped.argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=wrapped.env, cwd=cwd,
                )
            elif os.name == "nt":
                proc = await asyncio.create_subprocess_exec(
                    _powershell(), "-NoProfile", "-NonInteractive", "-Command", command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_child_env(), cwd=cwd,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_child_env(), cwd=cwd,
                )
        except sandbox.SandboxError as e:
            return f"Error: {e} Not run."
        except Exception as e:
            if sb.active:
                sandbox.mark_failed(sb.kind)
                return f"Sandbox failed: {e}. Not run."
            return f"Error executing command: {e}"

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
            output = stdout.decode("utf-8", errors="replace").strip()
            errors = stderr.decode("utf-8", errors="replace").strip()
            reason = sandbox.failure(sb, proc.returncode, errors)
            if reason and await asyncio.to_thread(sandbox.recheck, sb.kind):
                return f"Sandbox failed: {reason}. Not run."

            result = output
            if errors:
                result += f"\n\nSTDERR:\n{errors}" if result else errors

            # Redact before the cut, so a key split at the cut cannot survive
            result = redact(result)
            if not result:
                result = "(command completed with no output)"
            elif len(result) > MAX_OUTPUT:
                result = result[:MAX_OUTPUT] + f"\n\n... (truncated, {len(result)} total chars)"

            if proc.returncode != 0:
                result = f"Exit code {proc.returncode}:\n{result}"

            return note + result

        except TimeoutError:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (TimeoutError, ProcessLookupError):
                # Process didn't die from SIGTERM; force kill (works on Windows too)
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
            if wrapped and wrapped.cleanup:
                cleanup = await asyncio.create_subprocess_exec(
                    *wrapped.cleanup, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await cleanup.wait()
            return f"Error: Command timed out after {TIMEOUT} seconds."
        except Exception as e:
            return f"Error executing command: {e}"
