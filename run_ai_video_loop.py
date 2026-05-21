#!/usr/bin/env python3
"""Automate Douyin Shop Workbench AI video generation from the current page.

The script starts from the already-open "AI智能成片" page. It uses macOS
Vision OCR when available, and falls back to configurable coordinates.
"""

from __future__ import annotations

import argparse
import base64
import atexit
import fcntl
import json
import logging
import multiprocessing as mp
import os
import queue
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import pyautogui
    from PIL import Image
    import numpy as np
except ModuleNotFoundError as exc:
    fallback_python = Path("/Users/mac/.pyenv/versions/3.10.6/bin/python")
    if fallback_python.exists() and Path(sys.executable) != fallback_python:
        os.execv(str(fallback_python), [str(fallback_python), *sys.argv])
    raise SystemExit(
        f"Missing Python dependency: {exc.name}. Try:\n"
        f"  {fallback_python} {Path(__file__).name} --mode dry-run --max-runs 1"
    )

try:
    import Quartz
    import Vision
except Exception:  # pragma: no cover - depends on macOS PyObjC install
    Quartz = None
    Vision = None


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "runtime_state.json"
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
ERROR_DIR = LOG_DIR / "error_screenshots"
LOCK_PATH = ROOT / ".run_ai_video_loop.lock"
ACTIVE_SCREENSHOT_DIR = SCREENSHOT_DIR
ACTIVE_ERROR_DIR = ERROR_DIR
_PADDLE_OCR: Any | None = None
_PADDLE_WORKER: Any | None = None
LOG = logging.getLogger("douyin_ai_video_auto")
DEFAULT_REFRESH_TEXT = "巨量千川"
DEFAULT_REFRESH_OFFSET_FROM_TEXT = [-328, 0]
NVIDIA_QUOTA_MODEL = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
NVIDIA_API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"


@dataclass
class OCRItem:
    text: str
    confidence: float
    box: tuple[int, int, int, int]  # x, y, w, h

    @property
    def center(self) -> tuple[int, int]:
        x, y, w, h = self.box
        return x + w // 2, y + h // 2


class AutomationError(RuntimeError):
    pass


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ERROR_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    if LOG.handlers:
        return
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOG.addHandler(file_handler)
    LOG.addHandler(stream_handler)


def acquire_run_lock() -> Any:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_file = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise AutomationError("another run_ai_video_loop.py instance is already running")
    lock_file.write(f"pid={os.getpid()} started_at={datetime.now().isoformat(timespec='seconds')}\n")
    lock_file.flush()
    return lock_file


def free_space_gb(path: Path) -> float:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize / (1024 ** 3)


def swap_used_gb() -> float | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception as exc:
        LOG.warning("failed to read swap usage: %r", exc)
        return None
    match = re.search(r"used\s*=\s*([0-9.]+)M", result.stdout)
    if not match:
        return None
    return float(match.group(1)) / 1024


def configure_artifact_dirs(cfg: dict[str, Any]) -> None:
    global ACTIVE_SCREENSHOT_DIR, ACTIVE_ERROR_DIR
    storage = cfg.get("screenshot_storage", {})
    preferred_base = storage.get("preferred_base_dir")
    if preferred_base:
        base = Path(str(preferred_base)).expanduser()
        preferred_screenshot_dir = base / "screenshots"
        preferred_error_dir = base / "error_screenshots"
        try:
            preferred_screenshot_dir.mkdir(parents=True, exist_ok=True)
            preferred_error_dir.mkdir(parents=True, exist_ok=True)
            probe = preferred_screenshot_dir / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            ACTIVE_SCREENSHOT_DIR = preferred_screenshot_dir
            ACTIVE_ERROR_DIR = preferred_error_dir
        except Exception as exc:
            if storage.get("fallback_to_local", True):
                ACTIVE_SCREENSHOT_DIR = SCREENSHOT_DIR
                ACTIVE_ERROR_DIR = ERROR_DIR
                LOG.warning("preferred screenshot storage unavailable; using local storage: %r", exc)
            else:
                raise AutomationError(f"preferred screenshot storage unavailable: {exc!r}")
    else:
        ACTIVE_SCREENSHOT_DIR = SCREENSHOT_DIR
        ACTIVE_ERROR_DIR = ERROR_DIR
    LOG.info("screenshot directory: %s", ACTIVE_SCREENSHOT_DIR)
    LOG.info("error screenshot directory: %s", ACTIVE_ERROR_DIR)


