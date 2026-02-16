"""Channel connector — guided setup for Telegram, Discord, Slack, WhatsApp, WebChat."""

from __future__ import annotations

import secrets

import httpx
from rich.console import Console
from rich.panel import Panel

from microclaw.config import Config, load_config, save_config

console = Console()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_telegram_token(token: str) -> str | None:
    """Validate Telegram bot token via getMe. Returns bot username or None."""
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if data.get("ok"):
            return data["result"].get("username", "unknown")
    except Exception:
        pass
    return None


def _validate_discord_token(token: str) -> str | None:
    """Validate Discord bot token via /users/@me. Returns bot username or None."""
    try:
        resp = httpx.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("username", "unknown")
    except Exception:
        pass
    return None


def _validate_slack_token(bot_token: str) -> str | None:
    """Validate Slack bot token via auth.test. Returns bot name or None."""
    try:
        resp = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("bot_id") or data.get("user", "unknown")
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Per-channel connect flows
# ---------------------------------------------------------------------------

def _connect_telegram(config: Config) -> bool:
    """Guide user through Telegram bot setup."""
    import questionary

    console.print(Panel(
        "[bold]Telegram Bot Setup[/bold]\n\n"
        "1. Open Telegram and search for [cyan]@BotFather[/cyan]\n"
        "2. Send [cyan]/newbot[/cyan] and follow the prompts\n"
        "3. Copy the bot token (looks like [dim]123456:ABC-DEF1234...[/dim])",
        border_style="blue",
        padding=(1, 2),
    ))

    token = questionary.password("Paste your Telegram bot token:").ask()
    if not token or not token.strip():
        return False
    token = token.strip()

    console.print("[dim]Validating token...[/dim]")
    bot_name = _validate_telegram_token(token)
    if bot_name:
        console.print(f"[green]Connected to bot @{bot_name}[/green]")
    else:
        console.print("[yellow]Could not validate token (network issue or invalid token).[/yellow]")
        proceed = questionary.confirm("Save anyway?", default=False).ask()
        if not proceed:
            return False

    # Optional: restrict to specific users
    restrict = questionary.confirm("Restrict to specific Telegram usernames?", default=False).ask()
    allowed = []
    if restrict:
        users_str = questionary.text("Comma-separated usernames (without @):").ask()
        if users_str:
            allowed = [u.strip().lstrip("@") for u in users_str.split(",") if u.strip()]

    config.channels.telegram.enabled = True
    config.channels.telegram.token = token
    config.channels.telegram.allowed_users = allowed
    save_config(config)
    console.print("[green]Telegram channel saved and enabled.[/green]")
    return True


def _connect_discord(config: Config) -> bool:
    """Guide user through Discord bot setup."""
    import questionary

    console.print(Panel(
        "[bold]Discord Bot Setup[/bold]\n\n"
        "1. Go to [cyan]https://discord.com/developers/applications[/cyan]\n"
        "2. Click [cyan]New Application[/cyan] → name it → go to [cyan]Bot[/cyan] tab\n"
        "3. Click [cyan]Reset Token[/cyan] → copy the token\n"
        "4. Under [cyan]Privileged Gateway Intents[/cyan], enable [cyan]Message Content Intent[/cyan]\n"
        "5. Use the OAuth2 URL Generator to invite the bot to your server\n"
        "   (scopes: bot; permissions: Send Messages, Read Messages)",
        border_style="blue",
        padding=(1, 2),
    ))

    token = questionary.password("Paste your Discord bot token:").ask()
    if not token or not token.strip():
        return False
    token = token.strip()

    console.print("[dim]Validating token...[/dim]")
    bot_name = _validate_discord_token(token)
    if bot_name:
        console.print(f"[green]Connected to bot {bot_name}[/green]")
    else:
        console.print("[yellow]Could not validate token (network issue or invalid token).[/yellow]")
        proceed = questionary.confirm("Save anyway?", default=False).ask()
        if not proceed:
            return False

    restrict = questionary.confirm("Restrict to specific Discord user IDs?", default=False).ask()
    allowed = []
    if restrict:
        users_str = questionary.text("Comma-separated user IDs:").ask()
        if users_str:
            allowed = [u.strip() for u in users_str.split(",") if u.strip()]

    config.channels.discord.enabled = True
    config.channels.discord.token = token
    config.channels.discord.allowed_users = allowed
    save_config(config)
    console.print("[green]Discord channel saved and enabled.[/green]")
    return True


