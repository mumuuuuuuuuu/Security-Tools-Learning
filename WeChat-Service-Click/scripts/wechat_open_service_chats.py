"""依次搜索公众号并打开私信聊天框。

列表文件支持：
  1. TXT：每行一个公众号名称，空行和 # 注释会被忽略。
  2. CSV：默认读取第一列，也可用 --name-column 指定列名。

典型用法：
  python wechat_open_service_chats.py accounts.txt --tesseract-cmd "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"

脚本会依次处理列表中的公众号，最终停留在最后一个成功打开的聊天框。
微信版本更新后，如果按钮文案不同，可通过 --search-label、--category-label、
--message-label 参数补充候选文案。
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import json
import logging
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

# 导入 service_menu_runner 模块
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
import wechat_service_menu_runner as menu_runner


LOG = logging.getLogger("wechat-open-service-chats")
WECHAT_PROCESS_RE = re.compile(r"(?i)(?:^|\\)(?:Weixin|WeChat)\.exe$")
WEBVIEW_PROCESS_RE = re.compile(r"(?i)(?:^|\\)WeChatAppEx\.exe$")
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


@dataclass
class TextTarget:
    text: str
    x: int
    y: int
    source: str
    score: int = 0


@dataclass
class AccountResult:
    timestamp: str
    account: str
    status: str
    detail: str = ""


def import_dependencies():
    try:
        import pyautogui  # type: ignore
        from pywinauto import Desktop  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "缺少依赖，请先安装项目中的 requirements-automation.txt"
        ) from exc
    return pyautogui, Desktop


def query_process_path(pid: int) -> str:
    if not pid:
        return ""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_uint(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def window_area(window) -> int:
    try:
        rect = window.rectangle()
        return max(0, rect.width() * rect.height())
    except Exception:
        return 0


def focus_window(window) -> None:
    try:
        window.set_focus()
        return
    except Exception:
        pass
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = window.handle
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
    except Exception as exc:
        raise RuntimeError("无法把微信窗口切换到前台") from exc


def find_wechat_window(desktop):
    """找微信主窗口（Weixin.exe）。

    如果找不到，退而找 WeChatAppEx.exe 窗口（搜一搜 H5 独立窗口）。
    """
    matches = []
    for window in desktop.windows():
        try:
            if WECHAT_PROCESS_RE.search(query_process_path(window.process_id())):
                matches.append(window)
        except Exception:
            continue
    if matches:
        window = max(matches, key=window_area)
        focus_window(window)
        return window
    # 退而找搜一搜 H5 窗口
    for window in desktop.windows():
        try:
            if WEBVIEW_PROCESS_RE.search(query_process_path(window.process_id())):
                matches.append(window)
        except Exception:
            continue
    if matches:
        window = max(matches, key=window_area)
        focus_window(window)
        return window
    raise RuntimeError("未找到微信主窗口，请先登录并打开电脑版微信")


def find_search_window(desktop):
    """找搜一搜 H5 窗口（WeChatAppEx.exe），用于在独立窗口中操作搜索结果。"""
    matches = []
    for window in desktop.windows():
        try:
            if WEBVIEW_PROCESS_RE.search(query_process_path(window.process_id())):
                matches.append(window)
        except Exception:
            continue
    if matches:
        return max(matches, key=window_area)
    return None


def set_clipboard_text(text: str) -> None:
    """使用 Windows Unicode 剪贴板，避免 pyautogui 无法直接输入中文。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("GlobalAlloc 失败")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise OSError("GlobalLock 失败")
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise OSError("无法打开剪贴板")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise OSError("写入剪贴板失败")
        handle = None  # 成功后内存所有权归系统
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def normalize_text(value: str) -> str:
    return re.sub(r"[\s·•丨|]+", "", value or "").casefold()


