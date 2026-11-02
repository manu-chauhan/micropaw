"""Shell command execution tool with security blocking."""

from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path
from typing import Any

from micropaw.tools.base import Tool, ToolContext

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
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE", re.IGNORECASE)


def _child_env() -> dict[str, str]:
    """The current environment minus API keys, tokens and other secrets."""
    return {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}


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

    def needs_approval(self, args: dict[str, Any], context: ToolContext) -> str | None:
        """Every host command asks (confirm_dangerous=False: only after outside content was read)."""
        command = args.get("command", "")
        if not context.allow_shell or len(command) > MAX_COMMAND or any(p.search(command) for p in _BLOCKED_RE):
            return None  # execute() refuses these anyway
        if not context.confirm_dangerous and not context.marked:
            return None
        risky = any(p.search(command) for p in _DANGEROUS_RE)
        warning = "Careful: this can delete or change things.\n" if risky else ""
        return f"{warning}Run on this computer (in {context.workspace or 'the current folder'}):\n{command}"

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if not context.allow_shell:
            return "Error: Shell execution is not enabled. Ask the user to enable it in settings."

        command = args["command"]
        # Too long to show the owner in full in an approval, so never run it
        if len(command) > MAX_COMMAND:
            return f"Error: Command too long ({len(command):,} chars, max {MAX_COMMAND:,})."
        if any(pat.search(command) for pat in _BLOCKED_RE):
            return "Error: This command has been blocked for safety reasons."

        cwd = None
        if context.workspace:
            cwd = Path(context.workspace).expanduser()
            cwd.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_child_env(), cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)

            output = stdout.decode("utf-8", errors="replace").strip()
            errors = stderr.decode("utf-8", errors="replace").strip()

            result = output
            if errors:
                result += f"\n\nSTDERR:\n{errors}" if result else errors

            if not result:
                result = "(command completed with no output)"
            elif len(result) > MAX_OUTPUT:
                result = result[:MAX_OUTPUT] + f"\n\n... (truncated, {len(result)} total chars)"

            if proc.returncode != 0:
                result = f"Exit code {proc.returncode}:\n{result}"

            return result

        except TimeoutError:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (TimeoutError, ProcessLookupError):
                # Process didn't die from SIGTERM — force kill
                try:
                    os.kill(proc.pid, signal.SIGKILL)
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
            return f"Error: Command timed out after {TIMEOUT} seconds."
        except Exception as e:
            return f"Error executing command: {e}"
