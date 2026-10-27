"""Cron tool — schedule recurring or one-shot tasks via LLM."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from micropaw.tools.base import Tool, ToolContext

if TYPE_CHECKING:
    from micropaw.scheduler import Scheduler

logger = logging.getLogger(__name__)

_scheduler: Scheduler | None = None


def set_scheduler(scheduler: Scheduler) -> None:
    global _scheduler
    _scheduler = scheduler


class CronTool(Tool):
    name = "cron"
    description = (
        "Schedule tasks. Actions: 'add' (create a recurring or one-shot job), "
        "'list' (show all jobs), 'remove' (delete a job by ID)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The action to perform",
                "enum": ["add", "list", "remove"],
            },
            "description": {
                "type": "string",
                "description": "Task description — what to do when the job fires (for add)",
            },
            "interval_seconds": {
                "type": "integer",
                "description": "How often to run, in seconds. E.g. 3600 = every hour (for add)",
            },
            "one_shot": {
                "type": "boolean",
                "description": "If true, run once after interval_seconds then auto-delete (for add)",
            },
            "job_id": {
                "type": "integer",
                "description": "Job ID to remove (for remove)",
            },
        },
        "required": ["action"],
    }

    async def execute(self, args: dict[str, Any], context: ToolContext) -> str:
        if _scheduler is None:
            return "Error: Scheduler not initialized."

        action = args["action"]

        if action == "add":
            description = args.get("description", "")
            interval = args.get("interval_seconds", 0)
            if not description or interval <= 0:
                return "Error: 'add' requires 'description' and 'interval_seconds' > 0."
            one_shot = args.get("one_shot", False)
            if not one_shot and interval < _MIN_RECURRING:
                return f"Error: recurring jobs must be at least {_fmt_interval(_MIN_RECURRING)} apart."
            try:
                # One-shot jobs only deliver a reminder. Recurring jobs run with tools, so the
                # owner must switch them on; otherwise a web page could plant a standing task.
                job_id = await _scheduler.add_job(
                    description, interval, context.user_id, context.platform,
                    context.chat_id, one_shot=one_shot, enabled=one_shot,
                )
            except ValueError as e:
                return f"Error: {e}"
            if one_shot:
                return f"Scheduled reminder #{job_id}: \"{description}\" (in {_fmt_interval(interval)})"
            await _scheduler.notify(
                context.platform, context.chat_id,
                f"New scheduled task #{job_id} is paused until you turn it on:\n"
                f"\"{description}\" every {_fmt_interval(interval)}\n"
                f"To turn it on, reply: /cron enable {job_id}",
            )
            return (f"Saved recurring job #{job_id} (every {_fmt_interval(interval)}), paused. "
                    f"The user has been asked to turn it on with /cron enable {job_id}.")

        elif action == "list":
            jobs = await _scheduler.list_jobs()
            if not jobs:
                return "No scheduled jobs."
            lines = [f"Scheduled jobs ({len(jobs)}):"]
            for j in jobs:
                kind = "once" if j["one_shot"] else f"every {_fmt_interval(j['interval_seconds'])}"
                state = "" if j["enabled"] else ", paused"
                lines.append(f"  #{j['id']}: \"{j['description']}\" ({kind}{state})")
            return "\n".join(lines)

        elif action == "remove":
            job_id = args.get("job_id")
            if not job_id:
                return "Error: 'remove' requires 'job_id'."
            try:
                removed = await _scheduler.remove_job(int(job_id))
            except (ValueError, TypeError):
                return f"Error: Invalid job_id: {job_id!r}"
            return f"Job #{job_id} removed." if removed else f"Job #{job_id} not found."

        return f"Unknown action: {action}"


_MIN_RECURRING = 300  # seconds


def _fmt_interval(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"