def match_score(found: str, wanted: str) -> int:
    """匹配分数：100=精确，75=目标完整出现在识别结果中且长度接近，其他=0。

    避免宽松子串匹配导致误点（如找'公众号'时匹配到'XX公众号运营'）。
    """
    found_n = normalize_text(found)
    wanted_n = normalize_text(wanted)
    if not found_n or not wanted_n:
        return 0
    if found_n == wanted_n:
        return 100
    # 允许目标文字是识别结果的子串，但识别结果长度不能超过目标的 1.5 倍
    # （避免"公众号"匹配到"XX公众号运营中心"这种长账号名）
    if wanted_n in found_n and len(found_n) <= len(wanted_n) * 2:
        return 75
    # 允许识别结果是目标的子串（OCR 漏字），但至少 2 个字
    if found_n in wanted_n and len(found_n) >= 2:
        return 60
    return 0


def find_with_uia(window, labels: Sequence[str]) -> list[TextTarget]:
    targets: list[TextTarget] = []
    try:
        controls = window.descendants()
    except Exception:
        return targets
    for control in controls:
        try:
            text = (control.window_text() or "").strip()
            score = max((match_score(text, label) for label in labels), default=0)
            if not score or not control.is_visible() or not control.is_enabled():
                continue
            rect = control.rectangle()
            if rect.width() < 2 or rect.height() < 2:
                continue
            targets.append(
                TextTarget(text, rect.mid_point().x, rect.mid_point().y, "uia", score)
            )
        except Exception:
            continue
    return targets


# 全局调试截图目录（项目目录下的 debug 文件夹）
if getattr(sys, "frozen", False):
    # PyInstaller 打包进去的资源目录
    RESOURCE_DIR = Path(sys._MEIPASS)
    # 用户看到的 EXE 所在目录
    APP_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = Path(__file__).resolve().parent.parent
    APP_DIR = RESOURCE_DIR

PROJECT_DIR = APP_DIR
DEBUG_DIR = APP_DIR / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def save_debug_screenshot(pyautogui, window, step_name: str) -> Path:
    """保存调试截图，返回文件路径。"""
    rect = window.rectangle()
    img = pyautogui.screenshot(
        region=(rect.left, rect.top, rect.width(), rect.height())
    )
    ts = datetime.now().strftime("%H%M%S")
    path = DEBUG_DIR / f"{ts}_{step_name}.png"
    img.save(str(path))
    LOG.info("调试截图：%s", path)
    return path


