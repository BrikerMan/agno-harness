"""Microsoft Teams bot onboarding helper and interactive CLI wizard.

Teams has no scan-to-create QR code. The wizard shells out to the Teams Developer CLI
(``teams app create``), prints the install URL, and can scaffold a project.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from ..channels.teams import teams_messaging_endpoint
from ..channels.teams_connector import (
    TeamsChannelError,
    TeamsConnectorClient,
    teams_token_authority,
)
from ..config import RelayConfig
from .env_writer import save_env_file
from .scaffold import copy_project

TEAMS_DEV_PORTAL_URL = "https://dev.teams.microsoft.com/apps"
TEAMS_CLI_PACKAGE = "@microsoft/teams.cli"
_console = Console()


def generate_teams_manifest_template(
    bot_name: str = "AgnoRelayBot",
    bot_app_id: str = "YOUR_MICROSOFT_APP_ID",
    bot_description: str = "Enterprise multi-channel AGNO Agent bot powered by agno-harness.",
) -> dict[str, Any]:
    """Generate a standard Teams App Manifest (v1.16) for sideloading into Microsoft Teams."""
    return {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
        "manifestVersion": "1.16",
        "version": "1.0.0",
        "id": bot_app_id,
        "packageName": f"com.agno_harness.{bot_name.lower()}",
        "developer": {
            "name": "agno-harness",
            "websiteUrl": "https://github.com",
            "privacyUrl": "https://github.com/privacy",
            "termsOfUseUrl": "https://github.com/terms",
        },
        "icons": {
            "color": "color.png",
            "outline": "outline.png",
        },
        "name": {
            "short": bot_name,
            "full": f"{bot_name} AI Agent",
        },
        "description": {
            "short": bot_description[:80],
            "full": bot_description,
        },
        "accentColor": "#4F46E5",
        "bots": [
            {
                "botId": bot_app_id,
                "scopes": ["personal", "team", "groupchat"],
                "supportsFiles": True,
                "isNotificationOnly": False,
            }
        ],
        "permissions": ["identity", "message.send"],
        "validDomains": ["token.botframework.com"],
    }


class TeamsOnboardError(RuntimeError):
    """Teams Developer CLI did not return an app id, secret, or install URL."""


CreateTeamsApp = Callable[[str, str], dict[str, Any]]


def teams_cli_executable() -> str | None:
    """Return the ``teams`` binary from PATH, if the Developer CLI is installed."""
    return shutil.which("teams")


def normalize_messaging_endpoint(value: str) -> str:
    """Accept a public origin or a full messaging URL and return ``https://host/api/messages``."""
    text = value.strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith("https://"):
        raise TeamsOnboardError("Messaging endpoint must be an https URL")
    if text.endswith("/api/messages"):
        return text
    return f"{text}/api/messages"


def install_url_for(teams_app_id: str, install_link: str = "") -> str:
    """Install link printed by ``teams app create``. Teams does not use a QR code."""
    link = install_link.strip()
    if link:
        return link
    app_id = teams_app_id.strip()
    if not app_id:
        return ""
    return f"https://teams.microsoft.com/l/app/{app_id}?installAppPackage=true&webjoin=true"


