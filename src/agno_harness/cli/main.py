import asyncio
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from ..api_paths import HEALTH_PATH
from ..config import RelayConfig
from ..helpers.lark import interactive_lark_onboarding
from ..helpers.scaffold import CHANNELS, copy_project
from ..helpers.teams import (
    TeamsOnboardError,
    callback_setup_command,
    fetch_teams_app_registration,
    format_link,
    inspect_teams_config,
    interactive_teams_onboarding,
    print_url,
    probe_health,
    split_link,
    teams_cli_executable,
)

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
    name: str = typer.Option("", "--name", help="Bot name passed to `teams app create`"),
    endpoint: str = typer.Option(
        "",
        "--endpoint",
        help="Public messaging URL, for example https://host/api/v1/channels/teams/messages",
    ),
    project: str = typer.Option(
        "",
        "--project",
        help="Scaffold a teams agent project in this directory after the bot is created",
    ),
) -> None:
    """Create a Teams bot with the Developer CLI and print env lines. Does not write a file."""
    try:
        interactive_teams_onboarding(
            prompt_input=False,
            save_to_env=False,
            name=name,
            endpoint=endpoint,
            use_cli=teams_cli_executable() is not None,
            project_dir=project,
        )
    except TeamsOnboardError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


def _labeled_rows(rows: list[tuple[str, str]]) -> str:
    width = max(len(label) for label, _ in rows)
    return "\n".join(f"- {label:<{width}}  {value}" for label, value in rows)


def _url_row(label: str, url: str, *, style: str) -> str:
    """Put a long URL under its label so the pane does not clip the query."""
    text = format_link(url)
    if not text:
        return f"- {label:<10}  [red]missing[/red]"
    limit = max(48, console.width - 4)
    chunks = split_link(text, limit)
    body = "\n".join(f"  [{style} link={text}]{chunk}[/]" for chunk in chunks)
    return f"- {label}\n{body}"


def _relay_path(public_base: str, prefix: str, path: str) -> str:
    base = public_base.rstrip("/")
    pre = prefix.strip()
    if pre and not pre.startswith("/"):
        pre = "/" + pre
    pre = pre.rstrip("/")
    suffix = path if path.startswith("/") else f"/{path}"
    return f"{base}{pre}{suffix}"


@teams_app.command("doctor")
def teams_doctor_cmd(
    env_file: str = typer.Option(".env", "--env-file", help="Env file to load before checking"),
    base_url: str = typer.Option(
        "",
        "--base-url",
        help="Running server origin, e.g. http://127.0.0.1:8000",
    ),
    prefix: str = typer.Option(
        "",
        "--prefix",
        help="Router prefix passed to relay.get_router, if any",
    ),
) -> None:
    """Check credentials, the install link, and whether the messaging callback is set."""
    load_dotenv(env_file, override=False)
    public = base_url or "https://<your-domain>"
    report = inspect_teams_config(public_base=public, prefix=prefix)

    # The pane is often narrower than the width the terminal reports. A rule or
    # URL drawn at the reported width runs off the right edge.
    console.width = min(console.width, 92)

    console.print()
    console.print(Rule("Teams CLI State", style="white", align="left"))

    registration = None
    if not report.problems:
        registration = fetch_teams_app_registration(report.app_id)

    callback_label = "[red]missing[/red]"
    callback_extra: list[str] = []
    if registration is None:
        callback_label = "[yellow]skipped[/yellow]"
    elif registration.lookup_error:
        callback_label = f"[yellow]{registration.lookup_error}[/yellow]"
    elif registration.callback_configured:
        callback_label = _url_row("Callback", registration.callback_url, style="green")
    else:
        report.problems.append("messaging callback is not configured")
        callback_label = "[red]Not configured[/red]"
        callback_extra = [
            "1. Expose port 8000: [cyan]ngrok http 8000[/cyan]",
            f"2. Set the callback: [cyan]{callback_setup_command(report.app_id)}[/cyan]",
            f"Local route: [cyan]{report.messaging_endpoint}[/cyan]",
        ]

    secret_status = (
        f"[green]{report.password_hint}[/green]" if report.password_hint else "[red]missing[/red]"
    )
    agent_lines = _labeled_rows(
        [
            ("App ID", report.app_id or "[red]missing[/red]"),
            ("Password", secret_status),
            ("Tenant", report.tenant_id or "(empty — multi-tenant)"),
        ]
    )
    if callback_label.startswith("- "):
        agent_lines = f"{agent_lines}\n{callback_label}"
    else:
        agent_lines = f"{agent_lines}\n" + _labeled_rows([("Callback", callback_label)])
    if callback_extra:
        agent_lines = f"{agent_lines}\n" + "\n".join(callback_extra)
    print_url(console, "Agent State", agent_lines, border_style="cyan")

    if report.problems and registration is None:
        for problem in report.problems:
            console.print(f"[red]{problem}[/red]")
        raise typer.Exit(code=1)

    link_rows: list[str] = []
    if registration is not None and not registration.lookup_error:
        link_rows.append(_url_row("Install", registration.install_url, style="cyan"))
        link_rows.append(_url_row("Portal", registration.portal_url, style="blue"))
    if link_rows:
        print_url(
            console,
            "Developer Portal & Install URL",
            "\n".join(link_rows),
            border_style="blue",
        )
    elif registration is not None and registration.lookup_error:
        print_url(
            console,
            "Developer Portal & Install URL",
            registration.lookup_error,
            border_style="yellow",
        )

    if base_url:
        health_url = _relay_path(base_url, prefix, HEALTH_PATH)
        health_ok, health_detail = asyncio.run(probe_health(health_url))
        report.health_ok = health_ok
        report.health_detail = health_detail
        if health_ok:
            console.print(f"[green]Server is up:[/green] {health_url}")
        else:
            console.print(f"[red]{health_detail}[/red] {health_url}")

    raise typer.Exit(code=0 if report.ok else 1)


