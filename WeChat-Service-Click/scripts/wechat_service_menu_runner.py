"""Automate first-level menus in the currently open WeChat service-account chat.

UI Automation is attempted first.  If WeChat exposes no usable menu controls,
the script can fall back to OCR plus pyautogui.  The script never reads or
changes Windows proxy settings.

Run from a normal (non-elevated) terminal while the target service-account
chat is already open and visible::

    python scripts/wechat_service_menu_runner.py

OCR fallback requires pytesseract and a local Tesseract executable.  If it is
not on PATH, pass ``--tesseract-cmd``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


LOG = logging.getLogger("wechat-menu-runner")
WECHAT_PROCESS_RE = re.compile(r"(?i)(?:^|\\)(?:Weixin|WeChat)\.exe$")
WEBVIEW_PROCESS_RE = re.compile(r"(?i)(?:^|\\)WeChatAppEx\.exe$")
IGNORED_BUTTON_NAMES = {
    "",
    "发送",
    "表情",
    "更多",
    "语音",
    "切换到按住说话",
    "切换到键盘",
    "输入",
    "关闭",
    "最小化",
    "最大化",
    "置顶",
    "公众号主页",
    "你尚未关注该账号，去关注。",
    "你尚未关注该账号",
    "去关注",
}


@dataclass(frozen=True)
class MenuTarget:
    name: str
    x: int
    y: int
    source: str


@dataclass
class ClickLog:
    timestamp: str
    index: int
    menu: str
    source: str
    status: str
    detail: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def import_automation_dependencies():
    try:
        import pyautogui  # type: ignore
        from pywinauto import Desktop  # type: ignore
        import uiautomation as auto  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "缺少自动化依赖。请按需安装 requirements-automation.txt；脚本不会自动安装任何库。"
        ) from exc
    return pyautogui, Desktop, auto


def _query_process_path(pid: int) -> str:
    if not pid:
        return ""
    try:
        k32 = ctypes.WinDLL("kernel32")
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_uint(260)
            buf = ctypes.create_unicode_buffer(260)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return ""


def _safe_set_focus(window) -> None:
    """容错的 set_focus：pywinauto 失败(COMError)时回退 Win32 SetForegroundWindow。"""
    try:
        window.set_focus()
        return
    except Exception:
        pass
    try:
        hwnd = window.handle
        user32 = ctypes.WinDLL("user32")
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        for _ in range(3):
            if user32.SetForegroundWindow(hwnd):
                break
            time.sleep(0.1)
    except Exception:
        pass


def find_wechat_window(desktop):
    wechat_process_windows: list[tuple[str, str, object]] = []
    for window in desktop.windows():
        try:
            title = window.window_text().strip()
            process_path = _query_process_path(window.process_id())
            if not WECHAT_PROCESS_RE.search(process_path):
                continue
            wechat_process_windows.append((title, process_path, window))
        except Exception:
            continue
    if wechat_process_windows:
        visible = [item for item in wechat_process_windows if _safe_visible(item[2])]
        pool = visible or wechat_process_windows
        best = max(pool, key=lambda item: _window_area(item[2]))
        window = best[2]
        _safe_set_focus(window)
        return window
    visible: list[tuple[str, str]] = []
    for window in desktop.windows():
        try:
            if not _safe_visible(window):
                continue
            t = window.window_text().strip()
            if not t:
                continue
            visible.append((t, _query_process_path(window.process_id())))
        except Exception:
            continue
    detail = "(未匹配到微信进程窗口) 所有可见顶级窗口清单：\n" + (
        "\n".join(f"  title={t!r} proc={p}" for t, p in visible[:60])
        or "(无可见窗口)"
    )
    raise RuntimeError(f"未找到微信主窗口。{detail}")


def _window_area(window) -> int:
    try:
        r = window.rectangle()
        return max(0, (r.right - r.left) * (r.bottom - r.top))
    except Exception:
        return 0


def _safe_visible(window) -> bool:
    try:
        return bool(window.is_visible())
    except Exception:
        return False


def is_bottom_menu_candidate(control, window_rect) -> bool:
    try:
        rect = control.rectangle()
        name = control.window_text().strip()
        if not control.is_visible() or not control.is_enabled():
            return False
        if name in IGNORED_BUTTON_NAMES:
            return False
        if len(name) < 2:  # 过滤单字噪音
            return False
        if rect.width() < 35 or rect.height() < 20:
            return False
        bottom_band_top = window_rect.top + int(window_rect.height() * 0.72)
        return rect.top >= bottom_band_top and rect.bottom <= window_rect.bottom
    except Exception:
        return False


def _clean_menu_name(name: str) -> str:
    """清理 OCR 识别的菜单名，去掉前后噪音字符。"""
    import re
    # 去掉前后的非中文、非字母数字字符（如 =, -, ", ©, ), 等）
    cleaned = re.sub(r'^[^\u4e00-\u9fff\w]+', '', name)
    cleaned = re.sub(r'[^\u4e00-\u9fff\w]+$', '', cleaned)
    return cleaned


def _is_valid_menu_name(name: str) -> bool:
    """判断清理后的菜单名是否有效（至少包含 2 个中文字符或 2 个字母）。"""
    import re
    cleaned = _clean_menu_name(name)
    if len(cleaned) < 2:
        return False
    # 纯特殊字符（无中文、无字母数字）过滤掉
    if not re.search(r'[\u4e00-\u9fff\w]', cleaned):
        return False
    return True


def deduplicate_targets(targets: Iterable[MenuTarget]) -> list[MenuTarget]:
    result: list[MenuTarget] = []
    for target in sorted(targets, key=lambda item: (item.x, item.y)):
        # 清理名字
        target = MenuTarget(
            name=_clean_menu_name(target.name),
            x=target.x,
            y=target.y,
            source=target.source,
        )
        # 过滤无效名字
        if not _is_valid_menu_name(target.name):
            continue
        # 去重：x 间距 < 40px 且 y 间距 < 20px 视为同一个
        if any(abs(target.x - old.x) < 40 and abs(target.y - old.y) < 20 for old in result):
            continue
        result.append(target)
    return result


def discover_menus_with_pywinauto(window) -> list[MenuTarget]:
    rect = window.rectangle()
    targets = []
    for control in window.descendants(control_type="Button"):
        if not is_bottom_menu_candidate(control, rect):
            continue
        box = control.rectangle()
        targets.append(
            MenuTarget(
                name=control.window_text().strip() or f"menu@{box.mid_point().x}",
                x=box.mid_point().x,
                y=box.mid_point().y,
                source="pywinauto-uia",
            )
        )
    return deduplicate_targets(targets)


def discover_menus_with_uiautomation(auto, window) -> list[MenuTarget]:
    handle = window.handle
    root = auto.ControlFromHandle(handle)
    rect = root.BoundingRectangle
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
    height = bottom - top
    targets: list[MenuTarget] = []

    def walk(control, depth: int = 0):
        if depth > 8:
            return
        try:
            if control.ControlTypeName == "ButtonControl":
                br = control.BoundingRectangle
                x1, y1, x2, y2 = br.left, br.top, br.right, br.bottom
                name = (control.Name or "").strip()
                if (
                    name not in IGNORED_BUTTON_NAMES
                    and len(name) >= 2
                    and y1 >= top + int(height * 0.72)
                    and x2 - x1 >= 35
                    and y2 - y1 >= 20
                ):
                    targets.append(
                        MenuTarget(name or f"menu@{(x1 + x2) // 2}", (x1 + x2) // 2, (y1 + y2) // 2, "uiautomation")
                    )
            for child in control.GetChildren():
                walk(child, depth + 1)
        except Exception:
            return

    walk(root)
    return deduplicate_targets(targets)


def discover_menus_with_ocr(pyautogui, window, tesseract_cmd: str | None) -> list[MenuTarget]:
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        import pytesseract  # type: ignore
        from pytesseract import Output  # type: ignore
    except ImportError as exc:
        raise RuntimeError("OCR 回退需要 pillow、opencv-python 和 pytesseract。") from exc

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    rect = window.rectangle()
    region_top = rect.top + int(rect.height() * 0.72)
    image = pyautogui.screenshot(region=(rect.left, region_top, rect.width(), rect.bottom - region_top))
    pixels = np.array(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

    _, thresh_binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, thresh_inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    white_ratio = np.sum(thresh_binary == 255) / thresh_binary.size
    if white_ratio < 0.35:
        processed = thresh_inv
    else:
        processed = thresh_binary

    # 尝试多个 OCR 策略，取识别到文字最多的结果
    ocr_configs = [
        (processed, "--psm 6"),
        (gray, "--psm 6"),
        (gray, "--psm 11"),
    ]
    best_data = None
    best_count = 0
    for img, psm in ocr_configs:
        try:
            data = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=Output.DICT, config=psm)
        except pytesseract.TesseractError:
            data = pytesseract.image_to_data(img, lang="eng", output_type=Output.DICT, config=psm)
        count = sum(1 for t in data["text"] if t.strip())
        if count > best_count:
            best_count = count
            best_data = data
    data = best_data

    # 合并同一行的文字为完整菜单项（同二级菜单逻辑）
    raw_items = []
    for i, text in enumerate(data["text"]):
        name = text.strip()
        confidence = float(data["conf"][i]) if str(data["conf"][i]) != "-1" else -1
        if not name or confidence < 40 or name in IGNORED_BUTTON_NAMES:
            continue
        oy = int(data["top"][i]) // 2
        ox = int(data["left"][i]) // 2
        oh = int(data["height"][i]) // 2
        ow = int(data["width"][i]) // 2
        raw_items.append({"name": name, "y": region_top + oy + oh // 2, "x": rect.left + ox + ow // 2, "height": oh, "left": ox, "right": ox + ow, "top": oy, "bottom": oy + oh})

    # 按 y 分行
    raw_items.sort(key=lambda item: (item["y"], item["x"]))
    rows: list[list[dict]] = []
    if raw_items:
        current_row = [raw_items[0]]
        for item in raw_items[1:]:
            row_center = sum(r["y"] for r in current_row) / len(current_row)
            row_height = max(r["height"] for r in current_row)
            if abs(item["y"] - row_center) < row_height * 0.6:
                current_row.append(item)
            else:
                rows.append(current_row)
                current_row = [item]
        rows.append(current_row)

    # 一级菜单是水平排列的，同一行内按 x 间距分组（间距 > 25px 就是不同按钮）
    targets: list[MenuTarget] = []
    for row in rows:
        row.sort(key=lambda item: item["x"])
        groups: list[list[dict]] = []
        current_group = [row[0]]
        for item in row[1:]:
            prev_right = current_group[-1]["right"]
            gap = item["left"] - prev_right
            if gap > 25:
                groups.append(current_group)
                current_group = [item]
            else:
                current_group.append(item)
        groups.append(current_group)

        for group in groups:
            merged = "".join(item["name"] for item in group)
            if not merged or merged in IGNORED_BUTTON_NAMES or len(merged) < 2:
                continue
            min_left = min(item["left"] for item in group)
            max_right = max(item["right"] for item in group)
            min_top = min(item["top"] for item in group)
            max_bottom = max(item["bottom"] for item in group)
            cx = rect.left + (min_left + max_right) // 2
            cy = region_top + (min_top + max_bottom) // 2
            targets.append(MenuTarget(name=merged, x=cx, y=cy, source="ocr-pyautogui"))

    LOG.info("  一级菜单 OCR 合并结果：%s", [(t.name, t.x, t.y) for t in targets])
    return deduplicate_targets(targets)


def discover_menu_targets(pyautogui, desktop, auto, window, args) -> list[MenuTarget]:
    strategies = (
        lambda: discover_menus_with_pywinauto(window),
        lambda: discover_menus_with_uiautomation(auto, window),
    )
    for strategy in strategies:
        targets = strategy()
        if targets:
            return targets
    if args.no_ocr:
        return []
    return discover_menus_with_ocr(pyautogui, window, args.tesseract_cmd)


def discover_submenus_with_ocr(pyautogui, window, known_targets: Sequence[MenuTarget], primary_target: MenuTarget, tesseract_cmd: str | None, debug: bool = True, fast: bool = False) -> list[MenuTarget]:
    """用 OCR 识别一级菜单上方弹窗中的二级菜单项，但点击坐标用相对公式计算。"""
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        import pytesseract  # type: ignore
        from pytesseract import Output  # type: ignore
    except ImportError as exc:
        raise RuntimeError("OCR 识别二级菜单需要 pillow、opencv-python 和 pytesseract。") from exc

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    rect = window.rectangle()
    # 只截取一级按钮正上方窄区域（避免截到聊天消息）
    popup_width = 400  # 弹窗宽度估计
    region_left = max(rect.left, primary_target.x - popup_width // 2)
    region_right = min(rect.right, primary_target.x + popup_width // 2)
    region_top = primary_target.y - 450  # 弹窗在按钮上方约 450px
    region_bottom = primary_target.y - 20  # 不包含按钮本身
    region_width = region_right - region_left
    region_height = region_bottom - region_top
    if region_height < 50 or region_width < 50:
        return []
    image = pyautogui.screenshot(region=(region_left, region_top, region_width, region_height))

    if debug and not fast:
        debug_dir = Path(tempfile.gettempdir()) / "wechat_ocr_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_path = debug_dir / f"popup_{int(time.time())}.png"
        image.save(str(debug_path))
        LOG.info("  调试截图已保存：%s", debug_path)

    pixels = np.array(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)

    if not fast:
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    # fast 模式不放大，直接用原图

    _, thresh_binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    _, thresh_inv = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    white_ratio = np.sum(thresh_binary == 255) / thresh_binary.size
    if white_ratio < 0.35:
        processed = thresh_inv
    else:
        processed = thresh_binary

    if fast:
        # 快速模式：只用 1 种策略
        try:
            data = pytesseract.image_to_data(processed, lang="chi_sim+eng", output_type=Output.DICT, config="--psm 6")
        except pytesseract.TesseractError:
            data = pytesseract.image_to_data(processed, lang="eng", output_type=Output.DICT, config="--psm 6")
    else:
        # 正常模式：尝试多个 OCR 策略，取识别到文字最多的结果
        ocr_configs = [
            (processed, "--psm 6"),      # 二值化+统一文本块
            (gray, "--psm 6"),           # 灰度+统一文本块
            (gray, "--psm 11"),          # 灰度+稀疏文本
        ]
        best_data = None
        best_count = 0
        for img, psm in ocr_configs:
            try:
                data = pytesseract.image_to_data(img, lang="chi_sim+eng", output_type=Output.DICT, config=psm)
            except pytesseract.TesseractError:
                data = pytesseract.image_to_data(img, lang="eng", output_type=Output.DICT, config=psm)
            count = sum(1 for t in data["text"] if t.strip())
            if count > best_count:
                best_count = count
                best_data = data
        data = best_data

    # 合并同一行的文字
    raw_items = []
    scale = 1 if fast else 2  # fast 模式不放大，坐标不用除以 2
    for i, text in enumerate(data["text"]):
        name = text.strip()
        confidence = float(data["conf"][i]) if str(data["conf"][i]) != "-1" else -1
        if not name or confidence < 40 or name in IGNORED_BUTTON_NAMES:
            continue
        oy = int(data["top"][i]) // scale
        ox = int(data["left"][i]) // scale
        oh = int(data["height"][i]) // scale
        ow = int(data["width"][i]) // scale
        raw_items.append({"name": name, "y": region_top + oy + oh // 2, "x": region_left + ox + ow // 2, "height": oh, "left": ox, "right": ox + ow, "top": oy, "bottom": oy + oh})

    LOG.info("  OCR 原始识别结果：%s", [(item["name"], round(item["y"]), round(item["x"])) for item in raw_items])

    # 按 y 分行
    raw_items.sort(key=lambda item: (item["y"], item["x"]))
    rows: list[list[dict]] = []
    if raw_items:
        current_row = [raw_items[0]]
        for item in raw_items[1:]:
            row_center = sum(r["y"] for r in current_row) / len(current_row)
            row_height = max(r["height"] for r in current_row)
            if abs(item["y"] - row_center) < row_height * 0.6:
                current_row.append(item)
            else:
                rows.append(current_row)
                current_row = [item]
        rows.append(current_row)

    # 合并每行文字，过滤无效项
    ocr_names: list[str] = []
    for row in rows:
        row.sort(key=lambda item: item["x"])
        merged = "".join(item["name"] for item in row)
        if not merged or merged in IGNORED_BUTTON_NAMES or len(merged) < 2:
            continue
        ocr_names.append(merged)

    if not ocr_names:
        LOG.info("  OCR 未识别到二级菜单")
        return []

    # ★ 关键：坐标全部用相对公式计算，不依赖 OCR 坐标
    # x = 一级按钮 x（弹窗以按钮为中心展开，点击按钮 x 必中）
    # y = 一级按钮 y - 29 - (num_items - i) * 65（弹窗从按钮正上方向上展开）
    num_items = len(ocr_names)
    SUB_X_OFFSET = 0       # 用一级按钮 x 作为点击 x
    SUB_Y_BASE = 29        # 底部留白
    SUB_Y_SPACING = 65     # 每项高度

    targets: list[MenuTarget] = []
    for i, name in enumerate(ocr_names):
        cx = primary_target.x + SUB_X_OFFSET
        cy = primary_target.y - SUB_Y_BASE - (num_items - i) * SUB_Y_SPACING
        targets.append(MenuTarget(name=name, x=cx, y=cy, source="relative-calc"))

    LOG.info("  相对坐标计算二级菜单：%s", [(t.name, t.x, t.y) for t in targets])
    return targets


def discover_submenus_fast(pyautogui, window, known_targets: Sequence[MenuTarget], primary_target: MenuTarget, tesseract_cmd: str | None) -> list[MenuTarget]:
    """扫描阶段专用：直接 OCR，只用 1 种策略，不放大图像，速度快。"""
    return discover_submenus_with_ocr(pyautogui, window, known_targets, primary_target, tesseract_cmd, fast=True)


def discover_submenus(pyautogui, window, known_targets: Sequence[MenuTarget], primary_target: MenuTarget, tesseract_cmd: str | None) -> list[MenuTarget]:
    """先尝试 UIA 识别弹窗按钮，失败则回退到 OCR（坐标用相对公式计算）。"""
    try:
        rect = window.rectangle()
        targets: list[MenuTarget] = []
        mid_top = rect.top + int(rect.height() * 0.25)
        for control in window.descendants(control_type="Button"):
            try:
                box = control.rectangle()
                name = control.window_text().strip()
                x, y = box.mid_point().x, box.mid_point().y
                if name in IGNORED_BUTTON_NAMES:
                    continue
                if len(name) < 2:
                    continue
                if not control.is_visible() or not control.is_enabled():
                    continue
                if box.width() < 20 or box.height() < 16:
                    continue
                if not (box.top >= mid_top and box.bottom <= rect.bottom):
                    continue
                if any(abs(x - k.x) < 24 and abs(y - k.y) < 24 for k in known_targets):
                    continue
                targets.append(MenuTarget(name=name or f"sub@{x}", x=x, y=y, source="pywinauto-uia-sub"))
            except Exception:
                continue
        if targets:
            return targets
    except Exception as exc:
        LOG.info("  UIA 访问失败(%s)，直接 OCR ...", type(exc).__name__)
    # UIA 识别不到（弹窗是 WebView 自绘），回退 OCR
    LOG.info("  UIA 未识别到二级菜单，尝试 OCR ...")
    return discover_submenus_with_ocr(pyautogui, window, known_targets, primary_target, tesseract_cmd)


def wait_for_h5_window(desktop, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in desktop.windows():
            try:
                if WEBVIEW_PROCESS_RE.search(_query_process_path(window.process_id())):
                    return window
            except Exception:
                continue
        time.sleep(0.1)
    return None


def return_to_chat(pyautogui, wechat_window, h5_window, wait_seconds: float) -> None:
    """快速返回：尝试切换回微信窗口，不等待。"""
    if h5_window is not None:
        try:
            _safe_set_focus(h5_window)
            pyautogui.hotkey("alt", "left")
        except Exception:
            pass
    _safe_set_focus(wechat_window)
    time.sleep(wait_seconds)


def write_jsonl(path: Path, entry: ClickLog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def run(args) -> int:
    pyautogui, Desktop, auto = import_automation_dependencies()
    pyautogui.FAILSAFE = True
    desktop = Desktop(backend="uia")
    wechat = find_wechat_window(desktop)
    targets = discover_menu_targets(pyautogui, desktop, auto, wechat, args)
    if not targets:
        raise RuntimeError("未识别到聊天窗口底部一级菜单；请确认目标服务号聊天页已打开且菜单可见。")

    LOG.info("识别到 %d 个一级菜单：%s", len(targets), [item.name for item in targets])

    # ===== 弹窗管理：记录已存在的 WeChatAppEx 窗口，扫描和点击时关闭新弹出的 =====
    def _get_webview_handles():
        """获取当前所有 WeChatAppEx.exe 窗口的 handle。"""
        handles = set()
        for window in desktop.windows():
            try:
                if WEBVIEW_PROCESS_RE.search(_query_process_path(window.process_id())):
                    handles.add(window.handle)
            except Exception:
                continue
        return handles

    existing_handles = _get_webview_handles()

    def _close_popup_windows():
        """关闭点击后新弹出的小程序窗口，保留搜一搜 H5 不动。"""
        current_handles = _get_webview_handles()
        new_handles = current_handles - existing_handles
        for window in desktop.windows():
            try:
                if window.handle in new_handles:
                    _safe_set_focus(window)
                    time.sleep(0.02)
                    pyautogui.hotkey("alt", "f4")
                    time.sleep(0.05)
                    LOG.info("  已关闭弹出的小程序窗口")
            except Exception:
                continue
        # 回到微信主窗口
        _safe_set_focus(wechat)
        time.sleep(0.05)

    # ===== 阶段 1：扫描所有一级弹窗，拿到二级菜单数量和名称 =====
    all_subs: dict[int, list[MenuTarget]] = {}
    for index, target in enumerate(targets, start=1):
        try:
            _safe_set_focus(wechat)
            time.sleep(0.03)
            pyautogui.click(target.x, target.y)
            time.sleep(0.1)  # 弹窗展开只需极短时间
            try:
                subs = discover_submenus_fast(pyautogui, wechat, targets, target, args.tesseract_cmd)
            except Exception as exc:
                LOG.warning("  二级菜单扫描失败（%s）：%s", type(exc).__name__, exc)
                subs = []
            all_subs[index - 1] = subs
            if subs:
                LOG.info("  [扫描 %d/%d] 「%s」→ %d 个二级：%s", index, len(targets), target.name, len(subs), [s.name for s in subs])
            else:
                LOG.info("  [扫描 %d/%d] 「%s」→ 无二级", index, len(targets), target.name)
        except Exception as exc:
            LOG.warning("  扫描「%s」失败：%s", target.name, exc)
            all_subs[index - 1] = []
        # 扫描完后回到私信窗口，不关闭小程序
        _safe_set_focus(wechat)
        time.sleep(0.1)

    # ===== 阶段 2：统一点击 =====
    LOG.info("扫描完成，开始点击...")

    for index, target in enumerate(targets, start=1):
        write_jsonl(args.log_file, ClickLog(utc_now(), index, target.name, target.source, "started", "tier=primary"))
        subs = all_subs.get(index - 1, [])
        try:
            if subs:
                for sindex, sub in enumerate(subs, start=1):
                    menu_label = f"{target.name}/{sub.name}"
                    write_jsonl(args.log_file, ClickLog(utc_now(), index, menu_label, sub.source, "started", f"tier=sub parent={target.name}"))
                    try:
                        # 点一级菜单展开弹窗
                        pyautogui.click(target.x, target.y)
                        time.sleep(0.1)
                        # 点二级菜单
                        pyautogui.click(sub.x, sub.y)
                        LOG.info("  [二级 %d/%d] 已点击 %s (x=%d y=%d)", sindex, len(subs), sub.name, sub.x, sub.y)
                        time.sleep(0.2)  # 等页面/小程序加载
                        # 关闭可能弹出的小程序窗口，回到私信窗口
                        _close_popup_windows()
                    except Exception as exc:
                        LOG.exception("二级菜单 %s 执行失败", sub.name)
                        write_jsonl(args.log_file, ClickLog(utc_now(), index, menu_label, sub.source, "failed", f"tier=sub parent={target.name} {exc}"))
                        _close_popup_windows()
            else:
                # 一级菜单无二级，直接点击
                pyautogui.click(target.x, target.y)
                LOG.info("[一级 %d/%d] 已点击 %s", index, len(targets), target.name)
                time.sleep(0.3)
                _close_popup_windows()
        except Exception as exc:
            LOG.exception("一级菜单 %s 执行失败", target.name)
            write_jsonl(args.log_file, ClickLog(utc_now(), index, target.name, target.source, "failed", f"tier=primary {exc}"))
            _close_popup_windows()
    return 0


def _handle_h5(pyautogui, desktop, args, index, menu_label, source, parent) -> object:
    """快速模式：尝试检测 H5 但不等待加载完成，点击后立即返回。"""
    h5 = wait_for_h5_window(desktop, args.h5_timeout)
    if h5 is None:
        status, detail = "no-h5-window", f"{args.h5_timeout:.1f}s 内未发现 WeChatAppEx 窗口"
    else:
        _safe_set_focus(h5)
        status, detail = "loaded", "检测到 H5 窗口"
    tier = "sub" if parent else "primary"
    write_jsonl(args.log_file, ClickLog(utc_now(), index, menu_label, source, status, f"tier={tier} parent={parent} {detail}"))
    return h5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="依次访问当前微信服务号聊天窗口的一级及二级菜单")
    parser.add_argument("--load-wait", type=float, default=0.15, help="检测到 H5 后额外等待秒数")
    parser.add_argument("--h5-timeout", type=float, default=0.8, help="等待 H5 窗口出现的超时秒数")
    parser.add_argument("--return-wait", type=float, default=0.15, help="返回聊天页后的等待秒数")
    parser.add_argument("--sub-wait", type=float, default=0.3, help="点击一级菜单后等待二级菜单展开的秒数")
    parser.add_argument("--no-ocr", action="store_true", help="UIA 未识别菜单时禁用 OCR 回退")
    parser.add_argument("--tesseract-cmd", help="tesseract.exe 的完整路径")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path(tempfile.gettempdir()) / "wechat_menu_clicks.jsonl",
        help="JSON Lines 点击日志路径（默认: 系统临时目录）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.warning("用户中止")
        return 130
    except Exception:
        LOG.exception("自动化失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())