def _connect_slack(config: Config) -> bool:
    """Guide user through Slack bot setup."""
    import questionary

    console.print(Panel(
        "[bold]Slack Bot Setup[/bold]\n\n"
        "1. Go to [cyan]https://api.slack.com/apps[/cyan] → [cyan]Create New App[/cyan]\n"
        "2. Enable [cyan]Socket Mode[/cyan] → generate an App-Level Token\n"
        "   (scope: [dim]connections:write[/dim])\n"
        "3. Go to [cyan]OAuth & Permissions[/cyan] → add bot scopes:\n"
        "   [dim]chat:write, app_mentions:read, im:history, im:read[/dim]\n"
        "4. Install the app to your workspace → copy the Bot Token\n"
        "5. Under [cyan]Basic Information[/cyan], find the Signing Secret",
        border_style="blue",
        padding=(1, 2),
    ))

    bot_token = questionary.password("Bot User OAuth Token (xoxb-...):").ask()
    if not bot_token or not bot_token.strip():
        return False
    bot_token = bot_token.strip()

    app_token = questionary.password("App-Level Token (xapp-...):").ask()
    if not app_token or not app_token.strip():
        return False
    app_token = app_token.strip()

    signing_secret = questionary.password("Signing Secret:").ask()
    if not signing_secret:
        signing_secret = ""
    signing_secret = signing_secret.strip()

    console.print("[dim]Validating bot token...[/dim]")
    bot_name = _validate_slack_token(bot_token)
    if bot_name:
        console.print(f"[green]Connected to Slack bot ({bot_name})[/green]")
    else:
        console.print("[yellow]Could not validate token.[/yellow]")
        proceed = questionary.confirm("Save anyway?", default=False).ask()
        if not proceed:
            return False

    config.channels.slack.enabled = True
    config.channels.slack.bot_token = bot_token
    config.channels.slack.app_token = app_token
    config.channels.slack.signing_secret = signing_secret
    save_config(config)
    console.print("[green]Slack channel saved and enabled.[/green]")
    return True


def _connect_whatsapp(config: Config) -> bool:
    """Guide user through WhatsApp Cloud API setup."""
    import questionary

    console.print(Panel(
        "[bold]WhatsApp Business API Setup[/bold]\n\n"
        "1. Go to [cyan]https://developers.facebook.com[/cyan]\n"
        "2. Create an app → add [cyan]WhatsApp[/cyan] product\n"
        "3. Under WhatsApp → [cyan]API Setup[/cyan]:\n"
        "   • Copy the temporary access token (or generate a permanent one)\n"
        "   • Copy the Phone Number ID\n"
        "4. Set up a webhook URL pointing to your server\n"
        "   (MicroClaw listens on port [cyan]8080[/cyan] by default)\n"
        "5. Choose a verify token (any string you pick)",
        border_style="blue",
        padding=(1, 2),
    ))

    token = questionary.password("WhatsApp Access Token:").ask()
    if not token or not token.strip():
        return False
    token = token.strip()

    phone_id = questionary.text("Phone Number ID:").ask()
    if not phone_id or not phone_id.strip():
        return False
    phone_id = phone_id.strip()

    verify_token = questionary.text(
        "Webhook Verify Token (any string):",
        default=secrets.token_urlsafe(16),
    ).ask()
    if not verify_token:
        verify_token = secrets.token_urlsafe(16)
    verify_token = verify_token.strip()

    app_secret = questionary.password("App Secret (optional, for signature verification):").ask()
    app_secret = (app_secret or "").strip()

    config.channels.whatsapp.enabled = True
    config.channels.whatsapp.token = token
    config.channels.whatsapp.phone_id = phone_id
    config.channels.whatsapp.verify_token = verify_token
    config.channels.whatsapp.app_secret = app_secret
    save_config(config)
    console.print("[green]WhatsApp channel saved and enabled.[/green]")
    console.print(f"[dim]Webhook verify token: {verify_token}[/dim]")
    return True


def _connect_webchat(config: Config) -> bool:
    """Guide user through WebChat (WebSocket) setup."""
    import questionary

    console.print(Panel(
        "[bold]WebChat (WebSocket) Setup[/bold]\n\n"
        "WebChat runs a WebSocket server that any web frontend can connect to.\n"
        "No external service needed — it runs locally on your machine.",
        border_style="blue",
        padding=(1, 2),
    ))

    host = questionary.text("Host to bind:", default="127.0.0.1").ask()
    port = questionary.text("Port:", default="8765").ask()

    gen_token = questionary.confirm("Generate an auth token?", default=True).ask()
    auth_token = ""
    if gen_token:
        auth_token = secrets.token_urlsafe(32)
        console.print(f"[dim]Auth token: {auth_token}[/dim]")

    config.channels.webchat.enabled = True
    config.channels.webchat.host = (host or "127.0.0.1").strip()
    config.channels.webchat.port = int(port) if port else 8765
    config.channels.webchat.auth_token = auth_token
    save_config(config)
    console.print("[green]WebChat channel saved and enabled.[/green]")
    return True


# ---------------------------------------------------------------------------
# Main connect flow (used by both `connect` command and setup wizard)
# ---------------------------------------------------------------------------

CHANNEL_CHOICES = [
    ("Telegram", "telegram"),
    ("Discord", "discord"),
    ("Slack", "slack"),
    ("WhatsApp", "whatsapp"),
    ("WebChat (WebSocket)", "webchat"),
]

_CONNECT_FUNCS = {
    "telegram": _connect_telegram,
    "discord": _connect_discord,
    "slack": _connect_slack,
    "whatsapp": _connect_whatsapp,
    "webchat": _connect_webchat,
}


def connect_channel(channel_name: str | None = None, config: Config | None = None) -> bool:
    """Run the connect flow for a channel. If channel_name is None, prompts user to pick."""
    import questionary

    if config is None:
        config = load_config()

    if channel_name is None:
        choice = questionary.select(
            "Which channel do you want to connect?",
            choices=[questionary.Choice(label, value=value) for label, value in CHANNEL_CHOICES],
        ).ask()
        if choice is None:
            return False
        channel_name = choice

    func = _CONNECT_FUNCS.get(channel_name)
    if not func:
        console.print(f"[red]Unknown channel: {channel_name}[/red]")
        return False

    return func(config)
