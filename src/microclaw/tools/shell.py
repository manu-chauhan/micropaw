"""Shell command execution tool with security blocking."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from microclaw.tools.base import Tool, ToolContext

TIMEOUT = 30
MAX_OUTPUT = 10_000

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
    # Sensitive file access
    r"cat\s+.*\.ssh/", r"cat\s+.*\.microclaw/", r"cat\s+.*\.env\b", r"cat\s+.*/etc/shadow",
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
    r"\$\(", r"`",  # subshell / backtick execution bypass
    r"base64\s+.*\|\s*(ba)?sh", r"base64\s+-d\s+.*\|\s*(ba)?sh",
    r"xargs\s+.*sh\b",
    # Shell builtins that execute arbitrary code
    r"\beval\s+", r"\bsource\s+", r"\bexec\s+",
]

_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]


class ShellTool(Tool):
    name = "shell"
    description = "Execute a shell command and return its output (stdout + stderr)."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to execute"},
        },
        "required": ["command"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if not context.allow_shell:
            return "Error: Shell execution is not enabled. Ask the user to enable it in settings."

        command = args["command"]
        if any(pat.search(command) for pat in _BLOCKED_RE):
            return "Error: This command has been blocked for safety reasons."

        try:
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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

        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return f"Error: Command timed out after {TIMEOUT} seconds."
        except Exception as e:
            return f"Error executing command: {e}"