@app.command("init")
def init_project(
    project_dir: str = typer.Argument(".", help="Target directory for new agent project"),
    channel: str = typer.Option(
        "all",
        "--channel",
        help="Transport: all, cli, web, teams, or lark.",
    ),
    with_knowledge: bool = typer.Option(
        False,
        "--with-knowledge",
        help="Accepted for existing commands. Notes, a skill, and a sample sub-agent are always written.",
    ),
) -> None:
    """Scaffold an agent project. Channel selects which mount() calls are written."""
    selected = channel.lower().strip()
    if selected not in CHANNELS:
        console.print(f"[red]Unknown channel. Choose {', '.join(CHANNELS)}.[/red]")
        raise typer.Exit(code=1)

    target = Path(project_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    del with_knowledge
    copy_project(target, channel=selected)

    follow_up = {
        "all": (
            "mount_all calls web, teams, lark, and cli. "
            "Missing Teams or Lark settings log an error and the process stops."
        ),
        "cli": "The terminal calls cli.mount. FastAPI is not started.",
        "web": "AG-UI is [cyan]POST /api/v1/channels/web/agui[/cyan].",
        "teams": (
            "mount_all calls web and teams. "
            "Teams endpoint is [cyan]POST /api/v1/channels/teams/messages[/cyan]. "
            "Check it with [cyan]agno-harness teams doctor[/cyan]."
        ),
        "lark": "mount_all calls lark. Missing app id or secret logs an error and the process stops.",
    }[selected]
    notes = (
        "\nSeed notes: [cyan]app/knowledge/*.md[/cyan]"
        "\nLive notes and databases: [cyan]data/[/cyan] (gitignored)"
    )
    console.print(
        Panel(
            f"[bold green]✓ Scaffolding complete at {target}[/bold green]\n\n"
            f"Next steps:\n"
            f"  1. [cyan]cp .env.example .env[/cyan]\n"
            f"  2. [cyan]python agent.py[/cyan]\n"
            f"  3. {follow_up}{notes}",
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
        try:
            interactive_teams_onboarding(
                prompt_input=True,
                save_to_env=False,
                use_cli=teams_cli_executable() is not None,
            )
        except TeamsOnboardError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    else:
        console.print(f"[red]Unknown channel: {channel}. Choose 'lark' or 'teams'.[/red]")


@app.command("register")
def register_wizard(
    channel: str = typer.Argument(..., help="Channel to configure: 'teams' or 'lark'"),
) -> None:
    """Interactive guide for registering a bot in Teams or Lark."""
    ch = channel.lower()
    if ch in ("teams", "ms-teams"):
        try:
            interactive_teams_onboarding(
                prompt_input=True,
                save_to_env=False,
                use_cli=teams_cli_executable() is not None,
            )
        except TeamsOnboardError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    elif ch in ("lark", "feishu"):
        interactive_lark_onboarding(prompt_input=True, save_to_env=True)
    else:
        console.print(f"[red]Unknown channel: {channel}. Choose 'teams' or 'lark'.[/red]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
