from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..config import RelayConfig
from ..helpers.lark import interactive_lark_onboarding
from ..helpers.teams import interactive_teams_onboarding

app = typer.Typer(
    name="agno-harness",
    help="Enterprise-level agent scaffolding CLI (Agno Runtime + Relay)",
    no_args_is_help=True,
)
console = Console()

lark_app = typer.Typer(name="lark", help="Lark (Feishu) channel tools and onboarding wizard")
teams_app = typer.Typer(name="teams", help="Microsoft Teams channel tools and onboarding wizard")
app.add_typer(lark_app, name="lark")
app.add_typer(teams_app, name="teams")


@lark_app.command("onboard")
def lark_onboard_cmd(
    save: bool = typer.Option(True, "--save/--no-save", help="Save generated keys into .env file"),
    env_file: str = typer.Option(".env", "--env-file", help="Target .env file path"),
    prompt: bool = typer.Option(
        True, "--prompt/--no-prompt", help="Prompt for user credential input"
    ),
    manual: bool = typer.Option(
        False,
        "--manual",
        help="Manually enter existing App ID and Secret instead of scan-to-create flow",
    ),
    domain: str = typer.Option(
        "feishu", "--domain", help="Platform domain: 'feishu' (default) or 'lark'"
    ),
) -> None:
    """Interactive Lark (Feishu) bot creation and QR scan wizard."""
    interactive_lark_onboarding(
        prompt_input=prompt,
        save_to_env=save,
        env_file=env_file,
        manual=manual,
        domain=domain,
    )


@teams_app.command("onboard")
def teams_onboard_cmd(
    save: bool = typer.Option(True, "--save/--no-save", help="Save generated keys into .env file"),
    env_file: str = typer.Option(".env", "--env-file", help="Target .env file path"),
    prompt: bool = typer.Option(
        True, "--prompt/--no-prompt", help="Prompt for user credential input"
    ),
) -> None:
    """Interactive Microsoft Teams bot creation wizard."""
    interactive_teams_onboarding(prompt_input=prompt, save_to_env=save, env_file=env_file)


