"""Channel connector — guided setup for Telegram, Discord, Slack, WhatsApp, WebChat."""

from __future__ import annotations

import secrets

import httpx
from rich.console import Console
from rich.panel import Panel

from micropaw import net
from micropaw.config import Config, load_config, save_config

console = Console()

VENDOR_NAMES = {"telegram": "Telegram", "discord": "Discord", "slack": "Slack", "whatsapp": "WhatsApp (Meta)"}


# Validation helpers (also used by `micropaw test`)


def _ask_owner_ids(platform: str, current: list[str]) -> list[str]:
    """Ask for the owner's numeric account IDs. Only these accounts get answers."""
    import questionary

    console.print(
        f"[dim]Only your own {platform} accounts can use the bot. Enter your numeric {platform} user ID, "
        f"or leave blank: message the bot once and it replies with your ID, then run "
        f"'paw allow {platform} <id>'.[/dim]"
    )
    raw = questionary.text("Your user ID(s), comma-separated:", default=",".join(current)).ask() or ""
    return [u.strip() for u in raw.split(",") if u.strip()]

def _send(method: str, url: str, timeout: float, **kwargs) -> httpx.Response:
    """One request through the guarded client (the guard and proxy rules of every other connection)."""
    with net.sync_client(url, timeout=timeout) as client:
        return client.request(method, url, **kwargs)


