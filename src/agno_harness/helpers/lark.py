"""Feishu / Lark bot onboarding helper and interactive CLI wizard.

Provides automated Scan-to-Create QR code flow (zero-config Personal Agent registration),
terminal QR visualizer, recommended permissions and scope guide, and automated `.env` persistence.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

import httpx

from .env_writer import save_env_file
from .qrcode import print_terminal_qr

LARK_OPEN_DEV_URL = "https://open.feishu.cn/app"
LARK_DOCS_BOT_SETUP = "https://open.feishu.cn/document/home/introduction-to-custom-app-development"

FEISHU_ACCOUNTS_URL = "https://accounts.feishu.cn"
LARK_INTERNATIONAL_ACCOUNTS_URL = "https://accounts.larksuite.com"
APP_REGISTRATION_PATH = "/oauth/v1/app/registration"

RECOMMENDED_LARK_SCOPES = [
    ("im:message", "获取与发送单聊/群聊消息"),
    ("im:message.group_at_msg:readonly", "接收群聊中@机器人的消息（严格最小权限）"),
    ("im:message.p2p_msg:readonly", "接收单聊私聊消息"),
    ("im:resource:upload", "上传并发送图片、文件和多模态附件"),
    ("im:chat:readonly", "获取群聊基础信息及成员状态"),
]

RECOMMENDED_LARK_EVENTS = [
    ("im.message.receive_v1", "接收消息（包含文本、富文本 post、图片、文件）"),
    ("card.action.trigger", "卡片回传交互事件（HITL 审批、按钮回调）"),
]


def generate_lark_setup_guide() -> dict[str, Any]:
    """Return structured onboarding details for Feishu bot creation."""
    return {
        "portal_url": LARK_OPEN_DEV_URL,
        "recommended_scopes": RECOMMENDED_LARK_SCOPES,
        "recommended_events": RECOMMENDED_LARK_EVENTS,
        "recommended_mode": "WebSocket 长连接（无需公网 IP，无需配置 Webhook 回调域名）",
        "env_template": {
            "AGNO_HARNESS_LARK_APP_ID": "cli_a1b2c3d4e5f6...",
            "AGNO_HARNESS_LARK_APP_SECRET": "your_app_secret_here...",
            "AGNO_HARNESS_LARK_ENCRYPT_KEY": "optional_if_using_webhook",
            "AGNO_HARNESS_LARK_VERIFICATION_TOKEN": "optional_if_using_webhook",
        },
    }


def begin_lark_app_registration(
    domain: str = "feishu",
    *,
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Initiate Feishu / Lark Device Code flow to create a Personal Agent app.

    Returns registration payload including `device_code` and `verification_url`.
    """
    base_url = LARK_INTERNATIONAL_ACCOUNTS_URL if domain.lower() == "lark" else FEISHU_ACCOUNTS_URL
    url = f"{base_url}{APP_REGISTRATION_PATH}"

    payload = {
        "action": "begin",
        "archetype": "PersonalAgent",
        "auth_method": "client_secret",
        "request_user_info": "open_id tenant_brand",
    }

    def _do_request(c: httpx.Client) -> dict[str, Any]:
        resp = c.post(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Lark app registration failed (HTTP {resp.status_code}): {resp.text}"
            )
        data = resp.json()
        if "error" in data:
            desc = data.get("error_description") or data.get("error")
            raise RuntimeError(f"Lark app registration error: {desc}")
        return data

    if client is not None:
        data = _do_request(client)
    else:
        with httpx.Client(timeout=timeout) as c:
            data = _do_request(c)

    device_code = data.get("device_code")
    if not device_code:
        raise RuntimeError(f"Lark app registration missing device_code: {data}")

    user_code = data.get("user_code", "")
    verification_uri_complete = data.get("verification_uri_complete")
    if not verification_uri_complete:
        v_uri = data.get("verification_uri", "https://open.feishu.cn/page/launcher")
        verification_uri_complete = f"{v_uri}?user_code={user_code}"

    # Build direct QR URL with tracking params
    separator = "&" if "?" in verification_uri_complete else "?"
    qr_url = f"{verification_uri_complete}{separator}from=agno_harness&tp=ob_cli_app"

    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_url": verification_uri_complete,
        "qr_url": qr_url,
        "interval": int(data.get("interval", 5)),
        "expires_in": int(data.get("expires_in", 3600)),
    }