def cleanup_png_dir(directory: Path, keep_latest: int, max_age_days: float) -> int:
    if keep_latest < 0:
        keep_latest = 0
    if max_age_days <= 0:
        max_age_days = 36500
    if not directory.exists():
        return 0
    now = time.time()
    max_age_seconds = max_age_days * 24 * 60 * 60
    files = sorted(directory.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    deleted = 0
    for index, path in enumerate(files):
        try:
            age_expired = now - path.stat().st_mtime > max_age_seconds
            over_limit = index >= keep_latest
            if age_expired or over_limit:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            LOG.warning("failed to delete old screenshot %s: %r", path, exc)
    return deleted


def run_safety_checks(cfg: dict[str, Any], *, startup: bool) -> None:
    configure_artifact_dirs(cfg)
    safety = cfg.get("safety", {})
    if safety.get("cleanup_screenshots", True):
        deleted = cleanup_png_dir(
            ACTIVE_SCREENSHOT_DIR,
            int(safety.get("keep_latest_screenshots", 400)),
            float(safety.get("max_screenshot_age_days", 3)),
        )
        deleted += cleanup_png_dir(
            ACTIVE_ERROR_DIR,
            int(safety.get("keep_latest_error_screenshots", 100)),
            float(safety.get("max_error_screenshot_age_days", 14)),
        )
        if deleted:
            LOG.info("deleted %s old screenshot files during safety cleanup", deleted)

    min_free_gb = float(safety.get("min_free_disk_gb", 12))
    available_gb = free_space_gb(ROOT)
    LOG.info("free disk space: %.1f GiB (minimum %.1f GiB)", available_gb, min_free_gb)
    if available_gb < min_free_gb:
        phase = "startup" if startup else "runtime"
        raise AutomationError(
            f"low disk space at {phase}: {available_gb:.1f} GiB available, "
            f"minimum is {min_free_gb:.1f} GiB"
        )

    max_swap_gb = float(safety.get("max_swap_used_gb", 3.0))
    used_swap_gb = swap_used_gb()
    if used_swap_gb is not None:
        LOG.info("swap used: %.1f GiB (maximum %.1f GiB)", used_swap_gb, max_swap_gb)
        if used_swap_gb > max_swap_gb:
            phase = "startup" if startup else "runtime"
            raise AutomationError(
                f"high memory pressure at {phase}: swap used {used_swap_gb:.1f} GiB, "
                f"maximum is {max_swap_gb:.1f} GiB"
            )


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        LOG.warning("failed to load runtime state: %r", exc)
        return {}


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


def remember_successful_target(label: str, point: tuple[int, int], source: str) -> None:
    state = load_state()
    state.setdefault("successful_targets", {})[label] = {
        "point": [int(point[0]), int(point[1])],
        "source": source,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_state(state)
    LOG.info("remembered successful target %s at %s source=%s", label, point, source)


def get_remembered_target(label: str) -> tuple[int, int] | None:
    target = load_state().get("successful_targets", {}).get(label)
    point = target.get("point") if isinstance(target, dict) else None
    if isinstance(point, list) and len(point) == 2:
        return int(point[0]), int(point[1])
    return None


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def applescript_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def notify_script_finished(cfg: dict[str, Any], title: str, message: str) -> None:
    notify_cfg = cfg.get("notifications", {})
    if not notify_cfg.get("enabled", True):
        return

    if notify_cfg.get("desktop", True):
        script = (
            f'display notification "{applescript_quote(message)}" '
            f'with title "{applescript_quote(title)}"'
        )
        try:
            subprocess.run(["/usr/bin/osascript", "-e", script], check=False, timeout=5)
            LOG.info("desktop notification sent: %s - %s", title, message)
        except Exception as exc:
            LOG.warning("desktop notification failed: %r", exc)

    if notify_cfg.get("sound", True):
        sound_file = str(notify_cfg.get("sound_file", "/System/Library/Sounds/Glass.aiff"))
        try:
            subprocess.run(["/usr/bin/afplay", sound_file], check=False, timeout=8)
            LOG.info("finish sound played: %s", sound_file)
        except Exception as exc:
            LOG.warning("finish sound failed: %r", exc)


def activate_target_app(cfg: dict[str, Any]) -> None:
    app_cfg = cfg.get("app", {})
    if not app_cfg.get("activate_before_run", True):
        return
    app_name = str(app_cfg.get("name", "抖店工作台"))
    script = (
        f'tell application "{applescript_quote(app_name)}" to activate\n'
        "delay 0.5\n"
        f'tell application "System Events" to tell process "{applescript_quote(app_name)}"\n'
        "set frontmost to true\n"
        "end tell"
    )
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], check=False, timeout=8)
        time.sleep(float(app_cfg.get("settle_seconds", 1.0)))
        LOG.info("activated target app: %s", app_name)
    except Exception as exc:
        LOG.warning("failed to activate target app %s: %r", app_name, exc)


def close_target_app(cfg: dict[str, Any]) -> None:
    app_cfg = cfg.get("app", {})
    if not app_cfg.get("quit_after_success", False):
        return
    app_name = str(app_cfg.get("name", "抖店工作台"))
    script = f'tell application "{applescript_quote(app_name)}" to quit'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], check=False, timeout=10)
        LOG.info("quit target app after successful run: %s", app_name)
    except Exception as exc:
        LOG.warning("failed to quit target app %s: %r", app_name, exc)


