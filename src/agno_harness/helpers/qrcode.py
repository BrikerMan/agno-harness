"""Terminal QR code generator and link visualizer with zero mandatory C-dependencies."""

from __future__ import annotations

import shutil
import subprocess


def print_terminal_qr(data: str, title: str | None = None) -> None:
    """Print a terminal QR code if `qrcode` or `lark-cli` is available, or fallback to an ANSI bordered link box."""
    if title:
        print(f"\n\033[1;36m=== {title} ===\033[0m\n")

    # 1. Try using lark-cli if available in PATH (official formatting)
    if shutil.which("lark-cli"):
        try:
            res = subprocess.run(
                ["lark-cli", "auth", "qrcode", data, "--ascii"],
                capture_output=True,
                text=True,
                check=False,
                timeout=3,
            )
            if res.returncode == 0 and res.stdout.strip():
                print(res.stdout)
                return
        except Exception:
            pass

    # 2. Try using Python qrcode package
    try:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return
    except ImportError:
        pass

    # 3. Graceful zero-dependency fallback: print high-visibility ANSI link card
    print(
        "\n\033[1;33m[Tip: Run `uv add qrcode` to display direct scanable terminal QR codes]\033[0m"
    )
    print("\033[1;32m┌" + "─" * 68 + "┐\033[0m")
    print(
        f"\033[1;32m│\033[0m \033[1;37mScan or Open in Browser:\033[0m{' ' * 43}\033[1;32m│\033[0m"
    )
    print(f"\033[1;32m│\033[0m \033[4;34m{data:<66}\033[0m \033[1;32m│\033[0m")
    print("\033[1;32m└" + "─" * 68 + "┘\033[0m\n")