def ocr_targets(pyautogui, window, labels: Sequence[str], tesseract_cmd: str | None):
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        import pytesseract  # type: ignore
        from pytesseract import Output  # type: ignore
    except ImportError as exc:
        raise RuntimeError("OCR 回退需要 opencv-python、pytesseract 和 pillow") from exc

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    rect = window.rectangle()
    image = pyautogui.screenshot(
        region=(rect.left, rect.top, rect.width(), rect.height())
    )
    pixels = np.array(image)
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    try:
        data = pytesseract.image_to_data(
            gray, lang="chi_sim+eng", output_type=Output.DICT, config="--psm 11"
        )
    except pytesseract.TesseractError:
        data = pytesseract.image_to_data(
            gray, lang="eng", output_type=Output.DICT, config="--psm 11"
        )

    scale = 1.5
    raw = []
    for i, value in enumerate(data["text"]):
        value = value.strip()
        confidence = float(data["conf"][i]) if str(data["conf"][i]) != "-1" else -1
        if not value or confidence < 35:
            continue
        left = int(int(data["left"][i]) / scale)
        top = int(int(data["top"][i]) / scale)
        width = int(int(data["width"][i]) / scale)
        height = int(int(data["height"][i]) / scale)
        raw.append((value, left, top, left + width, top + height))

    # Tesseract 常把中文名称拆成数块；合并处于同一行且距离较近的文字。
    rows: list[list[tuple[str, int, int, int, int]]] = []
    for item in sorted(raw, key=lambda x: (x[2], x[1])):
        center_y = (item[2] + item[4]) // 2
        for row in rows:
            row_y = sum((x[2] + x[4]) // 2 for x in row) / len(row)
            if abs(center_y - row_y) <= max(12, (item[4] - item[2])):
                row.append(item)
                break
        else:
            rows.append([item])

    targets: list[TextTarget] = []
    for row in rows:
        row.sort(key=lambda x: x[1])
        groups: list[list[tuple[str, int, int, int, int]]] = []
        for item in row:
            if not groups or item[1] - groups[-1][-1][3] > 30:
                groups.append([item])
            else:
                groups[-1].append(item)
        for group in groups:
            text = "".join(x[0] for x in group)
            score = max((match_score(text, label) for label in labels), default=0)
            if not score:
                continue
            left = min(x[1] for x in group)
            right = max(x[3] for x in group)
            top = min(x[2] for x in group)
            bottom = max(x[4] for x in group)
            targets.append(
                TextTarget(
                    text,
                    rect.left + (left + right) // 2,
                    rect.top + (top + bottom) // 2,
                    "ocr",
                    score,
                )
            )
    return targets


def find_all_text(
    pyautogui,
    window,
    labels: Sequence[str],
    tesseract_cmd: str | None,
    timeout: float,
) -> list[TextTarget]:
    """返回所有匹配候选（不只是最佳匹配）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = find_with_uia(window, labels)
        try:
            extra = ocr_targets(pyautogui, window, labels, tesseract_cmd)
            candidates.extend(extra)
        except Exception:
            pass
        if candidates:
            return candidates
        time.sleep(0.35)
    return []


def find_text(
    pyautogui,
    window,
    labels: Sequence[str],
    tesseract_cmd: str | None,
    timeout: float,
) -> TextTarget | None:
    candidates = find_all_text(pyautogui, window, labels, tesseract_cmd, timeout)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.score, -item.y))


def click_text(
    pyautogui,
    window,
    labels: Sequence[str],
    tesseract_cmd: str | None,
    timeout: float,
) -> TextTarget:
    target = find_text(pyautogui, window, labels, tesseract_cmd, timeout)
    if target is None:
        raise RuntimeError(f"没有识别到：{' / '.join(labels)}")
    pyautogui.click(target.x, target.y)
    LOG.info("点击 %r，识别来源=%s", target.text, target.source)
    return target


def wechat_search_and_open_h5(pyautogui, wechat, account: str, args) -> None:
    """在 WeChat 主窗口按 Ctrl+F 搜索，点击'搜一搜'入口打开 H5 窗口。"""
    focus_window(wechat)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)
    rect = wechat.rectangle()
    pyautogui.click(rect.left + int(rect.width() * 0.25), rect.top + 35)
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("delete")
    time.sleep(0.1)
    set_clipboard_text(account)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(args.search_wait)

    entry = find_text(
        pyautogui, wechat, args.search_label, args.tesseract_cmd, 3.0
    )
    if not entry:
        raise RuntimeError(f"在微信搜索结果中找不到'搜一搜'入口（搜索：{account}）")
    pyautogui.click(entry.x, entry.y)
    LOG.info("已点击搜一搜入口，等待 H5 窗口打开...")
    time.sleep(args.page_wait)


def h5_click_gongzhonghao_tab(pyautogui, search_window, args) -> None:
    """在搜一搜主页面点击「公众号」标签，这样搜索结果只显示公众号。

    这是搜一搜主页面的顶级标签（截图1），不是搜索结果页的子标签。
    """
    focus_window(search_window)
    # 先找搜一搜主页面的「公众号」标签
    # 标签栏在搜索框下方：全部 | 视频号 | 文章 | 表情 | 公众号 | 小程序 | 朋友圈
    tab = find_text(
        pyautogui, search_window, ["公众号"], args.tesseract_cmd, 2.0
    )
    if not tab:
        raise RuntimeError("找不到搜一搜主页面的「公众号」标签")
    pyautogui.click(tab.x, tab.y)
    LOG.info("已点击「公众号」标签，限定搜索范围")
    time.sleep(args.page_wait)


def _click_search_box(pyautogui, search_window, args, prev_account: str | None) -> bool:
    """点击搜索框输入区。

    关键：必须确保光标真的进入了输入框。
    方法：找到「搜索」按钮的位置，点击它左边足够远的位置（避开上传图标）。
    点击后通过键盘验证：按一个空格看是否能输入。
    """
    rect = search_window.rectangle()

    # 找「搜索」按钮
    all_btns = find_all_text(
        pyautogui, search_window, ["搜索"], args.tesseract_cmd, 1.5
    )

    if all_btns:
        search_btn = min(all_btns, key=lambda t: t.y)
        # 搜索框左侧约 5%~55% 宽度是输入文字区
        # 点击窗口宽度的 15% 处（足够靠左，避开上传图标和搜索按钮）
        click_x = rect.left + int(rect.width() * 0.15)
        click_y = search_btn.y
        pyautogui.click(click_x, click_y)
        time.sleep(0.05)

        # 验证是否聚焦成功：按空格再看是否能删除
        pyautogui.press("space")
        time.sleep(0.04)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.02)
        pyautogui.press("delete")
        time.sleep(0.04)

        LOG.info("已点击搜索框输入区：(%d, %d) (窗口宽%d)", click_x, click_y, rect.width())
        return True

    # 兜底：搜索框通常在窗口高度 30% 位置
    box_x = rect.left + int(rect.width() * 0.15)
    box_y = rect.top + int(rect.height() * 0.30)
    pyautogui.click(box_x, box_y)
    time.sleep(0.08)
    LOG.warning("未找到「搜索」按钮，使用兜底位置：(%d, %d)", box_x, box_y)
    return True


def _wait_for_search_results(pyautogui, search_window, before_pixels, max_wait: float = 8.0) -> None:
    """等待搜索结果加载完成：至少等 0.8s，然后检测页面停止变化。

    before_pixels: Enter 前的截图像素，用于判断页面是否开始刷新。
    """
    rect = search_window.rectangle()
    w = rect.width()
    h = rect.height()

    sample_x = rect.left + int(w * 0.35)
    sample_y = rect.top + int(h * 0.30)
    sample_w, sample_h = 50, 30

    # 先固定等 0.5s（搜索结果通常 0.5s 内开始加载）
    time.sleep(0.5)

    start = time.monotonic()
    prev_pixels = None
    stable_count = 0

    while time.monotonic() - start < max_wait:
        img = pyautogui.screenshot(region=(sample_x, sample_y, sample_w, sample_h))
        gray = img.convert("L")
        pixels = list(gray.getdata())

        # 检测页面是否还在变化
        if prev_pixels is not None:
            diff_count = sum(1 for a, b in zip(pixels, prev_pixels) if abs(a - b) > 10)
            if diff_count < 3:
                stable_count += 1
                if stable_count >= 2:
                    LOG.info("搜索结果已稳定 (等待%.1fs)", time.monotonic() - start + 0.8)
                    return
            else:
                stable_count = 0

        prev_pixels = pixels
        time.sleep(0.15)

    LOG.info("等待搜索结果超时，继续执行 (等待%.1fs)", time.monotonic() - start + 0.8)


def h5_search_account(pyautogui, search_window, account: str, args, prev_account: str | None = None) -> None:
    """在搜一搜 H5 窗口内的搜索框里输入账号名并搜索。

    只按 Enter 触发搜索，自适应等待结果加载完成。
    """
    focus_window(search_window)

    # 1. 点击搜索框
    _click_search_box(pyautogui, search_window, args, prev_account)
    time.sleep(0.08)

    # 2. 清空
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.02)
    pyautogui.press("delete")
    time.sleep(0.04)

    # 3. 输入新账号名
    set_clipboard_text(account)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.20)  # 等粘贴完成

    # 4. Enter 前截图，用于检测页面刷新
    rect = search_window.rectangle()
    sample_x = rect.left + int(rect.width() * 0.35)
    sample_y = rect.top + int(rect.height() * 0.30)
    before_img = pyautogui.screenshot(region=(sample_x, sample_y, 50, 30))
    before_pixels = list(before_img.convert("L").getdata())

    # 5. 按 Enter 触发搜索
    pyautogui.press("enter")

    # 6. 检测页面刷新完成
    _wait_for_search_results(pyautogui, search_window, before_pixels, max_wait=5.0)

    LOG.info("H5 搜索完成：%s", account)


def h5_find_and_click_account(pyautogui, search_window, account: str, args) -> None:
    """直接点击第一个搜索结果的固定位置，不做 OCR 识别。

    所有目标都是第一个搜索结果，位置固定在窗口的约 30% 处。
    """
    rect = search_window.rectangle()
    w = rect.width()
    h = rect.height()

    # 第一个搜索结果的位置：窗口宽度 35%，高度 30%
    # （经过多次验证，所有账号的第一个结果都在这个位置附近）
    click_x = rect.left + int(w * 0.35)
    click_y = rect.top + int(h * 0.30)

    pyautogui.click(click_x, click_y)
    LOG.info("已点击第一个搜索结果 (x=%d, y=%d)：%s", click_x, click_y, account)
    time.sleep(args.page_wait)


def h5_click_message_button(pyautogui, search_window, account: str, args, desktop) -> None:
    """在账号详情页点击「私信/发消息/关注」按钮。

    账号详情页布局（从上到下）：
      [账号头像] [账号名] [政府/事业单位标签]
      [账号简介]
      [绿色「私信」按钮] 或 [绿色「关注」按钮]
      [菜单、小程序入口]

    私信按钮可能在首屏不可见，需要往下滚。
    """
    # 重新获取窗口（点击账号后可能新开标签）
    new_win = find_search_window(desktop)
    if new_win:
        focus_window(new_win)
        search_window = new_win

    rect = search_window.rectangle()
    # 依次尝试每个候选按钮
    candidate_labels = ["私信", "发消息", "关注", "进入公众号"]

    for scroll_attempt in range(5):
        for label in candidate_labels:
            if label not in args.message_label:
                continue
            btn = find_text(
                pyautogui, search_window, [label], args.tesseract_cmd, 1.5
            )
            if btn and btn.score >= 65:
                pyautogui.click(btn.x, btn.y)
                LOG.info("已点击「%s」按钮（score=%.1f, 第%d次尝试）", btn.text, btn.score, scroll_attempt + 1)
                time.sleep(args.chat_wait)
                return

        # 没找到，往下滚一屏
        if scroll_attempt < 4:
            pyautogui.moveTo(
                rect.left + int(rect.width() * 0.50),
                rect.top + int(rect.height() * 0.70),
            )
            pyautogui.scroll(-50)
            time.sleep(0.8)
            LOG.info("未找到消息按钮，向下滚动第 %d 次", scroll_attempt + 1)

    # 彻底找不到，报错并保存截图
    save_debug_screenshot(pyautogui, search_window, "ERROR_no_message_button")
    raise RuntimeError(
        f"在账号详情页找不到任何消息按钮（候选：{args.message_label}）。"
        f"请查看调试截图或用 --message-label 指定按钮文字。"
    )


def h5_go_to_main_page(pyautogui, search_window, args) -> None:
    """切换到第一个标签页（搜索页面）。

    搜索页面始终是第一个标签（最左边）。
    从截图看，第一个标签在导航按钮右边，文字为搜索词。
    直接点击标签栏第一个标签的位置。
    """
    focus_window(search_window)
    rect = search_window.rectangle()

    # 从截图分析：标签栏 [←][→][↻][🔴搜一搜图标][账号标签×][搜索]...
    # 三个导航按钮(← → ↻)约占 70px
    # 红色搜一搜图标中心约在 rect.left + 210, rect.top + 18
    tab_x = rect.left + 215
    tab_y = rect.top + 23

    LOG.info("点击第一个标签位置：(%d, %d)", tab_x, tab_y)
    pyautogui.click(tab_x, tab_y)
    time.sleep(0.5)

    # 保存截图验证
    save_debug_screenshot(pyautogui, search_window, "after_go_main_page")


def reset_to_chat_list(pyautogui, desktop, wechat) -> None:
    """失败后重置：尝试回到搜一搜主页面。"""
    search_win = find_search_window(desktop)
    if search_win:
        try:
            focus_window(search_win)
            tab = find_text(pyautogui, search_win, ["搜一搜"], None, 1.0)
            if tab:
                pyautogui.click(tab.x, tab.y)
                time.sleep(0.3)
        except Exception:
            pass
    try:
        focus_window(wechat)
    except Exception:
        pass


def open_private_chat(pyautogui, wechat, desktop, account: str, args, first_account: bool = False, prev_account: str | None = None) -> None:
    # 1. 确保搜一搜 H5 窗口存在
    search_window = find_search_window(desktop)
    if not search_window:
        wechat_search_and_open_h5(pyautogui, wechat, account, args)
        for _ in range(10):
            time.sleep(0.5)
            search_window = find_search_window(desktop)
            if search_window:
                break
        if not search_window:
            raise RuntimeError("无法打开搜一搜 H5 窗口")

    # 2. 回到搜一搜主页面（确保在起点）
    h5_go_to_main_page(pyautogui, search_window, args)
    save_debug_screenshot(pyautogui, search_window, "01_main_page")

    # 3. 在主页面点击「公众号」顶级标签（仅第一个账号需要，后续账号已在公众号分类）
    if first_account:
        h5_click_gongzhonghao_tab(pyautogui, search_window, args)
        save_debug_screenshot(pyautogui, search_window, "02_gongzhonghao_tab")

    # 4. 在 H5 搜索框输入账号名并搜索
    h5_search_account(pyautogui, search_window, account, args, prev_account=prev_account)
    save_debug_screenshot(pyautogui, search_window, "03_search_result")

    # 5. 找到并点击账号
    h5_find_and_click_account(pyautogui, search_window, account, args)
    time.sleep(args.page_wait)  # 等待详情页加载
    save_debug_screenshot(pyautogui, search_window, "04_after_click_account")

    # 6. 重新获取窗口（点击账号后可能新开标签页）
    current_win = find_search_window(desktop)
    if current_win:
        search_window = current_win
        focus_window(search_window)

    save_debug_screenshot(pyautogui, search_window, "05_detail_page")

    # 7. 点击「私信/发消息/关注」按钮
    h5_click_message_button(pyautogui, search_window, account, args, desktop)

    # 8. 验证聊天框已打开
    title = find_text(pyautogui, search_window, [account], args.tesseract_cmd, 2.0)
    if title is None:
        focus_window(wechat)
        title = find_text(pyautogui, wechat, [account], args.tesseract_cmd, 2.0)
    if title is None:
        raise RuntimeError("点击后未能确认聊天框已经打开")

    # 不在这里回搜一搜主页，等菜单点完后再回


def read_accounts(path: Path, name_column: str | None) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"列表文件不存在：{path}")
    accounts: list[str] = []
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            if name_column:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or name_column not in reader.fieldnames:
                    raise ValueError(f"CSV 中没有列：{name_column}")
                values: Iterable[str] = (row.get(name_column, "") for row in reader)
            else:
                reader = csv.reader(stream)
                values = (row[0] if row else "" for row in reader)
            accounts.extend(value.strip() for value in values if value.strip())
    else:
        with path.open("r", encoding="utf-8-sig") as stream:
            accounts.extend(
                line.strip()
                for line in stream
                if line.strip() and not line.lstrip().startswith("#")
            )
    # 保持原顺序去重。
    return list(dict.fromkeys(accounts))


def write_result(path: Path, result: AccountResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")


def split_labels(value: str) -> list[str]:
    return [item.strip() for item in value.split("|") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="逐个搜索公众号并打开私信聊天框")
    parser.add_argument(
        "account_file",
        nargs="?",
        type=Path,
        default=APP_DIR / "accounts.txt",
        help="服务号列表，默认读取程序旁边的 accounts.txt",
    )
    parser.add_argument("--name-column", help="CSV 中公众号名称所在的列名")
    # 默认使用项目自带的 Tesseract-OCR
    _default_tesseract = RESOURCE_DIR / "Tesseract-OCR" / "tesseract.exe"
    parser.add_argument("--tesseract-cmd", default=str(_default_tesseract) if _default_tesseract.exists() else None, help="tesseract.exe 路径（默认用项目自带的）")
    parser.add_argument("--search-label", type=split_labels, default=["搜一搜"], help="搜一搜按钮候选文案，以 | 分隔")
    parser.add_argument("--category-label", type=split_labels, default=["公众号", "公众号搜索"], help="公众号分类候选文案")
    parser.add_argument("--message-label", type=split_labels, default=["私信", "发消息"], help="进入聊天按钮候选文案")
    parser.add_argument("--max-scrolls", type=int, default=8, help="每个公众号最多滚动次数")
    parser.add_argument("--scroll-amount", type=int, default=50, help="每次滚轮格数")
    parser.add_argument("--search-wait", type=float, default=0.5)
    parser.add_argument("--page-wait", type=float, default=0.3)
    parser.add_argument("--result-wait", type=float, default=0.4)
    parser.add_argument("--scroll-wait", type=float, default=0.2)
    parser.add_argument("--chat-wait", type=float, default=0.4)
    parser.add_argument("--control-timeout", type=float, default=1.5)
    parser.add_argument("--between-accounts", type=float, default=0.1)
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument(
        "--log-file",
        type=Path,
        default=PROJECT_DIR / "wechat_open_service_chats.jsonl",
    )
    return parser


def _cleanup_old_records() -> None:
    """启动时清除上次的调试截图和日志文件。"""
    import shutil
    # 清空 debug 文件夹
    if DEBUG_DIR.exists():
        shutil.rmtree(DEBUG_DIR)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    # 删除日志文件
    for pattern in ["wechat_open_service_chats.jsonl", "wechat_menu_clicks.jsonl", "wechat_open_service_chats.log"]:
        f = PROJECT_DIR / pattern
        if f.exists():
            f.unlink()
    LOG.info("已清除上次记录")


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    _cleanup_old_records()
    accounts = read_accounts(args.account_file, args.name_column)
    if not accounts:
        LOG.error("公众号列表为空")
        return 2

    pyautogui, Desktop = import_dependencies()
    pyautogui.FAILSAFE = True
    desktop = Desktop(backend="uia")
    wechat = find_wechat_window(desktop)
    failures = 0
    prev_account: str | None = None

    # 构建 menu_runner 的参数命名空间
    menu_args = menu_runner.build_parser().parse_args([
        "--tesseract-cmd", str(args.tesseract_cmd) if args.tesseract_cmd else "",
        "--log-file", str(PROJECT_DIR / "wechat_menu_clicks.jsonl"),
    ])

    for index, account in enumerate(accounts, start=1):
        LOG.info("[%d/%d] 开始处理：%s", index, len(accounts), account)
        try:
            # 阶段 1：打开私信窗口
            open_private_chat(pyautogui, wechat, desktop, account, args, first_account=(index == 1), prev_account=prev_account)

            # 阶段 2：在私信窗口里点击菜单
            LOG.info("[%d/%d] 开始点击菜单：%s", index, len(accounts), account)
            try:
                menu_runner.run(menu_args)
            except Exception as menu_exc:
                LOG.warning("[%d/%d] 菜单点击出错（继续下一个账号）：%s", index, len(accounts), account, menu_exc)

            # 阶段 3：菜单点完了，回搜一搜主页面准备下一个
            current_search = find_search_window(desktop)
            if current_search:
                h5_go_to_main_page(pyautogui, current_search, args)
                LOG.info("已回到搜一搜主页面，准备下一个")

            result = AccountResult(
                datetime.now().astimezone().isoformat(timespec="seconds"),
                account,
                "opened",
                "已打开私信聊天框并完成菜单点击",
            )
            LOG.info("[%d/%d] 完成：%s", index, len(accounts), account)
            prev_account = account
        except Exception as exc:
            failures += 1
            result = AccountResult(
                datetime.now().astimezone().isoformat(timespec="seconds"),
                account,
                "failed",
                str(exc),
            )
            LOG.exception("[%d/%d] 失败：%s", index, len(accounts), account)
            reset_to_chat_list(pyautogui, desktop, wechat)  # 失败后重置状态
            if args.stop_on_error:
                write_result(args.log_file, result)
                break
        write_result(args.log_file, result)
        if index < len(accounts):
            time.sleep(args.between_accounts)

    LOG.info("处理结束：成功 %d，失败 %d", len(accounts) - failures, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
