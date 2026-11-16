# -*- coding: utf-8 -*-
"""MediaCrawler WebUI launcher.

Double-clicked through the desktop shortcut.  It starts the FastAPI/uvicorn
server in the background with no console window and opens the WebUI page in the
default browser as soon as the server answers.

If the server is already running it just opens the browser page again.
"""
import ctypes
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
# Port 8080 is the project default but is often taken by other software on
# Windows, so try a short list and use the first free one.
PORTS = [8090, 8091, 8092, 8093, 8081, 8099]
VENV_SCRIPTS = ROOT / ".venv" / "Scripts"
PYTHONW = VENV_SCRIPTS / "pythonw.exe"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "webui.log"
CREATE_NO_WINDOW = 0x08000000
TITLE = "MediaCrawler WebUI"


def health_ok(port, timeout=2.0):
    """True only when a MediaCrawler API server answers on this port."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/health", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read(200).decode("utf-8", "ignore")
            return '"ok"' in body.replace(" ", "")
    except Exception:
        return False


def port_in_use(port):
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        sock.connect((HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def message(text, flags=0x10):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, flags)
    except Exception:
        pass


def main():
    # already running? then just bring the page up
    for port in PORTS:
        if health_ok(port):
            webbrowser.open(f"http://{HOST}:{port}/")
            return 0

    port = next((p for p in PORTS if not port_in_use(p)), None)
    if port is None:
        message(
            "端口 " + "、".join(str(p) for p in PORTS) + " 都被占用，MediaCrawler 无法启动。\n\n"
            f"请关闭占用这些端口的程序后重试，或查看日志：\n{LOG_FILE}"
        )
        return 1

    if not PYTHONW.exists():
        message(
            "运行环境不完整：找不到\n"
            f"{PYTHONW}\n\n请重新运行安装脚本或联系管理员。"
        )
        return 1

    url = f"http://{HOST}:{port}/"
    LOG_DIR.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["PATH"] = str(VENV_SCRIPTS) + os.pathsep + env.get("PATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} starting WebUI server on port {port} =====\n")
        log.flush()
        subprocess.Popen(
            [str(PYTHONW), "-m", "uvicorn", "api.main:app",
             "--host", HOST, "--port", str(port)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )

    for _ in range(120):  # up to 60 seconds
        if health_ok(port):
            webbrowser.open(url)
            return 0
        time.sleep(0.5)

    message(
        "WebUI 启动超时（60 秒内未响应）。\n\n"
        f"请查看日志排查原因：\n{LOG_FILE}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