def click_mouse(point: tuple[int, int], cfg: dict[str, Any]) -> None:
    x, y = int(round(point[0])), int(round(point[1]))
    backend = str(cfg.get("click_backend", "quartz")).lower()
    LOG.info("click backend=%s point=(%s, %s)", backend, x, y)

    if backend == "quartz" and Quartz is not None:
        down = Quartz.CGEventCreateMouseEvent(
            None,
            Quartz.kCGEventLeftMouseDown,
            (x, y),
            Quartz.kCGMouseButtonLeft,
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        time.sleep(0.05)
        up = Quartz.CGEventCreateMouseEvent(
            None,
            Quartz.kCGEventLeftMouseUp,
            (x, y),
            Quartz.kCGMouseButtonLeft,
        )
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        return

    pyautogui.moveTo(x, y, duration=cfg.get("move_duration_seconds", 0.15))
    pyautogui.click()


def screenshot(path: Path) -> Image.Image:
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    screenshot_cfg = cfg.get("screenshot", {})
    retries = int(screenshot_cfg.get("retries", 3))
    retry_delay = float(screenshot_cfg.get("retry_delay_seconds", 1.0))
    screencapture_timeout = float(screenshot_cfg.get("screencapture_timeout_seconds", 20))
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            img = pyautogui.screenshot()
            img.save(path)
            return img
        except Exception as first_error:
            last_error = first_error
            LOG.debug("pyautogui screenshot failed on attempt %s/%s: %r", attempt, retries, first_error)
            try:
                subprocess.run(
                    ["/usr/sbin/screencapture", "-x", str(path)],
                    check=True,
                    timeout=screencapture_timeout,
                )
                return Image.open(path)
            except Exception as second_error:
                last_error = second_error
                LOG.warning("screencapture failed on attempt %s/%s: %r", attempt, retries, second_error)
                if attempt < retries:
                    time.sleep(retry_delay)

    raise AutomationError(
        "screenshot failed. Grant Screen Recording permission to Terminal/Codex/Python. "
        f"last_error={last_error!r}"
    )


def screen_size() -> tuple[int, int]:
    size = pyautogui.size()
    width, height = int(size.width), int(size.height)
    if width > 0 and height > 0:
        return width, height
    if Quartz is not None:
        display_id = Quartz.CGMainDisplayID()
        return int(Quartz.CGDisplayPixelsWide(display_id)), int(Quartz.CGDisplayPixelsHigh(display_id))
    return width, height


def scale_point(point: Iterable[float], cfg: dict[str, Any]) -> tuple[int, int]:
    sx, sy = screen_size()
    dw, dh = cfg["design_resolution"]
    if sx <= 0 or sy <= 0:
        sx, sy = int(dw), int(dh)
    x, y = point
    return int(round(x * sx / dw)), int(round(y * sy / dh))


def scale_offset(offset: Iterable[float], cfg: dict[str, Any]) -> tuple[int, int]:
    sx, sy = screen_size()
    dw, dh = cfg["design_resolution"]
    if sx <= 0 or sy <= 0:
        sx, sy = int(dw), int(dh)
    x, y = offset
    return int(round(float(x) * sx / dw)), int(round(float(y) * sy / dh))


def scale_region(region: Iterable[float], cfg: dict[str, Any]) -> tuple[int, int, int, int]:
    sx, sy = screen_size()
    dw, dh = cfg["design_resolution"]
    if sx <= 0 or sy <= 0:
        sx, sy = int(dw), int(dh)
    x, y, w, h = region
    return (
        int(round(x * sx / dw)),
        int(round(y * sy / dh)),
        int(round(w * sx / dw)),
        int(round(h * sy / dh)),
    )


def image_point_to_screen(point: tuple[int, int], image_path: Path) -> tuple[int, int]:
    """Convert screenshot pixel coordinates to PyAutoGUI screen coordinates."""
    sx, sy = screen_size()
    try:
        with Image.open(image_path) as img:
            iw, ih = img.size
    except Exception:
        return point
    if sx <= 0 or sy <= 0 or iw <= 0 or ih <= 0:
        return point
    return int(round(point[0] * sx / iw)), int(round(point[1] * sy / ih))


def crop_region(img: Image.Image, region: tuple[int, int, int, int] | None) -> Image.Image:
    if region is None:
        return img
    x, y, w, h = region
    return img.crop((x, y, x + w, y + h))


def paddle_worker_main(requests: Any, responses: Any, paddle_cfg: dict[str, Any]) -> None:
    try:
        from paddleocr import PaddleOCR
        from PIL import Image as WorkerImage
        import numpy as worker_np

        kwargs = {
            "use_angle_cls": bool(paddle_cfg.get("use_angle_cls", True)),
            "lang": paddle_cfg.get("lang", "ch"),
            "show_log": False,
        }
        if paddle_cfg.get("det_limit_side_len"):
            kwargs["det_limit_side_len"] = int(paddle_cfg["det_limit_side_len"])
        ocr = PaddleOCR(**kwargs)

        while True:
            request = requests.get()
            if request is None:
                break
            request_id, image_path, region = request
            try:
                img = WorkerImage.open(image_path).convert("RGB")
                if region is not None:
                    x, y, w, h = region
                    img = img.crop((x, y, x + w, y + h))
                    ox, oy = x, y
                else:
                    ox, oy = 0, 0
                result = ocr.ocr(worker_np.array(img), cls=True)
                items: list[dict[str, Any]] = []
                for page in result or []:
                    if not page:
                        continue
                    for line in page:
                        if not line or len(line) < 2:
                            continue
                        raw_box, raw_text = line[0], line[1]
                        xs = [float(p[0]) for p in raw_box]
                        ys = [float(p[1]) for p in raw_box]
                        items.append(
                            {
                                "text": str(raw_text[0]),
                                "confidence": float(raw_text[1]) if len(raw_text) > 1 else 0.0,
                                "box": [
                                    int(min(xs)) + ox,
                                    int(min(ys)) + oy,
                                    int(max(xs) - min(xs)),
                                    int(max(ys) - min(ys)),
                                ],
                            }
                        )
                responses.put((request_id, True, items, None))
            except Exception as exc:
                responses.put((request_id, False, [], repr(exc)))
    except Exception as exc:
        responses.put(("__startup__", False, [], repr(exc)))


def stop_paddle_worker() -> None:
    global _PADDLE_WORKER
    worker = _PADDLE_WORKER
    _PADDLE_WORKER = None
    if not worker:
        return
    process = worker.get("process")
    requests = worker.get("requests")
    try:
        if process is not None and process.is_alive():
            requests.put(None)
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        LOG.info("PaddleOCR worker stopped")
    except Exception as exc:
        LOG.warning("failed to stop PaddleOCR worker cleanly: %r", exc)


atexit.register(stop_paddle_worker)


def get_paddle_worker(cfg: dict[str, Any]) -> dict[str, Any]:
    global _PADDLE_WORKER
    if _PADDLE_WORKER and _PADDLE_WORKER["process"].is_alive():
        return _PADDLE_WORKER

    paddle_cfg = cfg.get("ocr", {}).get("paddle", {})
    ctx = mp.get_context("spawn")
    requests = ctx.Queue()
    responses = ctx.Queue()
    process = ctx.Process(target=paddle_worker_main, args=(requests, responses, paddle_cfg), daemon=True)
    process.start()
    _PADDLE_WORKER = {"process": process, "requests": requests, "responses": responses, "started_at": time.time()}
    LOG.info("PaddleOCR worker started: pid=%s", process.pid)
    return _PADDLE_WORKER


def get_paddle_ocr(cfg: dict[str, Any]) -> Any | None:
    global _PADDLE_OCR
    if not cfg.get("ocr", {}).get("enabled", True):
        LOG.info("PaddleOCR skipped: OCR is disabled in config")
        return None
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        LOG.debug("PaddleOCR import failed: %r", exc)
        return None
    if _PADDLE_OCR is not None:
        return _PADDLE_OCR

    paddle_cfg = cfg.get("ocr", {}).get("paddle", {})
    kwargs = {
        "use_angle_cls": bool(paddle_cfg.get("use_angle_cls", True)),
        "lang": paddle_cfg.get("lang", "ch"),
        "show_log": False,
    }
    if paddle_cfg.get("det_limit_side_len"):
        kwargs["det_limit_side_len"] = int(paddle_cfg["det_limit_side_len"])
    LOG.info("initializing PaddleOCR: %s", kwargs)
    _PADDLE_OCR = PaddleOCR(**kwargs)
    logging.disable(logging.NOTSET)
    setup_logging()
    return _PADDLE_OCR


def paddle_ocr(image_path: Path, region: tuple[int, int, int, int] | None, cfg: dict[str, Any]) -> list[OCRItem]:
    paddle_cfg = cfg.get("ocr", {}).get("paddle", {})
    if paddle_cfg.get("worker_enabled", True):
        worker = get_paddle_worker(cfg)
        request_id = f"{os.getpid()}-{time.time_ns()}"
        timeout = float(paddle_cfg.get("worker_timeout_seconds", 90))
        worker["requests"].put((request_id, str(image_path), region))
        deadline = time.time() + timeout
        startup_error: str | None = None
        while time.time() < deadline:
            try:
                response_id, ok, raw_items, error = worker["responses"].get(timeout=1)
            except queue.Empty:
                if not worker["process"].is_alive():
                    stop_paddle_worker()
                    raise AutomationError("PaddleOCR worker exited unexpectedly")
                continue
            if response_id == "__startup__":
                startup_error = str(error)
                break
            if response_id != request_id:
                LOG.warning("ignored stale PaddleOCR worker response: %s", response_id)
                continue
            if not ok:
                raise AutomationError(f"PaddleOCR worker failed: {error}")
            return [
                OCRItem(
                    text=str(item["text"]),
                    confidence=float(item["confidence"]),
                    box=tuple(int(v) for v in item["box"]),
                )
                for item in raw_items
            ]
        stop_paddle_worker()
        raise AutomationError(f"PaddleOCR worker timed out or failed: {startup_error or timeout}")

    ocr = get_paddle_ocr(cfg)
    if ocr is None:
        LOG.debug("PaddleOCR is not available")
        return []

    img = Image.open(image_path).convert("RGB")
    work_img = crop_region(img, region)
    img_np = np.array(work_img)
    result = ocr.ocr(img_np, cls=True)
    if not result:
        return []

    ox, oy = (region[0], region[1]) if region else (0, 0)
    items: list[OCRItem] = []
    for page in result:
        if not page:
            continue
        for line in page:
            if not line or len(line) < 2:
                continue
            raw_box, raw_text = line[0], line[1]
            text = str(raw_text[0])
            confidence = float(raw_text[1]) if len(raw_text) > 1 else 0.0
            xs = [float(p[0]) for p in raw_box]
            ys = [float(p[1]) for p in raw_box]
            left = int(min(xs)) + ox
            top = int(min(ys)) + oy
            width = int(max(xs) - min(xs))
            height = int(max(ys) - min(ys))
            items.append(OCRItem(text=text, confidence=confidence, box=(left, top, width, height)))
    return items


def vision_ocr(image_path: Path, region: tuple[int, int, int, int] | None = None) -> list[OCRItem]:
    if Quartz is None or Vision is None:
        return []

    img = Image.open(image_path)
    work_img = crop_region(img, region)
    tmp_path = image_path.with_name(image_path.stem + "_ocr.png")
    work_img.save(tmp_path)

    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, str(tmp_path).encode("utf-8"), len(str(tmp_path).encode("utf-8")), False
    )
    source = Quartz.CGImageSourceCreateWithURL(url, None)
    cg_image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if cg_image is None:
        return []

    results: list[Any] = []

    def handler(request, error):
        if error is None:
            results.extend(list(request.results() or []))

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(handler)
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(True)
    try:
        request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
    except Exception:
        pass

    image_handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg_image, {})
    ok, err = image_handler.performRequests_error_([request], None)
    if not ok:
        LOG.debug("Vision OCR failed: %s", err)
        return []

    ox, oy = (region[0], region[1]) if region else (0, 0)
    width, height = work_img.size
    items: list[OCRItem] = []
    for obs in results:
        candidates = obs.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string())
        conf = float(candidate.confidence())
        bb = obs.boundingBox()
        x = int(bb.origin.x * width) + ox
        h = int(bb.size.height * height)
        y = int((1.0 - bb.origin.y - bb.size.height) * height) + oy
        w = int(bb.size.width * width)
        items.append(OCRItem(text=text, confidence=conf, box=(x, y, w, h)))
    return items


