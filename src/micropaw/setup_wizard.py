"""Interactive setup wizard for MicroPaw."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from micropaw.config import Config, save_config

console = Console()


def run_setup() -> None:
    """Run the interactive setup wizard using questionary."""
    try:
        import questionary
    except ImportError:
        console.print("[red]questionary not installed. Install with: pip install questionary[/red]")
        return

    console.print(Panel(
        "[bold cyan]MicroPaw Setup Wizard[/bold cyan]\n"
        "Let's configure your AI assistant.",
        border_style="cyan",
    ))

    config = Config()

    # Provider selection
    provider = questionary.select(
        "Which AI provider do you want to use?",
        choices=[
            questionary.Choice("Claude (Anthropic)", value="claude"),
            questionary.Choice("OpenAI (GPT-4o)", value="openai"),
            questionary.Choice("Gemini (Google)", value="gemini"),
            questionary.Choice("Ollama (Local)", value="ollama"),
        ],
    ).ask()

    if provider is None:
        return
    config.provider = provider

    # API key
    if provider in ("claude", "openai", "gemini"):
        key_prompts = {
            "claude": "Enter your Anthropic API key:",
            "openai": "Enter your OpenAI API key:",
            "gemini": "Enter your Google Gemini API key:",
        }
        api_key = questionary.password(key_prompts[provider]).ask()
        if api_key:
            getattr(config.provider_config, provider).api_key = api_key
    elif provider == "ollama":
        host = questionary.text(
            "Ollama host:", default="http://localhost:11434"
        ).ask()
        if host:
            config.provider_config.ollama.host = host

        # Validate Ollama is running and let user pick a model
        from micropaw.connector import validate_ollama
        console.print("[dim]Checking Ollama...[/dim]")
        ok, detail = validate_ollama(host or "http://localhost:11434")
        if ok:
            console.print(f"[green]Ollama is running: {detail}[/green]")
            # Fetch model list for selection
            try:
                import httpx
                resp = httpx.get(f"{(host or 'http://localhost:11434').rstrip('/')}/api/tags", timeout=5)
                models = [m.get("name", "") for m in resp.json().get("models", []) if m.get("name")]
                if models:
                    model = questionary.select(
                        "Which model do you want to use?",
                        choices=models,
                        default=models[0],
                    ).ask()
                    if model:
                        config.provider_config.ollama.model = model
                else:
                    console.print("[yellow]No models found. Pull one with: ollama pull llama3.1[/yellow]")
            except Exception:
                pass
        else:
            console.print(f"[yellow]Warning: {detail}[/yellow]")
            console.print("[dim]You can start Ollama later. The default model is llama3.1.[/dim]")

    # Persona
    name = questionary.text(
        "What should your assistant be called?", default="MicroPaw"
    ).ask()
    if name:
        config.persona.name = name

    location = questionary.text(
        "Your location? (optional, helps with 'near me' queries, e.g. 'NYC' or '94102')",
        default="",
    ).ask()
    if location and location.strip():
        config.persona.location = location.strip()

    # Permissions
    allow_shell = questionary.confirm(
        "Allow shell command execution?", default=False
    ).ask()
    config.permissions.allow_shell = allow_shell or False

    allow_write = questionary.confirm(
        "Allow file writing?", default=False
    ).ask()
    config.permissions.allow_file_write = allow_write or False

    # Embeddings (for long-term memory with vector search)
    enable_embeddings = questionary.confirm(
        "Enable long-term memory with vector search?", default=False
    ).ask()
    if enable_embeddings:
        config.embedding.enabled = True
        if provider == "ollama":
            config.embedding.provider = "ollama"
            config.embedding.model = "nomic-embed-text"
            console.print(
                "[dim]Using Ollama embeddings (nomic-embed-text)."
                " Pull it with: ollama pull nomic-embed-text[/dim]"
            )
        else:
            # Check if fastembed is available
            _has_fe = False
            try:
                import fastembed  # noqa: F401
                _has_fe = True
            except ImportError:
                pass

            local_label = "Local (fastembed/ONNX, no API key)"
            if not _has_fe:
                local_label += " [NOT INSTALLED]"

            emb_choice = questionary.select(
                "Embedding provider:",
                choices=[
                    questionary.Choice(local_label, value="local"),
                    questionary.Choice("OpenAI (text-embedding-3-small)", value="openai"),
                    questionary.Choice("Ollama (nomic-embed-text)", value="ollama"),
                ],
            ).ask()
            if emb_choice:
                config.embedding.provider = emb_choice
                if emb_choice == "local":
                    config.embedding.model = "nomic-ai/nomic-embed-text-v1.5"
                    if not _has_fe:
                        console.print("[yellow]Warning: fastembed not installed. "
                                      "Install with: pip install fastembed[/yellow]")
                        console.print("[dim]Vector search will be disabled until installed.[/dim]")
                elif emb_choice == "openai":
                    config.embedding.model = "text-embedding-3-small"
                elif emb_choice == "ollama":
                    config.embedding.model = "nomic-embed-text"

    # Fallback providers
    use_fallback = questionary.confirm(
        "Set up fallback providers?", default=False
    ).ask()
    if use_fallback:
        available = [p for p in ["claude", "openai", "gemini", "ollama"] if p != provider]
        fallbacks = questionary.checkbox(
            "Select fallback providers (in order of preference):",
            choices=available,
        ).ask()
        if fallbacks:
            config.fallback_providers = fallbacks
            # Prompt for API keys for cloud fallback providers
            for fb in fallbacks:
                if fb == "ollama":
                    host = questionary.text(
                        "Ollama host for fallback:", default="http://localhost:11434"
                    ).ask()
                    if host:
                        config.provider_config.ollama.host = host
                elif fb in ("claude", "openai", "gemini"):
                    existing_key = getattr(config.provider_config, fb).api_key
                    if existing_key:
                        console.print(f"[dim]{fb} API key already set.[/dim]")
                    else:
                        key_labels = {"claude": "Anthropic", "openai": "OpenAI", "gemini": "Google Gemini"}
                        fb_key = questionary.password(
                            f"Enter your {key_labels[fb]} API key for {fb} fallback:"
                        ).ask()
                        if fb_key:
                            getattr(config.provider_config, fb).api_key = fb_key

    # --- Google Services (Gmail + Calendar) ---
    setup_google = questionary.confirm(
        "Configure Google services (Gmail + Calendar)?", default=False
    ).ask()
    if setup_google:
        console.print(Panel(
            "[bold]Google OAuth2 Setup[/bold]\n\n"
            "1. Go to https://console.cloud.google.com/apis/credentials\n"
            "2. Create a project (or select existing)\n"
            "3. Enable the [bold]Gmail API[/bold] and [bold]Google Calendar API[/bold]\n"
            "4. Create OAuth2 credentials (Desktop app type)\n"
            "5. Add [cyan]http://localhost:8089[/cyan] as redirect URI\n"
            "6. Copy the Client ID and Client Secret\n\n"
            "[dim]On first use, say 'authorize' in chat to sign in via browser.[/dim]",
            border_style="blue",
            padding=(1, 2),
        ))
        open_url = questionary.confirm(
            "Open Google Cloud Console in your browser?", default=False
        ).ask()
        if open_url:
            import webbrowser
            webbrowser.open("https://console.cloud.google.com/apis/credentials")
        client_id = questionary.text("OAuth2 Client ID:").ask()
        client_secret = questionary.password("OAuth2 Client Secret:").ask()
        if client_id and client_secret:
            config.google.enabled = True
            config.google.client_id = client_id.strip()
            config.google.client_secret = client_secret.strip()
            services = questionary.checkbox(
                "Which services to enable?",
                choices=[
                    questionary.Choice("Gmail", value="gmail", checked=True),
                    questionary.Choice("Google Calendar", value="calendar", checked=True),
                ],
            ).ask()
            if services:
                config.google.gmail_enabled = "gmail" in services
                config.google.calendar_enabled = "calendar" in services
            console.print("[green]Google services configured. Say 'authorize' in chat on first use.[/green]")

    # Save
    save_config(config)
    console.print("\n[green]Config saved![/green]")

    # Offer channel connection
    connect_channel = questionary.confirm(
        "Connect a messaging channel (Telegram, Discord, etc.)?", default=False
    ).ask()
    if connect_channel:
        from micropaw.connector import connect_channel as do_connect
        while True:
            do_connect(config=config)
            another = questionary.confirm("Connect another channel?", default=False).ask()
            if not another:
                break

    console.print("\n[dim]Run 'micropaw chat' to start chatting![/dim]")
