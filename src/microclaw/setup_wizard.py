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