def run_ocr(image_path: Path, region: tuple[int, int, int, int] | None, cfg: dict[str, Any]) -> list[OCRItem]:
    ocr_cfg = cfg.get("ocr", {})
    if not ocr_cfg.get("enabled", True):
        LOG.info("OCR skipped for %s: disabled in config", image_path.name)
        return []
    providers = [ocr_cfg.get("primary", "paddle"), *ocr_cfg.get("fallbacks", ["vision"])]
    seen: set[str] = set()
    for provider in providers:
        provider = str(provider).lower()
        if provider in seen:
            continue
        seen.add(provider)
        try:
            if provider == "paddle":
                items = paddle_ocr(image_path, region, cfg)
            elif provider == "vision":
                items = vision_ocr(image_path, region)
            else:
                LOG.warning("unknown OCR provider ignored: %s", provider)
                continue
            if items:
                LOG.info("OCR provider %s recognized %d text items", provider, len(items))
                return items
            LOG.debug("OCR provider %s returned no text", provider)
        except Exception as exc:
            LOG.warning("OCR provider %s failed: %r", provider, exc)
    return []


def normalize_text(text: str) -> str:
    return re.sub(r"[\s·。.,，、:：|丨]+", "", text)


def find_text(items: list[OCRItem], candidates: Iterable[str]) -> OCRItem | None:
    normalized_items = [(normalize_text(i.text), i) for i in items]
    for candidate in candidates:
        needle = normalize_text(candidate)
        for haystack, item in normalized_items:
            if needle and needle in haystack:
                return item
    return None


def resolve_region(cfg: dict[str, Any], region_name: str | None) -> tuple[int, int, int, int] | None:
    if not region_name:
        return None
    raw = cfg["regions"].get(region_name)
    if raw is None:
        return None
    return scale_region(raw, cfg)


