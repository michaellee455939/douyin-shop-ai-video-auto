# 抖店 AI 智能成片自动循环 MVP

这个项目主要从你已经打开的抖店工作台 App「AI智能成片」页面开始运行，负责当前页面内的选择商品、选择生成类型、点击生成、读取剩余额度并循环。

现在已补充一个独立导航脚本，可先把本机抖店工作台操作到「AI智能成片」页面。导航脚本只进入页面，不选择商品、不点击生成、不发布、不消耗额度。

## 文件结构

```text
douyin_shop_ai_video_auto/
├── config.json
├── find_qianchuan.py
├── run_full_ai_video_auto.py
├── run_ai_video_loop.py
├── scripts/
│   ├── navigate_ai_video_page.py
│   └── navigate_ai_video_page.command
├── logs/
│   ├── app.log
│   └── error_screenshots/
└── screenshots/
```

## 使用前准备

1. 手动打开抖店工作台 App。
2. 手动进入「AI智能成片」页面。
3. 确保 macOS 已给 Terminal/Codex/Python 授权：
   - 系统设置 -> 隐私与安全性 -> 辅助功能
   - 系统设置 -> 隐私与安全性 -> 屏幕录制
4. 首次建议只跑 dry-run 或最多 execute 1 次。

## 命令

一键进入「AI智能成片」页面，然后执行自动生成循环：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_full_ai_video_auto.py --mode execute --max-runs 1
```

先完整走一遍但不真正生成：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_full_ai_video_auto.py --mode dry-run --max-runs 1
```

如果页面已经在「AI智能成片」，可以跳过导航：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_full_ai_video_auto.py --skip-navigation --mode execute --max-runs 1
```

只识别目标，不点击：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_ai_video_loop.py --mode dry-run --max-runs 1
```

真正执行 1 次：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_ai_video_loop.py --mode execute --max-runs 1
```

确认稳定后最多执行 15 次：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_ai_video_loop.py --mode execute --max-runs 15
```

故事成片兜底版本：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 run_ai_video_loop_story_only.py --mode execute
```

查找顶部导航「巨量千川」坐标，不点击：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 find_qianchuan.py --no-notify
```

识别到「巨量千川」后点击它：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 find_qianchuan.py --click-target
```

识别到「巨量千川」后，根据固定偏移点击左上角刷新按钮：

```bash
cd /Users/mac/douyin_shop_ai_video_auto
python3 find_qianchuan.py --click-refresh
```

打开抖店工作台并进入「AI智能成片」页面：

```bash
/Users/mac/douyin_shop_ai_video_auto/scripts/navigate_ai_video_page.command
```

只观察并报告将点击的位置，不实际点击导航：

```bash
/Users/mac/douyin_shop_ai_video_auto/scripts/navigate_ai_video_page.command --dry-run --timeout 8
```

## 配置说明

`config.json` 里最重要的是：

- `targets.*.fallback_point`：OCR 找不到按钮时使用的备用点击坐标，按 1920x1080 截图估算，脚本会按实际屏幕尺寸缩放。
- `regions`：OCR 搜索区域，缩小范围能提高稳定性。
- `allow_coordinate_fallback`：设为 `false` 后，找不到 OCR 目标就不点坐标。
- `click_backend`：默认 `quartz`，使用 Quartz `CGEventCreateMouseEvent` 发送点击；可改为 `pyautogui`。
- `ocr.primary`：默认是 `paddle`，复用你现有脚本同款 `PaddleOCR(use_angle_cls=True, lang='ch')` 中文识别方式。
- `app.activate_before_run`：当前默认 `false`，不自动切换抖店窗口。
- `pre_run_manual_switch_seconds`：当前默认 `3`，第 1 轮执行前等待 3 秒，留给你手动切到抖店页面。
- `refresh`：主脚本运行时刷新页面。默认每轮开始先通过 OCR 查找「巨量千川」，按固定偏移点击刷新按钮；如果中途失败，最多刷新并重试当前轮 1 次，避免无限循环。
- `video_type_cycle`：按轮次循环选择视频生成类型，默认依次为 `故事成片`、`穿搭展示`、`单品展示`。
- `runtime_state.json`：运行中记录上一次确认成功的商品点击区域；后续商品关键词不匹配时优先复用该坐标，再退回第一条商品卡片。
- `waits.after_video_type_select_seconds`：选择视频类型后等待素材加载，默认 `10` 秒，避免过早点击「立即生成」。
- `readiness.generate_button_enabled_check`：等待期间记录「立即生成」按钮颜色是否像可点击状态，用于排查加载慢的问题。
- `notifications`：脚本结束、失败或中断时显示 macOS 桌面通知，并播放 `/System/Library/Sounds/Glass.aiff`。
- `on_quota_read_fail`：默认 `stop`，读取不到「今天还可免费提现生成X条视频」时停止，避免误循环。

如果按钮点偏了，先改 `fallback_point`。可以用 dry-run 看日志中输出的目标点位。

## 日志和截图

- 每一步日志：`logs/app.log`
- 每轮开始/结束以及等待过程截图：`screenshots/`
- 异常、卡住、找不到按钮截图：`logs/error_screenshots/`

## 注意

脚本优先使用 PaddleOCR 中文识别，和 `/Users/mac/工作/Macmini/办公软件/macOS操控IhponeOnMac/成品对iphone镜像区域文字识别.py` 的识别方式一致。PaddleOCR 不可用或识别失败时，再尝试 macOS Vision OCR，最后才使用 `config.json` 中的坐标兜底。第一阶段建议先 `--mode execute --max-runs 1` 验证一次，再逐步增加次数。

如果 `python3` 不是装有 pyautogui 的解释器，脚本会自动切到 `/Users/mac/.pyenv/versions/3.10.6/bin/python`。如果日志出现 `screen size: (0, 0)` 或 `screenshot failed`，说明当前启动脚本的 App/终端没有屏幕录制权限，先给它授权后重新运行。
