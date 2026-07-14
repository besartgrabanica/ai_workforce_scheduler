#!/usr/bin/env python3
"""Force-start the Workforce Scheduler, killing anything on port 5050 first."""

import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser

PORT = 5050
HOST = "127.0.0.1"
APP_URL = f"http://{HOST}:{PORT}"


def get_pids_on_port(port):
    """Return list of PIDs using the given port."""
    try:
        result = subprocess.run(
            ["fuser", f"{port}/tcp"],
            capture_output=True, text=True
        )
        pids = [int(p) for p in result.stdout.split() if p.strip().isdigit()]
        return pids
    except Exception:
        return []


def kill_port(port):
    pids = get_pids_on_port(port)
    if not pids:
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  Stopped process {pid} on port {port}.")
        except ProcessLookupError:
            pass
    time.sleep(1)
    # Force-kill anything still alive
    pids = get_pids_on_port(port)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"  Force-killed process {pid}.")
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    return True


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


def check_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        print("  WARNING: .env file not found.")
        print("  The AI Assistant will not work without an ANTHROPIC_API_KEY.")
        print()


def main():
    print("=" * 50)
    print("  Workforce Scheduler — Force Start")
    print("=" * 50)
    print()

    check_env()

    if port_in_use(PORT):
        print(f"  Port {PORT} is in use. Clearing it ...")
        kill_port(PORT)
        if port_in_use(PORT):
            print(f"  ERROR: Could not free port {PORT}. Try restarting your computer.")
            sys.exit(1)
        print(f"  Port {PORT} is now free.")
    else:
        print(f"  Port {PORT} is free.")

    print(f"  Starting app on {APP_URL} ...")
    print("  Press Ctrl+C to stop.")
    print()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    def open_browser():
        time.sleep(2)
        webbrowser.open(APP_URL)

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    os.execlp("python3", "python3", "-m", "flask", "--app", "app", "run",
              "--host", HOST, "--port", str(PORT))


if __name__ == "__main__":
    main()