def _env_assignments(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    return found


def _credential_map(payload: dict[str, Any], dotenv: dict[str, str] | None) -> dict[str, str]:
    inline = payload.get("credentials")
    if isinstance(inline, dict):
        mapped = {str(key): str(value) for key, value in inline.items() if value}
        if mapped.get("CLIENT_ID") and mapped.get("CLIENT_SECRET"):
            return mapped
    if dotenv is not None:
        return dotenv
    credentials_file = payload.get("credentialsFile")
    if isinstance(credentials_file, str) and credentials_file.strip():
        path = Path(credentials_file)
        if path.is_file():
            return _env_assignments(path.read_text(encoding="utf-8"))
    return {}


def parse_teams_cli_create(
    payload: dict[str, Any],
    *,
    dotenv: dict[str, str] | None = None,
) -> tuple[dict[str, str], str]:
    """Map ``teams app create --json`` onto harness env keys and the install URL."""
    credentials = _credential_map(payload, dotenv)
    app_id = (credentials.get("CLIENT_ID") or str(payload.get("botId") or "")).strip()
    password = credentials.get("CLIENT_SECRET", "").strip()
    tenant_id = credentials.get("TENANT_ID", "").strip()
    if not app_id or not password:
        raise TeamsOnboardError("teams app create JSON is missing CLIENT_ID or CLIENT_SECRET")
    install_url = install_url_for(
        str(payload.get("teamsAppId") or ""),
        str(payload.get("installLink") or ""),
    )
    if not install_url:
        raise TeamsOnboardError("teams app create JSON is missing installLink")
    keys = {
        "AGNO_HARNESS_TEAMS_APP_ID": app_id,
        "AGNO_HARNESS_TEAMS_APP_PASSWORD": password,
        "AGNO_HARNESS_TEAMS_TENANT_ID": tenant_id,
    }
    return keys, install_url


def _extract_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise TeamsOnboardError("teams app create did not print JSON")
    payload = json.loads(text[start : end + 1])
    if not isinstance(payload, dict):
        raise TeamsOnboardError("teams app create JSON was not an object")
    return payload


def create_teams_app_with_cli(name: str, endpoint: str) -> dict[str, Any]:
    """Run ``teams app create`` and let its progress print while Graph registration waits.

    ``--json`` hides those steps, so a successful create looks frozen for about half a minute.
    """
    executable = teams_cli_executable()
    if executable is None:
        raise TeamsOnboardError(
            "Teams Developer CLI is not on PATH. Install it with "
            f"`npm install -g {TEAMS_CLI_PACKAGE}`, then run `teams login --device-code`."
        )
    print_status = (
        "[bold]Creating the Teams bot…[/bold] Graph registration usually takes about a minute."
    )
    with _console.status(print_status, spinner="dots"), tempfile.TemporaryDirectory() as tmp:
        env_path = str(Path(tmp) / "teams.env")
        command = [
            executable,
            "app",
            "create",
            "--name",
            name,
            "--teams-managed",
            "--env",
            env_path,
        ]
        if endpoint:
            command.extend(["--endpoint", endpoint])
        env = os.environ.copy()
        env["TEAMS_NO_INTERACTIVE"] = "1"
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if completed.returncode != 0:
            raise TeamsOnboardError(
                format_teams_cli_failure(output.strip())
                or f"teams app create exited {completed.returncode}"
            )
        credentials = _env_assignments(Path(env_path).read_text(encoding="utf-8"))
    return payload_from_teams_create_output(output, credentials)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_INSTALL_URL = re.compile(r"https://teams\.microsoft\.com/l/app/[0-9a-fA-F-]+[^\s]*")
_TEAMS_APP_ID = re.compile(r"Teams App ID:\s*([0-9a-fA-F-]{36})")


def payload_from_teams_create_output(text: str, dotenv: dict[str, str]) -> dict[str, Any]:
    """Build the JSON shape ``parse_teams_cli_create`` expects from CLI text plus its env file."""
    plain = _ANSI.sub("", text)
    install = _INSTALL_URL.search(plain)
    teams_app = _TEAMS_APP_ID.search(plain)
    return {
        "teamsAppId": teams_app.group(1) if teams_app else "",
        "installLink": install.group(0).rstrip(".,)") if install else "",
        "credentials": {
            "CLIENT_ID": dotenv.get("CLIENT_ID", ""),
            "CLIENT_SECRET": dotenv.get("CLIENT_SECRET", ""),
            "TENANT_ID": dotenv.get("TENANT_ID", ""),
        },
    }


def scaffold_teams_project(project_dir: str, keys: dict[str, str] | None = None) -> Path:
    """Copy the teams channel template and write harness credentials into its ``.env``."""
    target = Path(project_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    copy_project(target, channel="teams")
    if keys:
        save_env_file(keys, filepath=str(target / ".env"))
    return target


def format_link(url: str) -> str:
    """Break a long Teams URL before the query so the id is not split mid-token."""
    if "?" not in url:
        return url
    path, query = url.split("?", 1)
    return f"{path}\n?{query}"


def callback_setup_command(app_id: str) -> str:
    """Teams CLI command that sets the bot messaging callback."""
    return f"teams app update {app_id} --endpoint https://<tunnel-host>/api/messages"


def _print_url(label: str, url: str) -> None:
    _console.print(Panel(format_link(url), title=label, border_style="cyan"))


def _print_cli_recipe(name: str, endpoint: str) -> None:
    endpoint_arg = endpoint or "https://<tunnel-host>/api/messages"
    _console.print(
        "\nTeams has no QR code. The Developer CLI prints an install URL.\n"
        f"  npm install -g {TEAMS_CLI_PACKAGE}\n"
        "  teams login --device-code\n"
        f'  teams app create --name "{name or "AgnoHarnessBot"}" '
        f'--endpoint "{endpoint_arg}"'
    )
    _print_url("Developer Portal", TEAMS_DEV_PORTAL_URL)


def _prompt_line(label: str) -> str:
    try:
        return input(label).strip()
    except EOFError:
        return ""


def teams_portal_token_message(username: str = "") -> str:
    """Explain a logged-in CLI whose Developer Portal token cannot create a bot."""
    who = f"Logged in as {username}. " if username else "The Teams CLI is logged in. "
    return (
        f"{who}The Developer Portal token is missing, so bot creation cannot get a Graph token.\n"
        "Browser `teams login` fails with AADSTS70007 and does not refresh an existing session.\n"
        "Run `teams logout`, then `teams login --device-code`, and open the URL it prints."
    )


def format_teams_cli_failure(detail: str) -> str:
    """Turn ``teams --json`` error output into one readable message."""
    payload: dict[str, Any] | None = None
    try:
        parsed = _extract_json_object(detail)
    except (TeamsOnboardError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        payload = parsed
    error = payload.get("error") if payload else None
    if isinstance(error, dict) and error.get("code") == "AUTH_TOKEN_FAILED":
        return teams_portal_token_message()
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return detail or "teams app create failed"


def fetch_teams_cli_status() -> dict[str, Any]:
    """Return ``teams status --json``. Exit code 0 still happens when the portal token is missing."""
    executable = teams_cli_executable()
    if executable is None:
        raise TeamsOnboardError(
            "Teams Developer CLI is not on PATH. Install it with "
            f"`npm install -g {TEAMS_CLI_PACKAGE}`, then run `teams login`."
        )
    completed = subprocess.run(
        [executable, "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        raise TeamsOnboardError(format_teams_cli_failure(completed.stderr.strip()))
    try:
        return _extract_json_object(completed.stdout)
    except (TeamsOnboardError, json.JSONDecodeError) as exc:
        raise TeamsOnboardError("teams status did not print JSON") from exc


def assert_teams_portal_ready(status: dict[str, Any]) -> None:
    """Stop before asking anything when login cannot create a bot."""
    if not status.get("loggedIn"):
        raise TeamsOnboardError(
            "Not logged in. Run `teams login --device-code` and open the URL it prints."
        )
    portal = status.get("tdp")
    connected = isinstance(portal, dict) and bool(portal.get("connected"))
    if not connected:
        raise TeamsOnboardError(teams_portal_token_message(str(status.get("username") or "")))


def _manual_keys(*, prompt_input: bool) -> dict[str, str]:
    app_id = ""
    app_password = ""
    tenant_id = ""
    if prompt_input and sys.stdin.isatty():
        app_id = _prompt_line("\nTeams app id (CLIENT_ID): ")
        app_password = _prompt_line("Teams bot client secret: ")
        tenant_id = _prompt_line(
            "Teams tenant id (required for single-tenant, empty for multi-tenant): "
        )
    if not app_id or not app_password:
        return {}
    return {
        "AGNO_HARNESS_TEAMS_APP_ID": app_id,
        "AGNO_HARNESS_TEAMS_APP_PASSWORD": app_password,
        "AGNO_HARNESS_TEAMS_TENANT_ID": tenant_id,
    }


def interactive_teams_onboarding(
    *,
    prompt_input: bool = False,
    save_to_env: bool = False,
    env_file: str = ".env",
    name: str = "",
    endpoint: str = "",
    use_cli: bool = False,
    create_app: CreateTeamsApp | None = None,
    project_dir: str = "",
    read_status: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Create a Teams bot and print its install URL and env lines.

    When the Teams CLI is logged in but the Developer Portal token is missing,
    this raises before asking for a name or endpoint. ``teams login`` does not
    refresh that token while the account is already signed in.
    """
    if create_app is None and use_cli:
        status = read_status() if read_status is not None else fetch_teams_cli_status()
        assert_teams_portal_ready(status)

    _console.print(
        Panel(
            "Creating a Microsoft Teams bot. This prints an install URL, not a QR code.\n"
            "Creation calls Graph and usually takes about a minute.",
            title="agno-harness teams onboard",
            border_style="blue",
        )
    )

    bot_name = name.strip() or "AgnoHarnessBot"
    messaging_endpoint = normalize_messaging_endpoint(endpoint) if endpoint.strip() else ""

    keys: dict[str, str] = {}
    install_url = ""
    create_now = create_app is not None or use_cli
    if create_now:
        runner = create_app or create_teams_app_with_cli
        payload = runner(bot_name, messaging_endpoint)
        keys, install_url = parse_teams_cli_create(payload)
        _print_url("Install in Teams", install_url)
    else:
        _print_cli_recipe(bot_name, messaging_endpoint)
        keys = _manual_keys(prompt_input=prompt_input)

    if keys:
        _console.print("\n[bold]Env[/bold] (printed only, not written):")
        for key, value in keys.items():
            _console.print(f"{key}={value}")
        if save_to_env:
            save_env_file(keys, filepath=env_file)
    else:
        _console.print("\n[yellow]No app id or client secret yet.[/yellow]")

    if project_dir.strip():
        target = scaffold_teams_project(project_dir, keys or None)
        _console.print(f"[green]Project written:[/green] {target}")
    elif keys:
        _console.print(
            "Copy the env lines into [cyan].env[/cyan], then run [cyan]agno-harness teams doctor[/cyan]."
        )

    return keys


@dataclass
class TeamsAppRegistration:
    """Install link and messaging callback read from ``teams app get --json``."""

    install_url: str = ""
    portal_url: str = ""
    callback_url: str = ""
    lookup_error: str = ""

    @property
    def callback_configured(self) -> bool:
        return bool(self.callback_url.strip())


def registration_from_app_get(payload: dict[str, Any]) -> TeamsAppRegistration:
    """Read install URL and messaging callback from ``teams app get --json``."""
    endpoint = payload.get("endpoint")
    callback = endpoint.strip() if isinstance(endpoint, str) else ""
    return TeamsAppRegistration(
        install_url=str(payload.get("installLink") or "").strip(),
        portal_url=str(payload.get("portalLink") or "").strip(),
        callback_url=callback,
    )


def fetch_teams_app_registration(app_id: str) -> TeamsAppRegistration:
    """Ask the Teams CLI whether this bot has an install link and a messaging callback."""
    executable = teams_cli_executable()
    if executable is None:
        return TeamsAppRegistration(lookup_error="Teams CLI is not installed")
    env = os.environ.copy()
    env["TEAMS_NO_INTERACTIVE"] = "1"
    completed = subprocess.run(
        [executable, "app", "get", app_id, "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    raw = (completed.stdout or completed.stderr or "").strip()
    if completed.returncode != 0:
        return TeamsAppRegistration(
            lookup_error=format_teams_cli_failure(raw) or "teams app get failed"
        )
    try:
        payload = _extract_json_object(completed.stdout or "")
    except (TeamsOnboardError, json.JSONDecodeError):
        return TeamsAppRegistration(lookup_error="teams app get did not print JSON")
    return registration_from_app_get(payload)


@dataclass
class TeamsDoctorReport:
    """Result of ``agno-harness teams doctor``."""

    app_id: str
    password_set: bool
    tenant_id: str | None
    authority: str
    messaging_endpoint: str
    problems: list[str] = field(default_factory=list)
    token_ok: bool | None = None
    token_detail: str = ""
    health_ok: bool | None = None
    health_detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and self.token_ok is not False and self.health_ok is not False


def inspect_teams_config(
    *,
    public_base: str = "https://<your-domain>",
    prefix: str = "",
) -> TeamsDoctorReport:
    """Read Teams env vars and build the messaging endpoint. Does not call the network."""
    app_id = RelayConfig.teams_app_id()
    password = RelayConfig.teams_app_password()
    tenant = RelayConfig.teams_tenant_id().strip() or None
    problems: list[str] = []
    if not app_id:
        problems.append("AGNO_HARNESS_TEAMS_APP_ID is empty")
    if not password:
        problems.append("AGNO_HARNESS_TEAMS_APP_PASSWORD is empty")
    authority = ""
    try:
        authority = teams_token_authority(tenant)
    except TeamsChannelError as exc:
        problems.append(str(exc))
    return TeamsDoctorReport(
        app_id=app_id,
        password_set=bool(password),
        tenant_id=tenant,
        authority=authority,
        messaging_endpoint=teams_messaging_endpoint(public_base, prefix),
        problems=problems,
    )


async def probe_teams_token(
    app_id: str, app_password: str, tenant_id: str | None
) -> tuple[bool, str]:
    """Request a Bot Connector token. Returns ``(ok, detail)`` and never prints the secret."""
    client = TeamsConnectorClient(app_id, app_password, tenant_id)
    try:
        await client.get_token()
    except TeamsChannelError as exc:
        return False, str(exc)
    else:
        return True, f"token ok via {client.authority}"
    finally:
        await client.aclose()


async def probe_health(url: str) -> tuple[bool, str]:
    """GET a relay health URL."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return False, f"health request failed: {exc}"
    if response.status_code == 200:
        return True, "health 200"
    return False, f"health HTTP {response.status_code}"