def capture_for_step(run_no: int, step: str) -> Path:
    filename = f"run_{run_no:03d}_{step}_{stamp()}.png"
    primary = ACTIVE_SCREENSHOT_DIR / filename
    fallback = SCREENSHOT_DIR / filename
    try:
        screenshot(primary)
        LOG.info("screenshot saved: %s", primary)
        return primary
    except Exception as exc:
        if primary == fallback:
            raise
        LOG.warning("preferred screenshot path failed, falling back to local: %s error=%r", primary, exc)
        screenshot(fallback)
        LOG.info("screenshot saved: %s", fallback)
        return fallback


def locate_text_in_region(
    cfg: dict[str, Any],
    run_no: int,
    label: str,
    texts: Iterable[str],
    region: tuple[int, int, int, int] | None,
) -> tuple[Path, OCRItem | None, list[OCRItem]]:
    shot = capture_for_step(run_no, label)
    items = run_ocr(shot, region, cfg)
    return shot, find_text(items, texts), items


def refresh_page(cfg: dict[str, Any], run_no: int, reason: str, dry_run: bool = False) -> bool:
    refresh_cfg = cfg.get("refresh", {})
    if not refresh_cfg.get("enabled", False):
        LOG.info("page refresh skipped for %s: disabled", reason)
        return False

    texts = refresh_cfg.get("anchor_texts", [DEFAULT_REFRESH_TEXT])
    raw_region = refresh_cfg.get("top_nav_region", [0, 45, 900, 70])
    region = scale_region(raw_region, cfg)
    fallback_point = refresh_cfg.get("fallback_point")
    allow_fallback = bool(refresh_cfg.get("allow_coordinate_fallback", True))
    wait_seconds = float(refresh_cfg.get("after_refresh_wait_seconds", 2.0))

    if cfg.get("prefer_coordinate_fallback", False) and allow_fallback and fallback_point:
        point = scale_point(fallback_point, cfg)
        LOG.info("page refresh for %s using preferred fallback point=%s", reason, point)
        if dry_run:
            LOG.info("[dry-run] would refresh page at %s", point)
            return True
        click_mouse(point, cfg)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        LOG.info("page refreshed for %s", reason)
        return True

    try:
        shot, item, _items = locate_text_in_region(cfg, run_no, f"refresh_{reason}", texts, region)
    except Exception as exc:
        LOG.warning("refresh OCR capture failed for %s: %r", reason, exc)
        item = None
        shot = None

    point: tuple[int, int] | None = None
    source = "ocr_offset"
    if item and shot is not None:
        anchor = image_point_to_screen(item.center, shot)
        offset = scale_offset(refresh_cfg.get("offset_from_anchor", DEFAULT_REFRESH_OFFSET_FROM_TEXT), cfg)
        point = (anchor[0] + offset[0], anchor[1] + offset[1])
        LOG.info("refresh anchor %s found at %s; refresh point=%s", item.text, anchor, point)

    if point is None and allow_fallback and fallback_point:
        point = scale_point(fallback_point, cfg)
        source = "fallback"
        LOG.warning("refresh anchor not found; using fallback point=%s", point)

    if point is None:
        LOG.warning("page refresh skipped for %s: anchor not found and no fallback", reason)
        return False

    LOG.info("page refresh for %s located by %s at %s", reason, source, point)
    if dry_run:
        LOG.info("[dry-run] would refresh page at %s", point)
        return True

    click_mouse(point, cfg)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    LOG.info("page refreshed for %s", reason)
    return True


def click_target(
    cfg: dict[str, Any],
    run_no: int,
    target_name: str,
    dry_run: bool,
    required: bool = True,
) -> tuple[int, int] | None:
    target = cfg["targets"][target_name]
    return click_target_spec(cfg, run_no, target_name, target, dry_run, required)


def click_target_spec(
    cfg: dict[str, Any],
    run_no: int,
    label: str,
    target: dict[str, Any],
    dry_run: bool,
    required: bool = True,
) -> tuple[int, int] | None:
    allow_coordinate = target.get("allow_coordinate_fallback", cfg.get("allow_coordinate_fallback", True))
    if cfg.get("prefer_coordinate_fallback", False):
        point: tuple[int, int] | None = None
        source = ""
        if target.get("fallback_to_remembered_success"):
            remembered = get_remembered_target(label)
            if remembered:
                point = remembered
                source = "remembered_success"
        if point is None and target.get("fallback_to_first_product") and target.get("first_product_point"):
            point = scale_point(target["first_product_point"], cfg)
            source = "first_product_fallback"
        if point is None and allow_coordinate and target.get("fallback_point"):
            point = scale_point(target["fallback_point"], cfg)
            source = "fallback"
        if point is not None:
            LOG.info("target %s located by preferred %s at %s", label, source, point)
            if dry_run:
                LOG.info("[dry-run] would click %s at %s", label, point)
                return point
            click_mouse(point, cfg)
            time.sleep(cfg.get("after_click_delay_seconds", 0.8))
            LOG.info("clicked %s at %s", label, point)
            return point

    shot = capture_for_step(run_no, f"before_{label}")
    region = resolve_region(cfg, target.get("region"))
    items = run_ocr(shot, region, cfg)
    item = find_text(items, target.get("texts", []))
    point: tuple[int, int] | None = None
    source = "ocr"

    if item:
        point = image_point_to_screen(item.center, shot)
        if "click_offset" in target:
            ox, oy = scale_point(target["click_offset"], {"design_resolution": cfg["design_resolution"]})
            point = (point[0] + ox, point[1] + oy)
    if point is None and target.get("fallback_to_remembered_success"):
        remembered = get_remembered_target(label)
        if remembered:
            point = remembered
            source = "remembered_success"
    if point is None and target.get("fallback_to_first_product") and target.get("first_product_point"):
        point = scale_point(target["first_product_point"], cfg)
        source = "first_product_fallback"
    if point is None and allow_coordinate and target.get("fallback_point"):
        point = scale_point(target["fallback_point"], cfg)
        source = "fallback"

    if point is None:
        msg = f"target not found: {label}"
        if required:
            save_error(run_no, label, msg)
            raise AutomationError(msg)
        LOG.warning(msg)
        return None

    LOG.info("target %s located by %s at %s", label, source, point)
    if dry_run:
        LOG.info("[dry-run] would click %s at %s", label, point)
        return point

    click_mouse(point, cfg)
    time.sleep(cfg.get("after_click_delay_seconds", 0.8))
    LOG.info("clicked %s at %s", label, point)
    return point


