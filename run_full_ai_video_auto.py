#!/usr/bin/env python3
"""Navigate to Douyin AI video page, then run the video generation loop."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NAVIGATE_SCRIPT = ROOT / "scripts" / "navigate_ai_video_page.py"
LOOP_SCRIPT = ROOT / "run_ai_video_loop.py"
FALLBACK_PYTHON = Path("/Users/mac/.pyenv/versions/3.10.6/bin/python")


def python_executable() -> str:
    if FALLBACK_PYTHON.exists() and Path(sys.executable) != FALLBACK_PYTHON:
        return str(FALLBACK_PYTHON)
    return sys.executable


def run_step(label: str, command: list[str]) -> int:
    print(f"\n========== {label} ==========", flush=True)
    print(" ".join(command), flush=True)
    completed = subprocess.run(command, cwd=str(ROOT), check=False)
    if completed.returncode != 0:
        print(f"{label} failed with exit code {completed.returncode}", file=sys.stderr, flush=True)
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-command flow: open/navigate Douyin Shop Workbench, then run AI video generation loop."
    )
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run")
    parser.add_argument("--max-runs", type=int, default=1)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--skip-navigation", action="store_true", help="Start loop directly from the current page.")
    parser.add_argument("--navigation-dry-run", action="store_true", help="Observe navigation target without clicking.")
    parser.add_argument("--navigation-timeout", type=float, default=45)
    parser.add_argument("--initial-wait", type=float, default=2.0)
    parser.add_argument("--after-click-wait", type=float, default=3.0)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--left-nav-width", type=int, default=460)
    parser.add_argument("--bundle-id", default="com.temai.im")
    parser.add_argument("--verbose-navigation", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    py = python_executable()

    if not args.skip_navigation:
        navigate_command = [
            py,
            str(NAVIGATE_SCRIPT),
            "--bundle-id",
            args.bundle_id,
            "--timeout",
            str(args.navigation_timeout),
            "--initial-wait",
            str(args.initial_wait),
            "--after-click-wait",
            str(args.after_click_wait),
            "--poll-interval",
            str(args.poll_interval),
            "--left-nav-width",
            str(args.left_nav_width),
        ]
        if args.navigation_dry_run:
            navigate_command.append("--dry-run")
        if args.verbose_navigation:
            navigate_command.append("--verbose")

        code = run_step("navigate to AI智能成片 page", navigate_command)
        if code != 0:
            return code

    loop_command = [
        py,
        str(LOOP_SCRIPT),
        "--mode",
        args.mode,
        "--max-runs",
        str(args.max_runs),
        "--config",
        str(Path(args.config).expanduser()),
    ]
    return run_step("run AI video loop", loop_command)


if __name__ == "__main__":
    raise SystemExit(main())
