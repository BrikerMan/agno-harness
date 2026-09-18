"""Microsoft Teams bot onboarding helper and interactive CLI wizard.

Provides step-by-step guidance for Microsoft Developer Portal / Azure Bot registration,
Adaptive Card configurations, direct key terminal output, and automated saving to `.env`.
"""

from __future__ import annotations

import sys
from typing import Any

from .env_writer import save_env_file
from .qrcode import print_terminal_qr

TEAMS_DEV_PORTAL_URL = "https://dev.teams.microsoft.com/bots"
AZURE_BOT_PORTAL_URL = "https://portal.azure.com/#view/HubsExtension/BrowseResource/resourceType/Microsoft.BotService%2FbotServices"

RECOMMENDED_TEAMS_SCOPES = [
    ("ChannelMessage.Read.Group", "群聊/团队频道消息接收"),
    ("ChatMessage.Read", "单聊私信消息接收"),
    ("ChatMessage.Send", "向 Teams 单聊或群聊下发消息与 Adaptive Cards"),
]


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


def interactive_teams_onboarding(
    *,
    prompt_input: bool = False,
    save_to_env: bool = False,
    env_file: str = ".env",
) -> dict[str, str]:
    """Run an interactive CLI setup wizard for Microsoft Teams bots.

    Outputs keys directly in terminal and offers to write or update `.env`.
    """
    print("\n\033[1;34m==============================================================\033[0m")
    print("\033[1;34m    🏢 agno-harness: Microsoft Teams 机器人交互式创建向导        \033[0m")
    print("\033[1;34m==============================================================\033[0m\n")

    print("1. 请在浏览器打开 Teams Developer Portal 创建 Bot：")
    print_terminal_qr(TEAMS_DEV_PORTAL_URL, title="Teams 开发者门户创建入口")

    print("\033[1;33m2. 配置消息回调端点 (Messaging Endpoint):\033[0m")
    print("   • 线上域名格式: \033[1;32mhttps://<your-domain>/api/messages\033[0m")
    print("   • 本地调试推荐: 使用 `devtunnel` 或 `ngrok` (例如: `ngrok http 8000`)")

    print("\n\033[1;33m3. 最小权限说明 (Least Privilege):\033[0m")
    for scope, desc in RECOMMENDED_TEAMS_SCOPES:
        print(f"   • \033[1;32m{scope:<30}\033[0m : {desc}")

    app_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    app_password = "sample_password_secret"
    tenant_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"

    if prompt_input and sys.stdin.isatty():
        try:
            val_id = input("\n请输入 Teams Microsoft App ID: ").strip()
            if val_id:
                app_id = val_id
            val_pwd = input("请输入 Teams Bot Client Secret/Password: ").strip()
            if val_pwd:
                app_password = val_pwd
            val_tenant = input("请输入 Teams Tenant ID (可选): ").strip()
            if val_tenant:
                tenant_id = val_tenant
        except EOFError:
            pass

    keys = {
        "AGNO_HARNESS_TEAMS_APP_ID": app_id,
        "AGNO_HARNESS_TEAMS_APP_PASSWORD": app_password,
        "AGNO_HARNESS_TEAMS_TENANT_ID": tenant_id,
    }

    print("\n\033[1;32m┌" + "─" * 68 + "┐\033[0m")
    print(
        f"\033[1;32m│\033[0m \033[1;37m🔑 生成的标准环境变量配置 (可以直接复制或自动写入):\033[0m{' ' * 16}\033[1;32m│\033[0m"
    )
    for k, v in keys.items():
        line_str = f"  {k}={v}"
        print(f"\033[1;32m│\033[0m \033[1;33m{line_str:<66}\033[0m \033[1;32m│\033[0m")
    print("\033[1;32m└" + "─" * 68 + "┘\033[0m\n")

    if save_to_env:
        target = save_env_file(keys, filepath=env_file)
        print(f"\033[1;32m✔ 凭据已成功保存至: {target}\033[0m\n")

    return keys