def click_video_type_for_run(cfg: dict[str, Any], run_no: int, dry_run: bool) -> tuple[int, int] | None:
    cycle = cfg.get("video_type_cycle") or []
    if not cycle:
        LOG.info("video_type_cycle is empty; falling back to first_video_type_card")
        return click_target(cfg, run_no, "first_video_type_card", dry_run)
    index = (run_no - 1) % len(cycle)
    target = cycle[index]
    name = target.get("name", f"video_type_{index + 1}")
    LOG.info("selected video type for run %03d: %s (%s/%s)", run_no, name, index + 1, len(cycle))
    return click_target_spec(cfg, run_no, f"video_type_{index + 1}_{name}", target, dry_run)


def save_error(run_no: int, step: str, message: str) -> Path:
    filename = f"run_{run_no:03d}_{step}_{stamp()}.png"
    path = ACTIVE_ERROR_DIR / filename
    try:
        screenshot(path)
        LOG.error("%s; error screenshot saved: %s", message, path)
    except Exception as exc:
        if path != ERROR_DIR / filename:
            fallback = ERROR_DIR / filename
            try:
                screenshot(fallback)
                LOG.error("%s; preferred error screenshot failed: %r; saved fallback: %s", message, exc, fallback)
                return fallback
            except Exception as fallback_exc:
                LOG.error(
                    "%s; additionally failed to save preferred and fallback error screenshots: %r / %r",
                    message,
                    exc,
                    fallback_exc,
                )
        else:
            LOG.error("%s; additionally failed to save error screenshot: %r", message, exc)
    return path


def wait_for_text(
    cfg: dict[str, Any],
    run_no: int,
    texts: Iterable[str],
    timeout: float,
    region_name: str | None,
    label: str,
) -> bool:
    if not cfg.get("ocr", {}).get("enabled", True):
        wait_seconds = float(
            cfg.get("ocr_disabled_waits", {}).get(label, cfg.get("ocr_disabled_waits", {}).get("default", 2.0))
        )
        wait_seconds = max(0.0, min(wait_seconds, timeout))
        LOG.info("wait condition %s using fixed wait %.1fs because OCR is disabled", label, wait_seconds)
        if wait_seconds:
            time.sleep(wait_seconds)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        shot = capture_for_step(run_no, f"wait_{label}")
        items = run_ocr(shot, resolve_region(cfg, region_name), cfg)
        if find_text(items, texts):
            LOG.info("wait condition met: %s", label)
            return True
        poll_seconds = float(cfg.get("performance", {}).get("wait_text_poll_seconds", 2.5))
        time.sleep(max(0.5, poll_seconds))
    save_error(run_no, f"timeout_{label}", f"timeout waiting for {label}")
    return False


def is_generate_button_enabled(image_path: Path, cfg: dict[str, Any]) -> bool:
    readiness = cfg.get("readiness", {})
    region_raw = readiness.get("generate_button_region")
    if not region_raw:
        return True
    region = scale_region(region_raw, cfg)
    img = Image.open(image_path).convert("RGB")
    x, y, w, h = region
    crop = img.crop((x, y, x + w, y + h))
    pixels = list(crop.getdata())
    if not pixels:
        return False
    red = sum(p[0] for p in pixels) / len(pixels)
    blue = sum(p[2] for p in pixels) / len(pixels)
    enabled = blue >= float(readiness.get("enabled_blue_min", 130)) and red <= float(readiness.get("enabled_red_max", 120))
    LOG.info("generate button color check: red=%.1f blue=%.1f enabled=%s", red, blue, enabled)
    return enabled


def wait_after_video_type_selected(cfg: dict[str, Any], run_no: int, dry_run: bool) -> None:
    min_wait_seconds = float(cfg.get("waits", {}).get("after_video_type_select_seconds", 0))
    readiness = cfg.get("readiness", {})
    timeout_seconds = float(readiness.get("material_ready_timeout_seconds", max(min_wait_seconds, 1)))
    if timeout_seconds <= 0:
        return
    LOG.info(
        "waiting for material readiness after video type selection: min_wait=%.1fs timeout=%.1fs",
        min_wait_seconds,
        timeout_seconds,
    )
    start = time.time()
    deadline = start + timeout_seconds
    texts = readiness.get("after_video_type_texts", ["立即生成"])
    loading_texts = readiness.get("loading_texts", [])
    last_enabled = False
    if not cfg.get("ocr", {}).get("enabled", True):
        if min_wait_seconds > 0:
            LOG.info("waiting %.1fs after video type selection before color readiness checks", min_wait_seconds)
            time.sleep(min_wait_seconds)
        shot = capture_for_step(run_no, "wait_material_ready")
        if is_generate_button_enabled(shot, cfg):
            LOG.info("material readiness confirmed by generate button color")
        else:
            LOG.warning("generate button color still looks disabled; proceeding because OCR is disabled")
        return

    while time.time() < deadline:
        shot = capture_for_step(run_no, "wait_material_ready")
        items = run_ocr(shot, resolve_region(cfg, "left_panel"), cfg)
        has_expected_text = bool(find_text(items, texts))
        has_loading_text = bool(find_text(items, loading_texts)) if loading_texts else False
        if readiness.get("generate_button_enabled_check", True):
            last_enabled = is_generate_button_enabled(shot, cfg)
        else:
            last_enabled = True
        elapsed = time.time() - start
        remaining = max(0.0, deadline - time.time())
        LOG.info(
            "material readiness poll: expected_text=%s loading_text=%s generate_enabled=%s elapsed=%.1fs remaining=%.1fs",
            has_expected_text,
            has_loading_text,
            last_enabled,
            elapsed,
            remaining,
        )
        if elapsed >= min_wait_seconds and last_enabled and not has_loading_text:
            LOG.info("material readiness confirmed")
            return
        poll_seconds = float(cfg.get("performance", {}).get("material_ready_poll_seconds", 3.0))
        time.sleep(min(max(0.5, poll_seconds), max(0.0, deadline - time.time())))
    LOG.warning(
        "material readiness timeout after %.1fs; proceeding to click generate and let result detection decide",
        timeout_seconds,
    )


