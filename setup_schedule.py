"""
Reads config.json and registers (or updates) the Windows Task Scheduler job
that runs fetch_data.py on a repeating interval. Re-run this any time you
change fetch_interval_minutes.
"""

import datetime
import json
import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
TASK_NAME = "PersonalBalanceDashboard_Fetch"


def load_settings():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "config.json not found. Copy config.example.json to config.json first."
        )
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def main():
    settings = load_settings()
    interval = settings.get("fetch_interval_minutes")
    if not isinstance(interval, int) or interval < 1:
        sys.exit("settings.json 'fetch_interval_minutes' must be a positive integer.")

    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable

    fetch_script = os.path.join(PROJECT_DIR, "fetch_data.py")
    start_time = datetime.datetime.now().strftime("%H:%M")

    cmd = [
        "schtasks", "/Create", "/F",
        "/SC", "MINUTE",
        "/MO", str(interval),
        "/TN", TASK_NAME,
        "/TR", f'"{pythonw}" "{fetch_script}"',
        "/ST", start_time,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(
            f"Failed to create scheduled task (exit {result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )

    print(f"Scheduled '{TASK_NAME}' to run fetch_data.py every {interval} minutes.")
    print(result.stdout.strip())


if __name__ == "__main__":
    main()
