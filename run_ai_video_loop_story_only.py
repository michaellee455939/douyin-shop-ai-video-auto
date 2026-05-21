#!/usr/bin/env python3
"""Run the Douyin AI video loop with the story-only video type config."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "run_ai_video_loop.py"
CONFIG = ROOT / "config_story_only.json"


def main() -> None:
    args = [sys.executable, str(SCRIPT), "--config", str(CONFIG), *sys.argv[1:]]
    os.execv(sys.executable, args)


if __name__ == "__main__":
    main()