def poll_lark_app_registration(
    device_code: str,
    domain: str = "feishu",
    interval: int = 5,
    expires_in: int = 600,
    *,
    timeout: float = 15.0,
    on_tick: Callable[[int], None] | None = None,
    client: httpx.Client | None = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Poll Lark/Feishu registration endpoint until user approves or flow expires."""
    current_interval = max(2, interval)
    deadline = time.time() + expires_in
    current_domain = domain.lower()
    tick_count = 0

    def _poll_once(c: httpx.Client, dom: str) -> dict[str, Any]:
        base_url = LARK_INTERNATIONAL_ACCOUNTS_URL if dom == "lark" else FEISHU_ACCOUNTS_URL
        url = f"{base_url}{APP_REGISTRATION_PATH}"
        payload = {
            "action": "poll",
            "device_code": device_code,
        }
        resp = c.post(
            url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return resp.json()

    def _loop(c: httpx.Client) -> dict[str, str]:
        nonlocal current_interval, current_domain, tick_count
        while time.time() < deadline:
            sleep_func(current_interval)
            tick_count += 1
            if on_tick:
                on_tick(tick_count)

            try:
                data = _poll_once(c, current_domain)
            except Exception:
                # Transient network glitch, keep polling
                continue

            err = data.get("error")
            if not err and data.get("client_id"):
                client_id = str(data["client_id"])
                client_secret = str(data.get("client_secret", ""))
                user_info = data.get("user_info", {})
                tenant_brand = user_info.get("tenant_brand", current_domain)
                user_open_id = str(user_info.get("open_id", ""))

                # If brand is Lark and client_secret missing, switch domain and poll
                if tenant_brand == "lark" and not client_secret and current_domain != "lark":
                    current_domain = "lark"
                    continue

                return {
                    "app_id": client_id,
                    "app_secret": client_secret,
                    "user_open_id": user_open_id,
                    "brand": tenant_brand,
                }

            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                current_interval = min(current_interval + 5, 30)
                continue
            elif err == "access_denied":
                raise PermissionError("飞书扫码创建应用已被用户取消/拒绝 (access_denied)")
            elif err == "expired_token":
                raise TimeoutError("二维码已过期，请重新运行向导生成新二维码 (expired_token)")
            else:
                desc = data.get("error_description") or err or "未知错误"
                raise RuntimeError(f"飞书应用创建轮询失败: {desc}")

        raise TimeoutError("等待扫码授权超时，请重试")

    if client is not None:
        return _loop(client)
    else:
        with httpx.Client(timeout=timeout) as c:
            return _loop(c)


def fetch_lark_app_info(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
) -> dict[str, Any] | None:
    """Optionally fetch application profile (name, description, avatar) via tenant access token."""
    base_url = (
        "https://open.larksuite.com" if domain.lower() == "lark" else "https://open.feishu.cn"
    )
    try:
        with httpx.Client(timeout=10.0) as client:
            token_resp = client.post(
                f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
            token_data = token_resp.json()
            if token_data.get("code") != 0:
                return None
            token = token_data.get("tenant_access_token")
            if not token:
                return None

            app_resp = client.get(
                f"{base_url}/open-apis/application/v6/applications/{app_id}?lang=zh_cn",
                headers={"Authorization": f"Bearer {token}"},
            )
            app_data = app_resp.json()
            if app_data.get("code") == 0:
                app_info = app_data.get("data", {}).get("app", {})
                return {
                    "app_name": app_info.get("app_name", ""),
                    "description": app_info.get("description", ""),
                    "avatar_url": app_info.get("avatar", {}).get("avatar_72", ""),
                }
    except Exception:
        pass
    return None


def interactive_lark_onboarding(
    *,
    prompt_input: bool = False,
    save_to_env: bool = False,
    env_file: str = ".env",
    manual: bool = False,
    domain: str = "feishu",
) -> dict[str, str]:
    """Run interactive Lark onboarding wizard.

    Default: One-click Scan-to-Create QR flow directly generates personal agent bot without manual keys.
    Manual: Falls back to classical App ID / Secret copy-paste prompts if requested.
    """
    # 1. Non-interactive / CI / testing path
    if not prompt_input:
        app_id = "cli_sample_app_id"
        app_secret = "sample_app_secret"
        keys = {
            "AGNO_HARNESS_LARK_APP_ID": app_id,
            "AGNO_HARNESS_LARK_APP_SECRET": app_secret,
        }
        if save_to_env:
            save_env_file(keys, filepath=env_file)
        return keys

    # 2. Explicit manual configuration path
    if manual:
        print("\n\033[1;36m==============================================================\033[0m")
        print("\033[1;36m       🤖 agno-harness: 飞书 (Lark) 智能体手动配置向导         \033[0m")
        print("\033[1;36m==============================================================\033[0m\n")

        print("1. 请用飞书扫码或在浏览器打开下方链接，进入飞书开放平台创建企业自建应用：")
        print_terminal_qr(LARK_OPEN_DEV_URL, title="飞书开放平台应用创建入口")

        print("\033[1;33m2. 推荐开通的最小权限清单 (Least Privilege):\033[0m")
        for scope, desc in RECOMMENDED_LARK_SCOPES:
            print(f"   • \033[1;32m{scope:<34}\033[0m : {desc}")

        print("\n\033[1;33m3. 推荐订阅的事件 (Event Subscriptions):\033[0m")
        for event, desc in RECOMMENDED_LARK_EVENTS:
            print(f"   • \033[1;32m{event:<34}\033[0m : {desc}")

        print("\n\033[1;33m4. 通信模式推荐:\033[0m")
        print(
            "   • \033[1;35mWebSocket 长连接模式 (推荐)\033[0m: 本地开发和私有部署无需公网 IP 和证书，直接运行即连通！"
        )

        app_id = "cli_sample_app_id"
        app_secret = "sample_app_secret"

        if sys.stdin.isatty():
            try:
                val_id = input("\n请输入飞书 App ID (例如 cli_...): ").strip()
                if val_id:
                    app_id = val_id
                val_sec = input("请输入飞书 App Secret: ").strip()
                if val_sec:
                    app_secret = val_sec
            except (EOFError, KeyboardInterrupt):
                print("\n\033[1;33m[!] 已取消手动配置。\033[0m\n")
                return {}

        keys = {
            "AGNO_HARNESS_LARK_APP_ID": app_id,
            "AGNO_HARNESS_LARK_APP_SECRET": app_secret,
        }

        print("\n\033[1;32m┌" + "─" * 68 + "┐\033[0m")
        print(
            f"\033[1;32m│\033[0m \033[1;37m🔑 生成的标准环境变量配置:\033[0m{' ' * 42}\033[1;32m│\033[0m"
        )
        for k, v in keys.items():
            line_str = f"  {k}={v}"
            print(f"\033[1;32m│\033[0m \033[1;33m{line_str:<66}\033[0m \033[1;32m│\033[0m")
        print("\033[1;32m└" + "─" * 68 + "┘\033[0m\n")

        if save_to_env:
            target = save_env_file(keys, filepath=env_file)
            print(f"\033[1;32m✔ 凭据已成功保存至: {target}\033[0m\n")

        return keys

    # 3. Default: One-Click Scan-to-Create QR Flow
    print("\n\033[1;36m==============================================================\033[0m")
    print("\033[1;36m       🤖 agno-harness: 飞书 (Lark) 智能体一键扫码创建向导       \033[0m")
    print("\033[1;36m==============================================================\033[0m\n")
    print("\033[1;33m⏳ 正在向飞书开放平台请求智能体创建二维码...\033[0m")

    try:
        reg_info = begin_lark_app_registration(domain=domain)
    except Exception as e:
        print(f"\n\033[1;31m✖ 自动请求飞书扫码创建失败: {e}\033[0m")
        print("\033[1;33m是否切换为手动输入 App ID / Secret？[y/N]: \033[0m", end="")
        try:
            choice = input().strip().lower()
            if choice == "y":
                return interactive_lark_onboarding(
                    prompt_input=True,
                    save_to_env=save_to_env,
                    env_file=env_file,
                    manual=True,
                    domain=domain,
                )
        except (EOFError, KeyboardInterrupt):
            pass
        return {}

    qr_url = reg_info.get("qr_url") or reg_info["verification_url"]
    verification_url = reg_info["verification_url"]
    device_code = reg_info["device_code"]
    interval = reg_info.get("interval", 5)
    expires_in = reg_info.get("expires_in", 600)

    print("\n\033[1;32m📱 请使用【飞书手机客户端】扫码，一键自动创建专属智能体应用：\033[0m")
    print_terminal_qr(qr_url)

    print(f"\033[1;37m🔗 浏览器直接访问: \033[4;34m{verification_url}\033[0m")
    print(
        "\033[1;33m👉 手机扫码后在飞书上点击「确认创建」，系统将自动完成应用开通并保存凭证。\033[0m"
    )
    print("\033[1;30m   (按 Ctrl+C 可随时取消)\033[0m\n")

    spinner_frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    frame_idx = 0

    def on_tick(count: int) -> None:
        nonlocal frame_idx
        frame = spinner_frames[frame_idx % len(spinner_frames)]
        frame_idx += 1
        elapsed = count * interval
        sys.stdout.write(f"\r\033[1;36m{frame}\033[0m 正在等待飞书手机端确认... [{elapsed}s] ")
        sys.stdout.flush()

    try:
        credentials = poll_lark_app_registration(
            device_code=device_code,
            domain=domain,
            interval=interval,
            expires_in=expires_in,
            on_tick=on_tick,
        )
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n\n\033[1;33m[!] 用户取消了飞书扫码创建向导。\033[0m\n")
        return {}
    except Exception as e:
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()
        print(f"\n\033[1;31m✖ 扫码创建失败: {e}\033[0m\n")
        return {}

    app_id = credentials["app_id"]
    app_secret = credentials["app_secret"]
    user_open_id = credentials.get("user_open_id", "")

    app_info = fetch_lark_app_info(app_id, app_secret, domain=domain)
    app_name = (app_info.get("app_name") if app_info else "") or "Personal Agent"

    print("\n\033[1;32m🎉 飞书智能体应用创建成功！\033[0m")
    if app_name:
        print(f"   • 应用名称:       \033[1;37m{app_name}\033[0m")
    print(f"   • App ID:         \033[1;32m{app_id}\033[0m")
    masked_sec = (
        app_secret[:4] + "*" * (len(app_secret) - 8) + app_secret[-4:]
        if len(app_secret) > 8
        else "********"
    )
    print(f"   • App Secret:     \033[1;33m{masked_sec}\033[0m (已安全获取)")
    if user_open_id:
        print(f"   • 创建者 Open ID: \033[1;36m{user_open_id}\033[0m")

    keys = {
        "AGNO_HARNESS_LARK_APP_ID": app_id,
        "AGNO_HARNESS_LARK_APP_SECRET": app_secret,
    }
    if user_open_id:
        keys["AGNO_HARNESS_LARK_USER_OPEN_ID"] = user_open_id

    if save_to_env:
        target = save_env_file(keys, filepath=env_file)
        print(f"\n\033[1;32m✔ 凭据已自动写入本地: {target}\033[0m")

    print("\n\033[1;35m🚀 下一步:\033[0m")
    print("   • 运行 \033[1;32magno-harness run\033[0m 或直接启动 Agent。")
    print("   • agno-harness 将自动通过 WebSocket 长连接接入飞书，无需公网 IP 和证书！\n")

    return keys
