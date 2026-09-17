# -*- coding: utf-8 -*-
"""pytest example: run an app inside the hidden sandbox and drive it with
message-level input. The real mouse/keyboard are NEVER touched.

Uses notepad.exe (present on every Windows) as the demo AUT.

Run:  python -m pytest examples/pytest_sandbox_example.py -v
"""
import ctypes
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import sandbox as sb  # noqa: E402

NOTEPAD = os.path.join(os.environ["SystemRoot"], "notepad.exe")


@pytest.fixture
def notepad():
    s = sb.SandboxSession(name="pytest-np", mode="ghost")
    hwnd = s.start(NOTEPAD, title_re="记事本|Notepad")
    yield s, hwnd
    s.stop(kill=True)


def test_window_is_isolated(notepad):
    s, hwnd = notepad
    # ghosted: on a monitor, but invisible + click-through + no taskbar
    assert sb.is_ghosted(hwnd)
    assert not sb.visible_to_user(hwnd)
    assert s.proc.poll() is None  # process alive


def test_screenshot_captures_content(notepad, tmp_path):
    s, hwnd = notepad
    sb.wm_settext(s.find_child(cls="Edit"), "SCREENSHOT PROOF")
    time.sleep(0.4)  # ghost windows repaint live, give it a beat
    path = s.screenshot(out=str(tmp_path / "np.png"))
    from PIL import Image
    img = Image.open(path)
    assert img.size[0] > 300 and img.size[1] > 200
    assert img.convert("L").getextrema()[0] < 120  # has dark pixels (text)


def test_text_roundtrip_and_keyboard(notepad):
    s, hwnd = notepad
    edit = s.find_child(cls="Edit")
    sb.wm_settext(edit, "ABC")            # replace-mode typing
    sb.msg_type(edit, "XY")               # WM_CHAR stream inserts at caret

    def _readback(expected, timeout=2.0):  # WM_CHAR is posted async: poll
        deadline = time.time() + timeout
        got = None
        while time.time() < deadline:
            got = sb.wm_gettext(edit)
            if got == expected:
                return got
            time.sleep(0.05)
        return got

    assert _readback("XYABC") == "XYABC"
    sb.msg_keys(edit, "ctrl+end")         # modifier combo via messages
    sb.msg_type(edit, "Z")
    assert _readback("XYABCZ") == "XYABCZ"


def _help_about_command_id(hwnd):
    """Derive notepad 帮助→关于 的 WM_COMMAND id（从菜单动态取，免写死）。"""
    u32 = ctypes.windll.user32
    menu = u32.GetMenu(ctypes.c_void_p(hwnd))
    if not menu:
        return None
    n_top = u32.GetMenuItemCount(ctypes.c_void_p(menu))
    help_sub = u32.GetSubMenu(ctypes.c_void_p(menu), n_top - 1)
    n_items = u32.GetMenuItemCount(ctypes.c_void_p(help_sub))
    mid = u32.GetMenuItemID(ctypes.c_void_p(help_sub), n_items - 1)
    return None if mid in (0xFFFFFFFF, -1) else int(mid)


def test_auto_sweep_catches_dialog(notepad):
    """s.start() 默认开启的看门狗：AUT 弹出的对话框必须被自动隔离。

    这是"对话框泄漏"事故（弹窗正常可见还抢焦点）的回归测试：
    帮助→关于 弹出模态对话框后，不看门狗干预的话它会一直以正常样式
    挂在用户屏幕上；看门狗应在 ~interval(0.4s) 内把它 ghost 化。
    """
    s, hwnd = notepad
    about_id = _help_about_command_id(hwnd)
    if about_id is None:
        pytest.skip("AUT 无经典菜单（Win11 新版记事本）")
    sb.send_command(hwnd, about_id)       # 弹出模态"关于"对话框

    dlg = None
    deadline = time.time() + 3.0
    while time.time() < deadline and dlg is None:
        for h, pid, cls, title, _v in sb.enum_windows():
            if pid == s.pid and h != hwnd and cls == "#32770":
                dlg = h
                break
        time.sleep(0.1)
    assert dlg, "About dialog did not appear"

    deadline = time.time() + 3.0
    while time.time() < deadline and not sb.is_ghosted(dlg):
        time.sleep(0.1)
    assert sb.is_ghosted(dlg), "auto-sweep watchdog failed to isolate dialog"
    assert not sb.visible_to_user(dlg)
    sb.post(dlg, sb.WM_CLOSE)             # 收尾：关掉模态框


def test_ghost_window_never_takes_foreground(notepad):
    """焦点保护回归：隔离后的 AUT 窗口必须永远抢不走前台。

    实测事故：ghost 只做隐身，缺 WS_EX_NOACTIVATE + 焦点归还时，
    AUT 启动/弹窗会抢走用户前台且一直不还——用户键盘全打进隐身窗口，
    测试循环期间表现为「焦点不停地被切换」。
    """
    s, hwnd = notepad
    fg = ctypes.windll.user32.GetForegroundWindow()
    # 1) 启动抢走的焦点已被 isolate() 归还：前台不属于 AUT
    assert sb.get_window_pid(fg) != s.pid
    # 2) 隔离窗口带 WS_EX_NOACTIVATE
    ex = ctypes.windll.user32.GetWindowLongW(
        ctypes.c_void_p(hwnd), -20)  # GWL_EXSTYLE
    assert ex & 0x08000000, "ghost window lacks WS_EX_NOACTIVATE"
    # 3) 就算被显式 SetForegroundWindow（AUT 代码顽固抢焦点），
    #    看门狗的 EVENT_SYSTEM_FOREGROUND 钩子也会立即归还
    ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    deadline = time.time() + 2.0
    while time.time() < deadline:
        fg = ctypes.windll.user32.GetForegroundWindow()
        if sb.get_window_pid(fg) != s.pid:
            break
        time.sleep(0.05)
    fg = ctypes.windll.user32.GetForegroundWindow()
    assert sb.get_window_pid(fg) != s.pid, \
        "AUT still owns the foreground 2s after an explicit steal"