def read_quota_once(cfg: dict[str, Any], run_no: int, step: str = "quota", save_failure: bool = True) -> int | None:
    if not cfg.get("ocr", {}).get("enabled", True):
        LOG.info("quota read skipped because OCR is disabled")
        return None

    timeout = float(cfg["timeouts"].get("quota_seconds", 8))
    deadline = time.time() + timeout
    patterns = [re.compile(p) for p in cfg.get("quota_patterns", [])]
    region = resolve_region(cfg, "quota")
    while time.time() < deadline:
        try:
            shot = capture_for_step(run_no, step)
        except AutomationError as exc:
            LOG.warning("quota screenshot failed: %r", exc)
            return None
        items = run_ocr(shot, region, cfg)
        joined = normalize_text(" ".join(i.text for i in items))
        LOG.info("quota OCR text: %s", joined or "<empty>")
        for pat in patterns:
            match = pat.search(joined)
            if match:
                quota = int(match.group(1))
                LOG.info("quota parsed: %s", quota)
                return quota
        time.sleep(1.0)
    if save_failure:
        save_error(run_no, "quota_read_failed", "could not read quota")
    return None


def ask_nvidia_if_quota_is_zero(cfg: dict[str, Any], run_no: int) -> bool | None:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        LOG.warning("NVIDIA_API_KEY is not set; cannot verify zero quota with NVIDIA model")
        return None

    try:
        shot = capture_for_step(run_no, "quota_nvidia_verify")
    except AutomationError as exc:
        LOG.warning("quota NVIDIA verification screenshot failed: %r", exc)
        return None

    try:
        with Image.open(shot) as img:
            region = resolve_region(cfg, "quota")
            if region is not None:
                img = crop_region(img, region)
            img = img.convert("RGB")
            verify_path = shot.with_name(shot.stem + "_crop.jpg")
            img.save(verify_path, format="JPEG", quality=92)
        image_b64 = base64.b64encode(verify_path.read_bytes()).decode("ascii")
    except Exception as exc:
        LOG.warning("failed to prepare quota image for NVIDIA verification: %r", exc)
        return None

    prompt = (
        "请判断图片中是否显示今天剩余可生成视频额度为0。"
        "只根据图片里的额度文字判断。"
        "如果明确是0，回答 JSON: {\"is_zero\": true}。"
        "如果不是0、看不清、或无法确认，回答 JSON: {\"is_zero\": false}。"
    )
    payload = {
        "model": NVIDIA_QUOTA_MODEL,
        "temperature": 0,
        "max_tokens": 512,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        NVIDIA_API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        timeout = float(cfg.get("quota_zero_verification", {}).get("nvidia_timeout_seconds", 30))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        LOG.warning("NVIDIA quota verification HTTP error: status=%s body=%s", exc.code, body[:1000])
        return None
    except Exception as exc:
        LOG.warning("NVIDIA quota verification failed: %r", exc)
        return None

    try:
        content = str(result["choices"][0]["message"]["content"])
    except Exception:
        LOG.warning("NVIDIA quota verification returned unexpected response: %s", result)
        return None

    LOG.info("NVIDIA quota verification response: %s", content)
    normalized = content.strip().lower()
    match = re.search(r'"is_zero"\s*:\s*(true|false)', normalized)
    if match:
        return match.group(1) == "true"
    if normalized in {"true", "yes", "0", "zero"}:
        return True
    if normalized in {"false", "no", "not zero"}:
        return False
    LOG.warning("NVIDIA quota verification response was not parseable; treating as not confirmed zero")
    return None


def read_quota(cfg: dict[str, Any], run_no: int) -> int | None:
    quota = read_quota_once(cfg, run_no)
    if quota != 0:
        return quota

    fallback_nonzero = int(cfg.get("quota_zero_verification", {}).get("unconfirmed_zero_fallback_quota", 1))
    LOG.warning("quota parsed as 0; rechecking with original OCR logic before stopping")
    delay = float(cfg.get("quota_zero_verification", {}).get("ocr_recheck_delay_seconds", 1.0))
    if delay > 0:
        time.sleep(delay)
    rechecked_quota = read_quota_once(cfg, run_no, step="quota_recheck", save_failure=False)
    if rechecked_quota is None:
        LOG.warning(
            "quota zero not confirmed because OCR recheck failed; treating as quota=%s to avoid false stop",
            fallback_nonzero,
        )
        return fallback_nonzero
    if rechecked_quota > 0:
        LOG.warning("quota zero not confirmed by OCR recheck; first=%s recheck=%s", quota, rechecked_quota)
        return rechecked_quota

    LOG.warning("quota still parsed as 0 after OCR recheck; verifying with NVIDIA model")
    nvidia_says_zero = ask_nvidia_if_quota_is_zero(cfg, run_no)
    if nvidia_says_zero is True:
        LOG.info("quota zero confirmed by NVIDIA model")
        return 0
    LOG.warning(
        "quota zero not confirmed by NVIDIA model; nvidia_result=%s; treating as quota=%s to avoid false stop",
        nvidia_says_zero,
        fallback_nonzero,
    )
    return fallback_nonzero


def run_once(cfg: dict[str, Any], run_no: int, dry_run: bool, refresh_before_run: bool = True) -> int | None:
    LOG.info(
        "========== run %03d start dry_run=%s refresh_before_run=%s ==========",
        run_no,
        dry_run,
        refresh_before_run,
    )
    activate_target_app(cfg)
    manual_delay = float(cfg.get("pre_run_manual_switch_seconds", 0))
    if manual_delay > 0 and run_no == 1:
        LOG.info("waiting %.1fs for manual window switch", manual_delay)
        time.sleep(manual_delay)
    if refresh_before_run and cfg.get("refresh", {}).get("before_each_run", False):
        refresh_page(cfg, run_no, "before_run", dry_run)
    capture_for_step(run_no, "start")

    starting_quota = read_quota(cfg, run_no)
    if starting_quota is not None:
        LOG.info("starting quota before run %03d: %s", run_no, starting_quota)
        if starting_quota <= 0:
            capture_for_step(run_no, "end")
            LOG.info("========== run %03d end quota=%s ==========", run_no, starting_quota)
            return starting_quota

    click_target(cfg, run_no, "select_product_button", dry_run)
    if not dry_run:
        ok = wait_for_text(
            cfg, run_no, ["选择商品", "全部商品", "推荐商品"], cfg["timeouts"]["modal_seconds"], "product_modal", "product_modal"
        )
        if not ok:
            raise AutomationError("product modal did not appear")

    product_point = click_target(cfg, run_no, "modal_product_checkbox", dry_run)
    click_target(cfg, run_no, "modal_confirm_button", dry_run)

    if not dry_run:
        time.sleep(float(cfg["waits"].get("after_select_product_confirm_seconds", 2.0)))
        ok = wait_for_text(
            cfg,
            run_no,
            ["选择视频生成类型", "活动", "故事成片", "穿搭展示", "单品展示"],
            cfg["timeouts"]["type_area_seconds"],
            "left_panel",
            "video_type_area",
        )
        if not ok:
            raise AutomationError("video type area did not appear")
        if product_point:
            remember_successful_target("modal_product_checkbox", product_point, "confirmed_after_modal_close")

    click_video_type_for_run(cfg, run_no, dry_run)
    wait_after_video_type_selected(cfg, run_no, dry_run)
    click_target(cfg, run_no, "generate_button", dry_run)

    if not dry_run:
        time.sleep(float(cfg["waits"].get("after_generate_click_seconds", 4.0)))
        ok = wait_for_text(
            cfg,
            run_no,
            cfg.get("result_success_texts", ["视频生成中", "预计需要"]),
            cfg["timeouts"]["result_seconds"],
            "result_panel",
            "result_task",
        )
        if not ok:
            raise AutomationError("new result task was not detected")

    quota = read_quota(cfg, run_no)
    if quota is None and starting_quota is not None and cfg.get("on_quota_read_fail") == "estimate_after_success":
        quota = max(starting_quota - 1, 0)
        LOG.warning("quota read failed after successful generation; estimated quota=%s from starting_quota=%s", quota, starting_quota)
    try:
        capture_for_step(run_no, "end")
    except AutomationError as exc:
        LOG.warning("end screenshot failed after run completion: %r", exc)
    LOG.info("========== run %03d end quota=%s ==========", run_no, quota)
    return quota


def parse_args() -> argparse.Namespace:
    cfg = load_config()
    parser = argparse.ArgumentParser(description="Loop generate Douyin Shop AI videos from current page.")
    parser.add_argument("--mode", choices=["dry-run", "execute"], default="dry-run" if cfg.get("dry_run_default", True) else "execute")
    parser.add_argument("--max-runs", type=int, default=int(cfg.get("default_max_runs", 1)), help="Maximum loop count, e.g. 1, 3, 15.")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to config.json.")
    return parser.parse_args()


def main() -> int:
    setup_logging()
    try:
        lock_file = acquire_run_lock()
    except AutomationError as exc:
        LOG.error("%s", exc)
        return 1
    args = parse_args()
    cfg_path = Path(args.config).expanduser().resolve()
    global CONFIG_PATH
    CONFIG_PATH = cfg_path
    cfg = load_config()
    dry_run = args.mode == "dry-run"

    pyautogui.FAILSAFE = True
    LOG.info("screen size: %s", screen_size())
    LOG.info("config: %s", cfg_path)
    LOG.info("mode=%s max_runs=%s", args.mode, args.max_runs)
    run_safety_checks(cfg, startup=True)

    last_quota: int | None = None
    run_no = 1
    while run_no <= args.max_runs:
        attempts = 0
        max_retries = int(cfg.get("refresh", {}).get("max_error_refresh_retries", 0))
        refresh_before_attempt = True
        try:
            while True:
                try:
                    quota = run_once(cfg, run_no, dry_run, refresh_before_run=refresh_before_attempt)
                    last_quota = quota
                    break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    attempts += 1
                    save_error(run_no, f"exception_attempt_{attempts}", repr(exc))
                    LOG.exception("run %03d failed on attempt %s/%s", run_no, attempts, max_retries + 1)
                    if dry_run or attempts > max_retries or not cfg.get("refresh", {}).get("on_error", False):
                        raise
                    refreshed = refresh_page(cfg, run_no, f"recover_attempt_{attempts}", dry_run=False)
                    if not refreshed:
                        LOG.warning("recovery refresh failed; aborting run %03d", run_no)
                        raise
                    refresh_before_attempt = False
                    LOG.info(
                        "retrying run %03d from select-product flow after recovery refresh (%s/%s)",
                        run_no,
                        attempts,
                        max_retries,
                    )

            if dry_run:
                LOG.info("dry-run finished after one planned run")
                break
            if quota is None:
                if cfg.get("on_quota_read_fail", "stop") == "continue":
                    LOG.warning("quota read failed; continuing because config allows it")
                else:
                    LOG.warning("quota read failed; stopping")
                    break
            elif quota <= 0:
                LOG.info("quota is 0; stopping")
                break
            recycle_runs = int(cfg.get("ocr", {}).get("paddle", {}).get("worker_recycle_runs", 0))
            if recycle_runs > 0 and run_no % recycle_runs == 0:
                LOG.info("recycling PaddleOCR worker after run %03d", run_no)
                stop_paddle_worker()
            run_safety_checks(cfg, startup=False)
            run_no += 1
            time.sleep(float(cfg["waits"].get("between_runs_seconds", 2.0)))
        except KeyboardInterrupt:
            LOG.warning("interrupted by user")
            notify_script_finished(cfg, "抖店自动生成脚本已中断", f"已中断，最后额度：{last_quota}")
            return 130
        except Exception:
            notify_script_finished(cfg, "抖店自动生成脚本失败", f"第 {run_no} 轮失败，请查看日志和错误截图")
            close_target_app(cfg)
            return 1

    LOG.info("loop finished; last_quota=%s", last_quota)
    notify_script_finished(cfg, "抖店自动生成脚本结束", f"运行完成，最后额度：{last_quota}")
    close_target_app(cfg)
    lock_file.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
