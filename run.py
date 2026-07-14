#!/usr/bin/env python3
"""Start the Workforce Scheduler app."""

import os
import socket
import subprocess
import sys
import time
import webbrowser

PORT = 5050
HOST = "127.0.0.1"
APP_URL = f"http://{HOST}:{PORT}"


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


def check_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        print("  WARNING: .env file not found.")
        print("  The AI Assistant will not work without an ANTHROPIC_API_KEY.")
        print(f"  Create the file at: {env_path}")
        print()


def main():
    print("=" * 50)
    print("  Workforce Scheduler — KiKxxl E.ON Kosovo")
    print("=" * 50)
    print()

    # Check .env
    check_env()

    # Check if already running
    if port_in_use(PORT):
        print(f"  ERROR: Port {PORT} is already in use.")
        print(f"  The app may already be running at {APP_URL}")
        print()
        print("  To force-restart, run:  python3 force_run.py")
        sys.exit(1)

    print(f"  Starting app on {APP_URL} ...")
    print("  Press Ctrl+C to stop.")
    print()

    # Change to script directory so Flask finds app.py
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Open browser after a short delay (in background)
    def open_browser():
        time.sleep(2)
        webbrowser.open(APP_URL)

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    # Run Flask
    os.execlp("python3", "python3", "-m", "flask", "--app", "app", "run",
              "--host", HOST, "--port", str(PORT))


if __name__ == "__main__":
    main()
