import os
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="agno-relay",
    help="Universal Multi-Channel Production Gateway CLI for AGNO Agents",
    no_args_is_help=True,
)
console = Console()


@app.command("init")
def init_project(
    project_dir: str = typer.Argument(".", help="Target directory for new agent project"),
) -> None:
    """Scaffold a new agno-relay project with multi-channel support."""
    target = Path(project_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    agent_py = target / "agent.py"
    if not agent_py.exists():
        agent_py.write_text(
            '''"""AGNO Agent entrypoint with agno-relay multi-channel gateway."""

import os
from dotenv import load_dotenv
from agno.agent import Agent
from agno_relay import RelayApp, CLIChannel, LarkChannel, TeamsChannel, WebChannel

load_dotenv()

# 1. Define your standard AGNO agent
agent = Agent(
    name="Enterprise Assistant",
    instructions="You are an enterprise AI assistant.",
)

# 2. Mount onto agno-relay
app = RelayApp(agent)

# Add CLI for fast local testing
app.add_channel(CLIChannel())

# Add Lark channel if credentials are configured
if os.getenv("LARK_APP_ID") and os.getenv("LARK_APP_SECRET"):
    app.add_channel(
        LarkChannel(
            app_id=os.environ["LARK_APP_ID"],
            app_secret=os.environ["LARK_APP_SECRET"],
            use_websocket=True,
        )
    )

# Add Teams channel if configured
if os.getenv("TEAMS_APP_ID"):
    app.add_channel(
        TeamsChannel(
            bot_app_id=os.environ["TEAMS_APP_ID"],
            bot_app_password=os.getenv("TEAMS_APP_PASSWORD"),
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
            """# agno-relay configuration
TEAMS_APP_ID=
TEAMS_APP_PASSWORD=
TEAMS_APP_TENANT_ID=

LARK_APP_ID=
LARK_APP_SECRET=
LARK_VERIFICATION_TOKEN=

DATABASE_URL=sqlite+aiosqlite:///messages.db
""",
            encoding="utf-8",
        )

    console.print(
        Panel(
            f"[bold green]✓ Scaffolding complete at {target}[/bold green]\n\n"
            f"Next steps:\n"
            f"  1. [cyan]cp .env.example .env[/cyan]\n"
            f"  2. [cyan]python agent.py[/cyan]",
            title="agno-relay init",
            border_style="green",
        )
    )


@app.command("status")
def status_check() -> None:
    """Inspect environment configuration and channel readiness."""
    table = Table(title="agno-relay Channel & Runtime Status")
    table.add_column("Channel / Component", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    # CLI
    table.add_row("CLI", "[green]Ready[/green]", "Rich interactive terminal active")

    # Web
    table.add_row("Web (AG-UI)", "[green]Ready[/green]", "FastAPI SSE endpoint available")

    # Teams
    teams_id = os.getenv("TEAMS_APP_ID")
    if teams_id:
        table.add_row("Teams", "[green]Configured[/green]", f"App ID: {teams_id[:8]}...")
    else:
        table.add_row("Teams", "[yellow]Not Configured[/yellow]", "Missing TEAMS_APP_ID in env")

    # Lark
    lark_id = os.getenv("LARK_APP_ID")
    if lark_id:
        table.add_row("Lark (Feishu)", "[green]Configured[/green]", f"App ID: {lark_id[:8]}...")
    else:
        table.add_row(
            "Lark (Feishu)", "[yellow]Not Configured[/yellow]", "Missing LARK_APP_ID in env"
        )

    console.print(table)


@app.command("register")
def register_wizard(
    channel: str = typer.Argument(..., help="Channel to configure: 'teams' or 'lark'"),
) -> None:
    """Interactive guide for registering a bot in Teams or Lark."""
    ch = channel.lower()
    if ch == "teams":
        console.print(
            Panel(
                "[bold cyan]Microsoft Teams Bot Registration Guide[/bold cyan]\n\n"
                "1. Go to Azure Portal -> Microsoft Entra ID -> App registrations.\n"
                "2. Register a new Multi-tenant or Single-tenant application.\n"
                "3. Under 'Certificates & secrets', create a Client Secret.\n"
                "4. Go to Azure Bot Service, link your App ID and Secret, and configure endpoint:\n"
                "   [bold yellow]https://your-domain.com/api/messages[/bold yellow]\n"
                "5. Save TEAMS_APP_ID and TEAMS_APP_PASSWORD into your .env file.",
                title="Teams Setup",
            )
        )
    elif ch in ("lark", "feishu"):
        console.print(
            Panel(
                "[bold cyan]Lark / Feishu Bot Registration Guide[/bold cyan]\n\n"
                "1. Go to Lark Open Platform -> Developer Console -> Create Custom App.\n"
                "2. In 'App Credentials', copy App ID and App Secret.\n"
                "3. In 'Permissions & Scopes', add:\n"
                "   - [green]im:message[/green], [green]im:message:send_as_bot[/green], [green]im:chat[/green]\n"
                "4. In 'Event Subscriptions', select [bold]WebSocket Long Connection[/bold].\n"
                "   (Zero public IP or domain required for local development!)\n"
                "5. Save LARK_APP_ID and LARK_APP_SECRET into your .env file.",
                title="Lark Setup",
            )
        )
    else:
        console.print(f"[red]Unknown channel: {channel}. Choose 'teams' or 'lark'.[/red]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