def validate_telegram_token(token: str) -> tuple[bool, str]:
    """Validate Telegram bot token via getMe. Returns (ok, detail)."""
    try:
        resp = _send("GET", f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = resp.json()
        if data.get("ok"):
            username = data["result"].get("username", "unknown")
            return True, f"@{username}"
        return False, data.get("description", "invalid token")
    except httpx.ConnectError:
        return False, "connection failed (network issue)"
    except Exception as e:
        return False, str(e)


def validate_discord_token(token: str) -> tuple[bool, str]:
    """Validate Discord bot token via /users/@me. Returns (ok, detail)."""
    try:
        resp = _send(
            "GET", "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            username = data.get("username", "unknown")
            app_id = data.get("id", "")
            return True, f"{username} (id: {app_id})"
        if resp.status_code == 401:
            return False, "invalid token (401 Unauthorized)"
        return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, "connection failed (network issue)"
    except Exception as e:
        return False, str(e)


def _get_discord_app_id(token: str) -> str | None:
    """Get the Discord application ID from a bot token."""
    try:
        resp = _send(
            "GET", "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("id")
    except Exception:
        pass
    return None


def discord_invite_url(app_id: str) -> str:
    """Generate a Discord bot invite URL with required permissions."""
    # Permissions: Send Messages (2048) + Read Messages/View Channels (1024) + Read Message History (65536)
    permissions = 2048 | 1024 | 65536
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions={permissions}&scope=bot"


def validate_slack_token(bot_token: str) -> tuple[bool, str]:
    """Validate Slack bot token via auth.test. Returns (ok, detail)."""
    try:
        resp = _send(
            "POST", "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            team = data.get("team", "unknown")
            user = data.get("user", "unknown")
            return True, f"{user} in {team}"
        return False, data.get("error", "invalid token")
    except httpx.ConnectError:
        return False, "connection failed (network issue)"
    except Exception as e:
        return False, str(e)


def validate_ollama(host: str) -> tuple[bool, str]:
    """Check if Ollama is reachable."""
    try:
        resp = _send("GET", f"{host.rstrip('/')}/api/tags", timeout=5)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            names = [m.get("name", "") for m in models[:5]]
            return True, f"{len(models)} model(s): {', '.join(names)}" if models else "running (no models pulled)"
        return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        return False, f"cannot connect to {host}"
    except Exception as e:
        return False, str(e)


def validate_provider_key(config: Config) -> tuple[bool, str]:
    """Validate the configured AI provider."""
    provider = config.provider
    if provider == "ollama":
        return validate_ollama(config.provider_config.ollama.host)
    elif provider == "claude":
        key = config.provider_config.claude.api_key
        if not key:
            return False, "no API key configured"
        try:
            resp = _send(
                "POST", "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={
                    "model": config.provider_config.claude.model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                timeout=15,
            )
            if resp.status_code == 200:
                return True, f"{config.provider_config.claude.model}"
            if resp.status_code == 401:
                return False, "invalid API key (401)"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)
    elif provider == "openai":
        key = config.provider_config.openai.api_key
        if not key:
            return False, "no API key configured"
        try:
            resp = _send(
                "GET", "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                return True, f"{config.provider_config.openai.model}"
            if resp.status_code == 401:
                return False, "invalid API key (401)"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)
    elif provider == "gemini":
        key = config.provider_config.gemini.api_key
        if not key:
            return False, "no API key configured"
        try:
            resp = _send(
                "GET", "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key},
                timeout=10,
            )
            if resp.status_code == 200:
                return True, f"{config.provider_config.gemini.model}"
            if resp.status_code in (401, 403):
                return False, "invalid API key"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, str(e)
    return False, f"unknown provider: {provider}"


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
    ok, detail = validate_telegram_token(token)
    if ok:
        console.print(f"[green]Connected to bot {detail}[/green]")
    else:
        console.print(f"[yellow]Validation failed: {detail}[/yellow]")
        proceed = questionary.confirm("Save anyway?", default=False).ask()
        if not proceed:
            return False

    config.channels.telegram.enabled = True
    config.channels.telegram.token = token
    config.channels.telegram.allowed_users = _ask_owner_ids("telegram", config.channels.telegram.allowed_users)
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
        "4. [bold yellow]IMPORTANT:[/bold yellow] Under [cyan]Privileged Gateway Intents[/cyan],\n"
        "   enable [bold]Message Content Intent[/bold] (without this the bot can't read messages!)\n"
        "5. We'll generate the invite link for you after validation",
        border_style="blue",
        padding=(1, 2),
    ))

    token = questionary.password("Paste your Discord bot token:").ask()
    if not token or not token.strip():
        return False
    token = token.strip()

    console.print("[dim]Validating token...[/dim]")
    ok, detail = validate_discord_token(token)
    app_id = None
    if ok:
        console.print(f"[green]Connected to bot {detail}[/green]")
        app_id = _get_discord_app_id(token)
        if app_id:
            invite = discord_invite_url(app_id)
            console.print()
            console.print(Panel(
                f"[bold]Invite your bot to a server:[/bold]\n\n"
                f"[cyan]{invite}[/cyan]\n\n"
                f"Open this link in your browser, select a server, and authorize.",
                border_style="green",
                padding=(1, 2),
            ))
            console.print()
    else:
        console.print(f"[yellow]Validation failed: {detail}[/yellow]")
        proceed = questionary.confirm("Save anyway?", default=False).ask()
        if not proceed:
            return False

    config.channels.discord.enabled = True
    config.channels.discord.token = token
    config.channels.discord.allowed_users = _ask_owner_ids("discord", config.channels.discord.allowed_users)
    save_config(config)
    console.print("[green]Discord channel saved and enabled.[/green]")
    console.print("[dim]Reminder: Make sure Message Content Intent is enabled in the Developer Portal![/dim]")
    return True


def _connect_slack(config: Config) -> bool:
    """Guide user through Slack bot setup."""
    import questionary

    console.print(Panel(
        "[bold]Slack Bot Setup[/bold]\n\n"
        "1. Go to [cyan]https://api.slack.com/apps[/cyan] → [cyan]Create New App[/cyan] (from scratch)\n"
        "2. [bold]Socket Mode[/bold] (left sidebar) → toggle ON → generate App-Level Token\n"
        "   name it anything, scope: [dim]connections:write[/dim] → copy it ([dim]xapp-...[/dim])\n"
        "3. [bold]OAuth & Permissions[/bold] → Bot Token Scopes → add:\n"
        "   [dim]chat:write, app_mentions:read, im:history, im:read, im:write[/dim]\n"
        "4. [bold]Install App[/bold] (left sidebar) → Install to Workspace → copy Bot Token ([dim]xoxb-...[/dim])\n"
        "5. [bold]Event Subscriptions[/bold] → toggle ON → Subscribe to: [dim]message.im[/dim]\n"
        "6. [bold]Basic Information[/bold] → Signing Secret (optional for Socket Mode)",
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

    signing_secret = questionary.password("Signing Secret (optional, press Enter to skip):").ask()
    signing_secret = (signing_secret or "").strip()

    console.print("[dim]Validating bot token...[/dim]")
    ok, detail = validate_slack_token(bot_token)
    if ok:
        console.print(f"[green]Connected to Slack: {detail}[/green]")
    else:
        console.print(f"[yellow]Validation failed: {detail}[/yellow]")
        proceed = questionary.confirm("Save anyway?", default=False).ask()
        if not proceed:
            return False

    config.channels.slack.enabled = True
    config.channels.slack.bot_token = bot_token
    config.channels.slack.app_token = app_token
    config.channels.slack.signing_secret = signing_secret
    config.channels.slack.allowed_users = _ask_owner_ids("slack", config.channels.slack.allowed_users)
    save_config(config)
    console.print("[green]Slack channel saved and enabled.[/green]")
    return True


def _connect_whatsapp(config: Config) -> bool:
    """Guide user through WhatsApp Cloud API setup."""
    import questionary

    console.print(Panel(
        "[bold]WhatsApp Business API Setup[/bold]\n\n"
        "[bold yellow]Note:[/bold yellow] WhatsApp requires a Meta Business account and a\n"
        "publicly accessible server for webhooks. This is the most complex\n"
        "channel to set up.\n\n"
        "1. Go to [cyan]https://developers.facebook.com[/cyan]\n"
        "2. Create an app → select [cyan]Business[/cyan] type → add [cyan]WhatsApp[/cyan] product\n"
        "3. Under WhatsApp → [cyan]API Setup[/cyan]:\n"
        "   • Copy the [bold]Temporary Access Token[/bold]\n"
        "     [yellow](expires in 24h — see docs for permanent token)[/yellow]\n"
        "   • Copy the [bold]Phone Number ID[/bold]\n"
        "4. Webhook setup — your server must be reachable from the internet:\n"
        "   • URL: [cyan]https://your-server:8080/webhook[/cyan]\n"
        "   • Or use a tunnel: [cyan]ngrok http 8080[/cyan] / [cyan]cloudflared tunnel[/cyan]\n"
        "5. Subscribe to [cyan]messages[/cyan] webhook field",
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
        "Webhook Verify Token (any string — Meta sends this to verify your webhook):",
        default=secrets.token_urlsafe(16),
    ).ask()
    if not verify_token:
        verify_token = secrets.token_urlsafe(16)
    verify_token = verify_token.strip()

    app_secret = ""
    while not app_secret:
        # Required: without it anyone who reaches the webhook can pose as any sender
        app_secret = (questionary.password("App Secret (Meta app dashboard > Basic):").ask() or "").strip()
        if not app_secret and not questionary.confirm("App Secret is required. Try again?", default=True).ask():
            return False

    config.channels.whatsapp.enabled = True
    config.channels.whatsapp.token = token
    config.channels.whatsapp.phone_id = phone_id
    config.channels.whatsapp.verify_token = verify_token
    config.channels.whatsapp.app_secret = app_secret
    config.channels.whatsapp.allowed_users = _ask_owner_ids("whatsapp", config.channels.whatsapp.allowed_users)
    save_config(config)

    console.print("[green]WhatsApp channel saved and enabled.[/green]")
    console.print()
    console.print(Panel(
        f"[bold]Webhook Configuration[/bold]\n\n"
        f"Callback URL: [cyan]https://YOUR_SERVER:8080/webhook[/cyan]\n"
        f"Verify Token: [cyan]{verify_token}[/cyan]\n\n"
        f"[bold yellow]Important:[/bold yellow]\n"
        f"• The temporary access token expires in [bold]24 hours[/bold]\n"
        f"• For permanent access, create a System User in Meta Business Settings\n"
        f"  and generate a permanent token\n"
        f"• Your server must be publicly accessible (use ngrok or cloudflared\n"
        f"  for local development: [dim]ngrok http 8080[/dim])",
        border_style="yellow",
        padding=(1, 2),
    ))
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
    try:
        config.channels.webchat.port = int(port) if port else 8765
    except ValueError:
        console.print("[yellow]Invalid port, using default 8765.[/yellow]")
        config.channels.webchat.port = 8765
    config.channels.webchat.auth_token = auth_token
    save_config(config)
    console.print("[green]WebChat channel saved and enabled.[/green]")
    return True


def test_channels(config: Config) -> None:
    """Validate all configured channels and provider without starting the gateway."""
    from rich.table import Table

    table = Table(title="MicroPaw Connectivity Test")
    table.add_column("Component", style="cyan")
    table.add_column("Status")
    table.add_column("Detail", style="dim")

    ok_s = "[green]pass[/green]"
    fail_s = "[red]FAIL[/red]"
    skip_s = "[dim]skip[/dim]"

    # Test AI provider
    console.print("[dim]Testing AI provider...[/dim]")
    prov_ok, prov_detail = validate_provider_key(config)
    table.add_row(f"Provider ({config.provider})", ok_s if prov_ok else fail_s, prov_detail)

    # Test Telegram
    tg = config.channels.telegram
    if tg.enabled and tg.token:
        console.print("[dim]Testing Telegram...[/dim]")
        tg_ok, tg_detail = validate_telegram_token(tg.token)
        table.add_row("Telegram", ok_s if tg_ok else fail_s, tg_detail)
    else:
        table.add_row("Telegram", skip_s, "not configured")

    # Test Discord
    dc = config.channels.discord
    if dc.enabled and dc.token:
        console.print("[dim]Testing Discord...[/dim]")
        dc_ok, dc_detail = validate_discord_token(dc.token)
        status = ok_s if dc_ok else fail_s
        table.add_row("Discord", status, dc_detail)
        if dc_ok:
            app_id = _get_discord_app_id(dc.token)
            if app_id:
                table.add_row("  invite URL", "[dim]--[/dim]", discord_invite_url(app_id))
            table.add_row(
                "  [yellow]reminder[/yellow]", "[dim]--[/dim]",
                "Ensure Message Content Intent is ON in Developer Portal",
            )
    else:
        table.add_row("Discord", skip_s, "not configured")

    # Test Slack
    sl = config.channels.slack
    if sl.enabled and sl.bot_token:
        console.print("[dim]Testing Slack...[/dim]")
        sl_ok, sl_detail = validate_slack_token(sl.bot_token)
        table.add_row("Slack", ok_s if sl_ok else fail_s, sl_detail)
    else:
        table.add_row("Slack", skip_s, "not configured")

    # WhatsApp — can't validate token easily, just check it's configured
    wa = config.channels.whatsapp
    if wa.enabled and wa.token:
        table.add_row(
            "WhatsApp", "[yellow]configured[/yellow]",
            "token set (cannot validate without sending a message)",
        )
        table.add_row(
            "  [yellow]warning[/yellow]", "[dim]--[/dim]",
            "Temporary tokens expire in 24h — use a permanent System User token",
        )
        table.add_row("  webhook", "[dim]--[/dim]", "Expects POST at :8080/webhook (verify_token set)")
    else:
        table.add_row("WhatsApp", skip_s, "not configured")

    # WebChat — just local, check config
    wc = config.channels.webchat
    if wc.enabled:
        table.add_row("WebChat", ok_s, f"ws://{wc.host}:{wc.port}/ws (auth: {'set' if wc.auth_token else 'none'})")
    else:
        table.add_row("WebChat", skip_s, "not configured")

    console.print()
    console.print(table)


# Main connect flow (used by both `connect` command and setup wizard)

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

    if channel_name in VENDOR_NAMES:
        console.print(f"[yellow]Every message to and from this bot passes through {VENDOR_NAMES[channel_name]} "
                      "in readable form. Do not use it for anything you want to keep off their servers.[/yellow]")
    return func(config)
