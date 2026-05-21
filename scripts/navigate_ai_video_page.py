#!/usr/bin/env python3
"""Open Douyin Shop Workbench and navigate to the AI video page.

This script intentionally stops at the "AI智能成片" page. It does not select
products, generate videos, publish, or consume quota.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from PIL import Image

try:
    import pyautogui
except Exception as exc:  # pragma: no cover - runtime dependency on macOS
    raise SystemExit(f"pyautogui is required: {exc}") from exc

try:
    import Quartz
    import Vision
except Exception:
    Quartz = None
    Vision = None


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots" / "navigation"
DEFAULT_BUNDLE_ID = "com.temai.im"

READY_TEXTS = ["选择带货商品", "选择商品自动生成视频", "本期仅支持部分商品"]
NAV_TEXTS = ["AI智能成片", "智能成片"]
EXPAND_TEXTS = ["短视频运营", "流量"]
STOP_TEXTS = ["扫码登录", "请登录", "登录过期", "授权登录", "风险提示", "确认支付", "立即支付", "购买额度"]


@dataclass
class OCRItem:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.bbox
        return x + w // 2, y + h // 2


class NavigationError(RuntimeError):
    pass


def setup_logging(verbose: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "navigate_ai_video_page.log", encoding="utf-8"),
        ],
    )


def normalize(text: str) -> str:
    return "".join(str(text).split()).lower()


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def capture(step: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOT_DIR / f"{step}_{timestamp()}.png"
    try:
        image = pyautogui.screenshot()
        image.save(path)
    except Exception as first_error:
        logging.warning("pyautogui screenshot failed: %r; trying screencapture", first_error)
        subprocess.run(["/usr/sbin/screencapture", "-x", str(path)], check=True, timeout=20)
    logging.debug("screenshot saved: %s", path)
    return path


def crop_region(image: Image.Image, region: tuple[int, int, int, int] | None) -> Image.Image:
    if region is None:
        return image
    x, y, w, h = region
    return image.crop((x, y, x + w, y + h))


def ocr(image_path: Path, region: tuple[int, int, int, int] | None = None) -> list[OCRItem]:
    if Quartz is None or Vision is None:
        raise NavigationError("Vision OCR is unavailable. Install/use Python with PyObjC Vision and Quartz support.")

    image = Image.open(image_path).convert("RGB")
    work = crop_region(image, region)
    tmp = image_path.with_name(image_path.stem + "_ocr.png")
    work.save(tmp)

    encoded = str(tmp).encode("utf-8")
    url = Quartz.CFURLCreateFromFileSystemRepresentation(None, encoded, len(encoded), False)
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if cg_image is None:
        return []

    observations = []

    def handler(request, error) -> None:
        if error is None:
            observations.extend(list(request.results() or []))

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    try:
        request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
    except Exception:
        pass

    ok, _error = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {}).performRequests_error_([request], None)
    if not ok:
        return []

    ox, oy = (region[0], region[1]) if region else (0, 0)
    width, height = work.size
    items: list[OCRItem] = []
    for obs in observations:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        box = obs.boundingBox()
        x = int(box.origin.x * width) + ox
        h = int(box.size.height * height)
        y = int((1.0 - box.origin.y - box.size.height) * height) + oy
        w = int(box.size.width * width)
        items.append(OCRItem(str(candidate.string()), float(candidate.confidence()), (x, y, w, h)))
    return items


def contains_any(items: Iterable[OCRItem], texts: Iterable[str]) -> bool:
    haystack = normalize(" ".join(item.text for item in items))
    return any(normalize(text) in haystack for text in texts)


def find_text(items: Iterable[OCRItem], texts: Iterable[str], *, exact: bool = False) -> OCRItem | None:
    normalized = [(normalize(item.text), item) for item in items]
    for text in texts:
        needle = normalize(text)
        for haystack, item in normalized:
            if needle and ((haystack == needle) if exact else (needle in haystack)):
                return item
    return None


def click_item(item: OCRItem, dry_run: bool, label: str) -> None:
    x, y = item.center
    logging.info("%s at %s text=%r dry_run=%s", label, (x, y), item.text, dry_run)
    if dry_run:
        return
    pyautogui.moveTo(x, y, duration=0.12)
    pyautogui.click()


def open_app(bundle_id: str, dry_run: bool) -> None:
    logging.info("open app bundle_id=%s dry_run=%s", bundle_id, dry_run)
    subprocess.run(["open", "-b", bundle_id], check=False, timeout=10)


def scan(step: str) -> tuple[Path, list[OCRItem]]:
    path = capture(step)
    items = ocr(path)
    logging.debug("ocr items=%s", len(items))
    return path, items


def page_is_ready(items: list[OCRItem]) -> bool:
    return contains_any(items, ["智能成片"]) and contains_any(items, READY_TEXTS)


def navigate(args: argparse.Namespace) -> int:
    pyautogui.FAILSAFE = True
    open_app(args.bundle_id, args.dry_run)
    time.sleep(args.initial_wait)

    deadline = time.time() + args.timeout
    clicked_nav = False
    clicked_expand = False

    while time.time() < deadline:
        _path, items = scan("observe")
        if contains_any(items, STOP_TEXTS):
            raise NavigationError("detected login/auth/risk/payment-related text; manual handling required")
        if page_is_ready(items):
            logging.info("AI智能成片 page is ready")
            return 0

        # Prefer the left navigation area to avoid clicking historical result text.
        left_items = [item for item in items if item.bbox[0] < args.left_nav_width and item.bbox[1] > 80]
        nav_item = find_text(left_items, NAV_TEXTS, exact=True)
        if nav_item and not clicked_nav:
            click_item(nav_item, args.dry_run, "click AI智能成片 navigation")
            clicked_nav = True
            time.sleep(args.after_click_wait)
            if args.dry_run:
                return 0
            continue

        expand_item = find_text(left_items, EXPAND_TEXTS)
        if expand_item and not clicked_expand:
            click_item(expand_item, args.dry_run, "click navigation group")
            clicked_expand = True
            time.sleep(args.after_click_wait)
            if args.dry_run:
                return 0
            continue

        logging.info("waiting for AI智能成片 navigation/page")
        time.sleep(args.poll_interval)

    raise NavigationError(f"AI智能成片 page was not reached within {args.timeout:.0f}s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Navigate Douyin Shop Workbench to AI智能成片 page.")
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--initial-wait", type=float, default=2.0)
    parser.add_argument("--after-click-wait", type=float, default=3.0)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--left-nav-width", type=int, default=460)
    parser.add_argument("--dry-run", action="store_true", help="Observe and report intended click without changing UI state.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose)
    try:
        return navigate(args)
    except NavigationError as exc:
        logging.error("%s", exc)
        return 2
    except Exception:
        logging.exception("navigation failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
