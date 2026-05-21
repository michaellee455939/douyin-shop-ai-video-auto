#!/usr/bin/env python3
"""Find the "巨量千川" top-nav entry in Douyin Shop Workbench.

The script captures the top navigation area, OCRs it with the same engine as
run_ai_video_loop.py, and prints the matched text coordinate. It can optionally
click the matched nav item or the refresh button inferred from the nav item's
fixed offset in the current layout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_ai_video_loop import (  # noqa: E402
    CONFIG_PATH,
    SCREENSHOT_DIR,
    activate_target_app,
    click_mouse,
    image_point_to_screen,
    load_config,
    notify_script_finished,
    run_ocr,
    scale_region,
    screen_size,
    screenshot,
    setup_logging,
)


TARGET_TEXT = "巨量千川"

# Region format follows config.json: x, y, width, height, measured on
# design_resolution. It covers the browser/app top nav where "巨量千川" appears.
DEFAULT_TOP_NAV_REGION = [0, 70, 900, 80]

# In the provided 1920x1080 screenshot, "巨量千川" center is about (392, 96)
# and the refresh button center is about (68, 97).
REFRESH_OFFSET_FROM_TARGET = [-324, 1]


def normalize_text(text: str) -> str:
    return re.sub(r"[\s·。.,，、:：|丨]+", "", text)


def scale_offset(offset: Iterable[float], cfg: dict[str, Any]) -> tuple[int, int]:
    sx, sy = screen_size()
    dw, dh = cfg["design_resolution"]
    x, y = offset
    return int(round(float(x) * sx / dw)), int(round(float(y) * sy / dh))


def find_target(items: list[Any], target_text: str) -> Any | None:
    needle = normalize_text(target_text)
    for item in items:
        haystack = normalize_text(item.text)
        if needle and (needle in haystack or haystack in needle):
            return item
    return None


def item_to_dict(item: Any) -> dict[str, Any]:
    return {
        "text": item.text,
        "confidence": round(float(item.confidence), 4),
        "box": list(item.box),
        "center": list(item.center),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find or click Douyin Shop top-nav 巨量千川.")
    parser.add_argument("--target-text", default=TARGET_TEXT, help="Text to find. Default: 巨量千川")
    parser.add_argument(
        "--top-nav-region",
        nargs=4,
        type=int,
        default=DEFAULT_TOP_NAV_REGION,
        metavar=("X", "Y", "W", "H"),
        help="OCR region in design-resolution coordinates.",
    )
    parser.add_argument("--click-target", action="store_true", help="Click the recognized 巨量千川 nav item.")
    parser.add_argument("--click-refresh", action="store_true", help="Click refresh button inferred from 巨量千川 coordinate.")
    parser.add_argument("--activate", action="store_true", help="Activate 抖店工作台 before capture.")
    parser.add_argument("--no-notify", action="store_true", help="Do not send a desktop notification when done.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.json.")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    # Keep the existing project config loader behavior.
    import run_ai_video_loop

    run_ai_video_loop.CONFIG_PATH = Path(args.config).expanduser().resolve()
    cfg = load_config()

    if args.activate:
        activate_target_app(cfg)

    shot = SCREENSHOT_DIR / f"find_qianchuan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    screenshot(shot)

    region = scale_region(args.top_nav_region, cfg)
    items = run_ocr(shot, region, cfg)
    found = find_target(items, args.target_text)

    output: dict[str, Any] = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "screenshot": str(shot),
        "screen_size": list(screen_size()),
        "target_text": args.target_text,
        "top_nav_region": list(region),
        "found": found is not None,
        "all_texts": [item_to_dict(item) for item in items],
    }

    if found is not None:
        target_point = image_point_to_screen(found.center, shot)
        refresh_offset = scale_offset(REFRESH_OFFSET_FROM_TARGET, cfg)
        refresh_point = (target_point[0] + refresh_offset[0], target_point[1] + refresh_offset[1])

        output["matched"] = item_to_dict(found)
        output["target_coordinate"] = list(target_point)
        output["refresh_coordinate_by_offset"] = list(refresh_point)
        output["refresh_offset_from_target"] = list(refresh_offset)

        if args.click_target and args.click_refresh:
            raise SystemExit("Only pass one of --click-target or --click-refresh.")
        if args.click_target:
            click_mouse(target_point, cfg)
            time.sleep(float(cfg.get("after_click_delay_seconds", 0.8)))
            output["clicked"] = "target"
        elif args.click_refresh:
            click_mouse(refresh_point, cfg)
            time.sleep(float(cfg.get("after_click_delay_seconds", 0.8)))
            output["clicked"] = "refresh"

    print(json.dumps(output, ensure_ascii=False, indent=2))

    if not args.no_notify:
        if found is None:
            notify_script_finished(cfg, "巨量千川查找结束", "没有找到巨量千川")
        else:
            notify_script_finished(cfg, "巨量千川查找结束", f"坐标：{output['target_coordinate']}")

    return 0 if found is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
