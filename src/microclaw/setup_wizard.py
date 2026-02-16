"""Interactive setup wizard for MicroClaw."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from microclaw.config import Config, save_config

console = Console()


def run_setup() -> None:
    """Run the interactive setup wizard using questionary."""
    try:
        import questionary
    except ImportError:
        console.print("[red]questionary not installed. Install with: pip install questionary[/red]")
        return

    console.print(Panel(
        "[bold cyan]MicroClaw Setup Wizard[/bold cyan]\n"
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
            if provider == "claude":
                config.provider_config.claude.api_key = api_key
            elif provider == "openai":
                config.provider_config.openai.api_key = api_key
            elif provider == "gemini":
                config.provider_config.gemini.api_key = api_key
    elif provider == "ollama":
        host = questionary.text(
            "Ollama host:", default="http://localhost:11434"
        ).ask()
        if host:
            config.provider_config.ollama.host = host

        # Validate Ollama is running and let user pick a model
        from microclaw.connector import validate_ollama
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
        "What should your assistant be called?", default="MicroClaw"
    ).ask()
    if name:
        config.persona.name = name

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
            console.print("[dim]Using Ollama embeddings (nomic-embed-text). Pull it with: ollama pull nomic-embed-text[/dim]")
        else:
            emb_choice = questionary.select(
                "Embedding provider:",
                choices=[
                    questionary.Choice("Local (sentence-transformers, no API key)", value="local"),
                    questionary.Choice("OpenAI (text-embedding-3-small)", value="openai"),
                    questionary.Choice("Ollama (nomic-embed-text)", value="ollama"),
                ],
            ).ask()
            if emb_choice:
                config.embedding.provider = emb_choice
                if emb_choice == "local":
                    config.embedding.model = "BAAI/bge-small-en-v1.5"
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
                        f"Ollama host for fallback:", default="http://localhost:11434"
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
                            if fb == "claude":
                                config.provider_config.claude.api_key = fb_key
                            elif fb == "openai":
                                config.provider_config.openai.api_key = fb_key
                            elif fb == "gemini":
                                config.provider_config.gemini.api_key = fb_key

    # Save
    save_config(config)
    console.print(f"\n[green]Config saved![/green]")

    # Offer channel connection
    connect_channel = questionary.confirm(
        "Connect a messaging channel (Telegram, Discord, etc.)?", default=False
    ).ask()
    if connect_channel:
        from microclaw.connector import connect_channel as do_connect
        while True:
            do_connect(config=config)
            another = questionary.confirm("Connect another channel?", default=False).ask()
            if not another:
                break

    console.print("\n[dim]Run 'microclaw chat' to start chatting![/dim]")