@app.command("init")
def init_project(
    project_dir: str = typer.Argument(".", help="Target directory for new agent project"),
) -> None:
    """Scaffold a new agno-harness project with multi-channel support."""
    target = Path(project_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    agent_py = target / "agent.py"
    if not agent_py.exists():
        agent_py.write_text(
            '''"""AGNO Agent entrypoint with agno-harness multi-channel gateway."""

import os
from dotenv import load_dotenv
from agno.agent import Agent
from agno_harness import AgentRuntime, RelayApp, CLIChannel, LarkChannel, TeamsChannel, WebChannel

load_dotenv()

# 1. Define your standard AGNO agent
agent = Agent(
    name="Enterprise Assistant",
    instructions="You are an enterprise AI assistant.",
)

# 2. Drive with AgentRuntime (strict & standardized AG-UI execution kernel)
runtime = AgentRuntime(agent=agent)

# 3. Mount onto agno-harness multi-channel gateway
app = RelayApp(runtime=runtime)

# Add CLI for fast local testing
app.add_channel(CLIChannel())

# Add Lark channel if credentials are configured
if os.getenv("AGNO_HARNESS_LARK_APP_ID") and os.getenv("AGNO_HARNESS_LARK_APP_SECRET"):
    app.add_channel(
        LarkChannel(
            app_id=os.environ["AGNO_HARNESS_LARK_APP_ID"],
            app_secret=os.environ["AGNO_HARNESS_LARK_APP_SECRET"],
            use_websocket=True,
        )
    )

# Add Teams channel if configured
if os.getenv("AGNO_HARNESS_TEAMS_APP_ID"):
    app.add_channel(
        TeamsChannel(
            bot_app_id=os.environ["AGNO_HARNESS_TEAMS_APP_ID"],
            bot_app_password=os.getenv("AGNO_HARNESS_TEAMS_APP_PASSWORD"),
        )
    )

if __name__ == "__main__":
    app.serve()
''',
            encoding="utf-8",
        )

    env_file = target / ".env.example"
    if not env_file.exists():
        env_file.write_text(
            """# agno-harness configuration
AGNO_HARNESS_LLM_BASE_URL=
AGNO_HARNESS_LLM_API_KEY=
AGNO_HARNESS_LLM_MODEL=gpt-4o

AGNO_HARNESS_TEAMS_APP_ID=
AGNO_HARNESS_TEAMS_APP_PASSWORD=
AGNO_HARNESS_TEAMS_TENANT_ID=

AGNO_HARNESS_LARK_APP_ID=
AGNO_HARNESS_LARK_APP_SECRET=
AGNO_HARNESS_LARK_VERIFICATION_TOKEN=
AGNO_HARNESS_LARK_ENCRYPT_KEY=
AGNO_HARNESS_LARK_USE_WEBSOCKET=true

AGNO_HARNESS_DATABASE_URL=sqlite+aiosqlite:///messages.db
AGNO_HARNESS_TIMEZONE=
AGNO_HARNESS_LOG_LEVEL=INFO
""",
            encoding="utf-8",
        )

    console.print(
        Panel(
            f"[bold green]✓ Scaffolding complete at {target}[/bold green]\n\n"
            f"Next steps:\n"
            f"  1. [cyan]cp .env.example .env[/cyan]\n"
            f"  2. [cyan]python agent.py[/cyan]",
            title="agno-harness init",
            border_style="green",
        )
    )


@app.command("status")
def status_check() -> None:
    """Inspect environment configuration and channel readiness."""
    table = Table(title="agno-harness Channel & Runtime Status")
    table.add_column("Channel / Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    # CLI
    table.add_row("CLI", "[green]Ready[/green]", "Rich interactive terminal active")

    # Web
    table.add_row("Web (AG-UI)", "[green]Ready[/green]", "FastAPI SSE endpoint available")

    # Teams
    teams_id = RelayConfig.teams_app_id()
    if teams_id:
        table.add_row("Teams", "[green]Configured[/green]", f"App ID: {teams_id[:8]}...")
    else:
        table.add_row(
            "Teams", "[yellow]Not Configured[/yellow]", "Run `agno-harness teams onboard`"
        )

    # Lark
    lark_id = RelayConfig.lark_app_id()
    if lark_id:
        table.add_row("Lark (Feishu)", "[green]Configured[/green]", f"App ID: {lark_id[:8]}...")
    else:
        table.add_row(
            "Lark (Feishu)", "[yellow]Not Configured[/yellow]", "Run `agno-harness lark onboard`"
        )

    console.print(table)


@app.command("onboard")
def onboard_command(
    channel: str = typer.Option("lark", "--channel", "-c", help="Channel: 'lark' or 'teams'"),
    manual: bool = typer.Option(
        False, "--manual", help="Manual entry mode instead of QR scan-to-create"
    ),
    domain: str = typer.Option(
        "feishu", "--domain", help="Platform domain: 'feishu' (default) or 'lark'"
    ),
    env_file: str = typer.Option(".env", "--env-file", help="Target .env file path"),
) -> None:
    """One-click onboarding wizard for messaging channels (default: Feishu QR scan-to-create)."""
    ch = channel.lower()
    if ch in ("lark", "feishu"):
        interactive_lark_onboarding(
            prompt_input=True,
            save_to_env=True,
            env_file=env_file,
            manual=manual,
            domain=domain,
        )
    elif ch in ("teams", "ms-teams"):
        interactive_teams_onboarding(prompt_input=True, save_to_env=True, env_file=env_file)
    else:
        console.print(f"[red]Unknown channel: {channel}. Choose 'lark' or 'teams'.[/red]")


@app.command("register")
def register_wizard(
    channel: str = typer.Argument(..., help="Channel to configure: 'teams' or 'lark'"),
) -> None:
    """Interactive guide for registering a bot in Teams or Lark."""
    ch = channel.lower()
    if ch in ("teams", "ms-teams"):
        interactive_teams_onboarding(prompt_input=True, save_to_env=True)
    elif ch in ("lark", "feishu"):
        interactive_lark_onboarding(prompt_input=True, save_to_env=True)
    else:
        console.print(f"[red]Unknown channel: {channel}. Choose 'teams' or 'lark'.[/red]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
