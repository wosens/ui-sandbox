# -*- coding: utf-8 -*-
# Copyright (c) 2026 梅文海. All rights reserved. Created: 2026-08-28.
"""
ui-sandbox core library
=======================
Hidden UI test sandbox for Windows: run a GUI app ISOLATED from the real
mouse/keyboard, keep it invisible, and drive it with message-level input.

Three isolation primitives (combined by `mode`):
  1. offscreen  : move the window beyond the virtual screen (SWP_NOACTIVATE)
                  + remove its taskbar button (ITaskbarList::DeleteTab)
  2. vd         : additionally move the window to a dedicated virtual desktop
                  (needs pyvda; windows on other VDs are invisible but stay
                  reachable by UIA / window messages)
  3. stealth    : additionally set WS_EX_TOOLWINDOW (removes from Alt-Tab)

Input is simulated at the MESSAGE level (PostMessage WM_LBUTTONDOWN /
WM_CHAR / WM_COMMAND / BM_CLICK) or via UIA patterns (Invoke/Select/Value),
so the physical cursor never moves and the user's focus is never stolen.

Screenshots use PrintWindow(PW_RENDERFULLCONTENT), which works while the
window is offscreen / on another virtual desktop.

Dependencies: pywin32, Pillow. Optional: pyvda (vd mode), pywinauto (uia_*).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import struct
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------- constants

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_COMMAND = 0x0111
WM_SYSCOMMAND = 0x0112
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_CLOSE = 0x0010

BM_CLICK = 0x00F5
MK_LBUTTON = 0x0001

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020  # click-through for layered windows
WS_EX_NOACTIVATE = 0x08000000   # window can never become foreground
WS_VISIBLE = 0x10000000
WS_MINIMIZE = 0x20000000
SW_SHOWNOACTIVATE = 4
LWA_ALPHA = 2

GA_ROOT = 2
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
EVENT_SYSTEM_FOREGROUND = 0x0003  # win-event: a window became the foreground
EVENT_OBJECT_SHOW = 0x8003        # win-event: a window just became visible
WINEVENT_OUTOFCONTEXT = 0x0000
INFINITE = 0xFFFFFFFF

# 64-bit-safe HWND returns (default c_int restype truncates big handles)
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.GetAncestor.restype = ctypes.c_void_p

SMTO_ABORTIFHUNG = 0x0002

# VK table for keys() -- name -> virtual key code
_VK = {
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "alt": 0x12, "menu": 0x12,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08, "bs": 0x08,
    "delete": 0x2E, "del": 0x2E, "ins": 0x2D, "insert": 0x2D,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pgdn": 0x22, "pageup": 0x21, "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
    "capslock": 0x14, "numlock": 0x90,
    "shift+tab": None,  # handled by parser
}
_MODIFIERS = {0x11, 0x10, 0x12, 0x5B, 0x5C}


class SandboxError(RuntimeError):
    pass


# ---------------------------------------------------------------- dpi setup

def _enable_dpi_awareness() -> None:
    """Per-Monitor-V2 awareness so rectangles/captures are physical pixels."""
    try:
        val = ctypes.c_void_p(-4)  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(val):
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()


# ---------------------------------------------------------------- low level

def _make_lparam(x: int, y: int) -> int:
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def get_window_rect(hwnd: int) -> tuple:
    rc = wt.RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rc)):
        raise SandboxError(f"GetWindowRect failed for hwnd=0x{hwnd:X}")
    return rc.left, rc.top, rc.right, rc.bottom


def get_client_rect(hwnd: int) -> tuple:
    rc = wt.RECT()
    if not user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rc)):
        raise SandboxError(f"GetClientRect failed for hwnd=0x{hwnd:X}")
    return rc.left, rc.top, rc.right, rc.bottom


def client_to_screen(hwnd: int, x: int, y: int) -> tuple:
    pt = wt.POINT(x, y)
    user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt))
    return pt.x, pt.y


def get_window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 2)
    return buf.value


def get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
    return buf.value


def get_window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value


def is_window_visible(hwnd: int) -> bool:
    return bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd)))


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(ctypes.c_void_p(hwnd)))


def enum_windows() -> list:
    """All top-level windows -> [(hwnd, pid, cls, title, visible)]
    (IME helper windows like Sogou's SoPY_* are filtered out)."""
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        if not _is_ime_class(get_class_name(hwnd)):
            out.append((hwnd, get_window_pid(hwnd), get_class_name(hwnd),
                        get_window_text(hwnd), is_window_visible(hwnd)))
        return True

    user32.EnumWindows(cb, 0)
    return out


# Helper windows injected by IMEs (Sogou etc.) into every process -- they
# share the AUT's pid but belong to the input method; never touch them.
_IME_CLASS_PREFIXES = ("sopy", "sogou", "class_sogou", "sgtool", "sogoucomp")
_IME_CLASS_NAMES = {"default ime", "msctfime ui", "ime"}


def _is_ime_class(cls: str) -> bool:
    c = (cls or "").lower()
    if c in _IME_CLASS_NAMES:
        return True
    return any(c.startswith(p) for p in _IME_CLASS_PREFIXES) or "ime" in c


def enum_child_windows(parent: int) -> list:
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _l):
        out.append((hwnd, get_class_name(hwnd), get_window_text(hwnd)))
        return True

    user32.EnumChildWindows(ctypes.c_void_p(parent), cb, 0)
    return out


def post(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
    return bool(user32.PostMessageW(ctypes.c_void_p(hwnd), msg,
                                    ctypes.c_size_t(wparam),
                                    ctypes.c_ssize_t(lparam)))


def post_text(hwnd: int, msg: int, wparam: int, text: str) -> bool:
    """Send a string-lparam message (WM_SETTEXT etc.). MUST be synchronous:
    PostMessage is async and the string buffer would dangle after return
    (classic bug: target reads freed memory)."""
    buf = ctypes.c_wchar_p(text)
    send_timeout(hwnd, msg, wparam, buf)
    return True


def send_timeout(hwnd: int, msg: int, wparam: int, lparam, ms: int = 2000):
    res = ctypes.c_ssize_t(0)
    ok = user32.SendMessageTimeoutW(ctypes.c_void_p(hwnd), msg,
                                    ctypes.c_size_t(wparam), lparam,
                                    SMTO_ABORTIFHUNG, ms, ctypes.byref(res))
    if not ok:
        raise SandboxError(f"SendMessageTimeout(hwnd=0x{hwnd:X}, msg=0x{msg:X}) "
                           f"timed out or failed (hung window?)")
    return res.value


def wm_gettext(hwnd: int) -> str:
    """Read control text via WM_GETTEXT (works for Edit/Button/Static...)."""
    n = send_timeout(hwnd, WM_GETTEXTLENGTH, 0, 0)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    send_timeout(hwnd, WM_GETTEXT, n + 1, ctypes.byref(buf))
    return buf.value


def wm_settext(hwnd: int, text: str) -> bool:
    return post_text(hwnd, WM_SETTEXT, 0, text)


# ------------------------------------------------------- isolation helpers

def get_foreground_window() -> int:
    """Current foreground window (0 when none). 64-bit-safe."""
    return user32.GetForegroundWindow() or 0


def get_window_tid(hwnd: int) -> int:
    return user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), None)


def restore_focus_to(hwnd: int) -> bool:
    """Best-effort give the FOREGROUND back to a user window.

    SetForegroundWindow alone is refused unless the caller already owns the
    foreground, so attach to the current foreground + target thread input
    queues first (classic AttachThreadInput trick used by automation libs).
    Used after isolating an AUT window that had stolen the focus: the user
    keeps typing in their own window without ever noticing the sandbox."""
    if not hwnd or not is_window(hwnd):
        return False
    if get_foreground_window() == hwnd:
        return True
    fg = get_foreground_window()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = get_window_tid(fg) if fg else 0
    tgt_tid = get_window_tid(hwnd)
    at1 = user32.AttachThreadInput(cur_tid, fg_tid, True) if fg_tid else 0
    at2 = user32.AttachThreadInput(cur_tid, tgt_tid, True) if tgt_tid else 0
    try:
        ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
        if at2:
            user32.SetFocus(ctypes.c_void_p(hwnd))  # belt & braces
    finally:
        if at2:
            user32.AttachThreadInput(cur_tid, tgt_tid, False)
        if at1:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    return ok


def virtual_screen_rect() -> tuple:
    l = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    t = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return l, t, l + w, t + h


def move_offscreen(hwnd: int, margin: int = 100) -> tuple:
    """Move window fully beyond the virtual screen (keeps size, no activate).
    NOTE: offscreen windows stop receiving WM_PAINT -- captures freeze at the
    last rendered frame. Prefer mode='ghost' when you need live screenshots."""
    ex = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE,
                          ex | WS_EX_NOACTIVATE)  # can never steal focus
    vs_l, vs_t, vs_r, vs_b = virtual_screen_rect()
    # pick the nearest edge outside all monitors
    x, y = vs_r + margin, vs_t
    if not user32.SetWindowPos(ctypes.c_void_p(hwnd), None, x, y, 0, 0,
                               SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE):
        raise SandboxError(f"SetWindowPos failed for hwnd=0x{hwnd:X}")
    return get_window_rect(hwnd)


def ghostify(hwnd: int) -> None:
    """GHOST MODE -- the core isolation primitive.

    Keeps the window ON a real monitor (so it keeps receiving WM_PAINT and the
    DWM surface stays live for PrintWindow capture) while making it:
      - invisible to the user  : WS_EX_LAYERED + alpha = 1/255
      - non-interactive       : WS_EX_TRANSPARENT (all mouse events pass through)
      - never focusable       : WS_EX_NOACTIVATE (SetForegroundWindow on it
                                 fails, so the AUT can never steal the user's
                                 foreground, even from its own code)
      - absent from taskbar    : ITaskbarList::DeleteTab
      - absent from Alt-Tab    : WS_EX_TOOLWINDOW
    The window never takes focus, never blocks the real cursor, and its
    z-order stays at the bottom."""
    ex = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    user32.SetWindowLongW(
        ctypes.c_void_p(hwnd), GWL_EXSTYLE,
        ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
        | WS_EX_NOACTIVATE)
    user32.SetLayeredWindowAttributes(ctypes.c_void_p(hwnd), 0, 1, LWA_ALPHA)
    vs_l, vs_t, vs_r, vs_b = virtual_screen_rect()
    user32.SetWindowPos(ctypes.c_void_p(hwnd), 1,  # HWND_BOTTOM
                        vs_l + 8, vs_t + 8, 0, 0,
                        SWP_NOSIZE | SWP_NOACTIVATE)
    remove_from_taskbar(hwnd)


def is_ghosted(hwnd: int) -> bool:
    """True when the window is a layered window with (near-)zero alpha."""
    ex = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    if not (ex & WS_EX_LAYERED):
        return False
    alpha = wt.BYTE(255)
    flags = wt.DWORD(0)
    key = wt.COLORREF(0)
    if user32.GetLayeredWindowAttributes(ctypes.c_void_p(hwnd),
                                         ctypes.byref(key), ctypes.byref(alpha),
                                         ctypes.byref(flags)):
        return alpha.value <= 8
    return False


def deghost(hwnd: int) -> None:
    """Undo ghostify (restore full opacity + normal ex-style)."""
    ex = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    user32.SetWindowLongW(
        ctypes.c_void_p(hwnd), GWL_EXSTYLE,
        ex & ~(WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
               | WS_EX_NOACTIVATE))


def remove_from_taskbar(hwnd: int) -> bool:
    """ITaskbarList::DeleteTab -- remove the taskbar button (reversible by
    the app itself if it re-registers; call again after sweeps)."""
    try:
        import pythoncom
        from win32com.client import Dispatch
        pythoncom.CoInitialize()
        tb = Dispatch("{56FDF344-FD6D-11d0-958A-006097C9A090}")
        tb.DeleteTab(hwnd)
        return True
    except Exception:
        return False


def add_toolwindow(hwnd: int) -> bool:
    """WS_EX_TOOLWINDOW: removes the window from Alt-Tab / taskbar.
    Slightly intrusive (affects some apps), opt-in via mode 'stealth'."""
    ex = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    return bool(user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE,
                                      ex | WS_EX_TOOLWINDOW))


def on_screen(hwnd: int) -> bool:
    l, t, r, b = get_window_rect(hwnd)
    vs_l, vs_t, vs_r, vs_b = virtual_screen_rect()
    return not (r <= vs_l or b <= vs_t or l >= vs_r or t >= vs_b)


def visible_to_user(hwnd: int) -> bool:
    """True when this window could actually disturb the user: on a monitor,
    not ghosted. Ghost windows sit on a monitor but are invisible and
    click-through, so they are harmless."""
    return on_screen(hwnd) and not is_ghosted(hwnd)


def is_minimized(hwnd: int) -> bool:
    style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE)
    return bool(style & WS_MINIMIZE)


def ensure_restored(hwnd: int) -> bool:
    """If the window got minimized (rect -32000 / capture would be garbage),
    restore WITHOUT activating it, then push it offscreen again.
    Returns True when a restore happened."""
    if not is_minimized(hwnd):
        return False
    user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
    time.sleep(0.15)
    if on_screen(hwnd):
        move_offscreen(hwnd)
        remove_from_taskbar(hwnd)
    return True


def move_to_virtual_desktop(hwnd: int, desktop_number: int | None = None) -> dict:
    """Move window to another virtual desktop. Requires pyvda (Win10+).
    If desktop_number is None: use the last desktop, creating one when the
    system only has a single desktop. Returns {'number': n, 'created': bool}"""
    try:
        import pyvda
    except ImportError:
        raise SandboxError("vd mode requires pyvda: pip install pyvda")
    desktops = pyvda.get_virtual_desktops()
    created = False
    if len(desktops) == 1:
        vd = desktops[0].create() if hasattr(desktops[0], "create") else None
        if vd is None:
            raise SandboxError("pyvda cannot create a virtual desktop on this build")
        created = True
        desktops = pyvda.get_virtual_desktops()
    if desktop_number is None:
        desktop_number = desktops[-1].number  # last one == far from user's current
    vd = pyvda.VirtualDesktop(number=desktop_number)
    pyvda.AppView(hwnd=hwnd).move(vd)
    return {"number": desktop_number, "created": created}


def is_on_current_virtual_desktop(hwnd: int) -> bool:
    try:
        import pyvda
        return bool(pyvda.AppView(hwnd=hwnd).is_on_current_desktop())
    except Exception:
        return True  # assume yes when pyvda unavailable


# ------------------------------------------------------------- screenshots

def capture_window(hwnd: int, path: str, client_only: bool = False) -> str:
    """PrintWindow capture; works offscreen / on other virtual desktops.
    Returns the path. Raises SandboxError when both PrintWindow and the
    BitBlt fallback fail (typical for exclusive-fullscreen / some GPU apps)."""
    from PIL import Image

    hwnd = int(hwnd)
    ensure_restored(hwnd)  # minimized windows capture as 160x28 garbage
    if client_only:
        cl, ct, cr, cb = get_client_rect(hwnd)
        sx, sy = client_to_screen(hwnd, 0, 0)
        w, h = cr - cl, cb - ct
    else:
        l, t, r, b = get_window_rect(hwnd)
        w, h = r - l, b - t
    if w <= 0 or h <= 0:
        raise SandboxError(f"window 0x{hwnd:X} has empty rect ({w}x{h})")

    hdc_window = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    if not hdc_window:
        raise SandboxError(f"GetWindowDC failed for 0x{hwnd:X}")
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_window)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_window, w, h)
        old = gdi32.SelectObject(hdc_mem, hbmp)
        try:
            flags = PW_RENDERFULLCONTENT
            if client_only:
                flags |= PW_CLIENTONLY
            ok = user32.PrintWindow(ctypes.c_void_p(hwnd), hdc_mem, flags)
            if not ok:  # legacy fallback (works when window is composited)
                gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_window, 0, 0, 0x00CC0020)
            # extract bits
            class _BMIH(ctypes.Structure):
                _fields_ = [("biSize", wt.UINT), ("biWidth", wt.LONG),
                            ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                            ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                            ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                            ("biClrImportant", wt.DWORD)]

            class _BMI(ctypes.Structure):
                _fields_ = [("bmiHeader", _BMIH), ("bmiColors", wt.DWORD * 3)]

            bmi = _BMI()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BMIH)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h  # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            got = gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
            if not got:
                raise SandboxError("GetDIBits failed for 0x%X" % hwnd)
            img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
            # detect fully-black capture (GPU apps often fail offscreen)
            ex = img.getextrema()
            if all(lo == hi == 0 for lo, hi in ex):
                img.save(path)  # still save for debugging
                raise SandboxError(
                    f"capture of 0x{hwnd:X} is all black (hardware-rendered "
                    f"content? try client_only=True, or run on current desktop")
            img.save(path)
            return path
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
    finally:
        user32.ReleaseDC(ctypes.c_void_p(hwnd), hdc_window)


# ------------------------------------------------------------- input (msg)

def msg_click(hwnd: int, x: int = 0, y: int = 0, button: str = "left",
              double: bool = False) -> None:
    """Message-level mouse click at CLIENT coordinates. Does not move the
    real cursor, does not need focus. Apps that read GetCursorPos()/use
    mouse capture may not respond -- prefer uia_click for those."""
    down, up = {
        "left": (WM_LBUTTONDOWN, WM_LBUTTONUP),
        "right": (WM_RBUTTONDOWN, WM_RBUTTONUP),
        "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP),
    }[button]
    lp = _make_lparam(x, y)
    flag = MK_LBUTTON if button == "left" else 0
    post(hwnd, down, flag, lp)
    time.sleep(0.02)
    post(hwnd, up, 0, lp)
    if double:
        time.sleep(0.02)
        post(hwnd, down, flag, lp)
        time.sleep(0.02)
        post(hwnd, up, 0, lp)


def bm_click(hwnd: int) -> None:
    """BM_CLICK for standard BUTTON controls -- most reliable msg-level click.
    Uses SendMessageTimeout: posted BM_CLICK can get lost in modal dialog
    loops (verified on a Win10 #32770 error dialog)."""
    if get_class_name(hwnd).lower() != "button":
        raise SandboxError(f"0x{hwnd:X} is not a Button (class="
                           f"{get_class_name(hwnd)}); use msg_click/uia_click")
    send_timeout(hwnd, BM_CLICK, 0, 0)


def msg_move(hwnd: int, x: int, y: int) -> None:
    post(hwnd, WM_MOUSEMOVE, 0, _make_lparam(x, y))


def msg_drag(hwnd: int, x1: int, y1: int, x2: int, y2: int,
             steps: int = 10, hold: float = 0.12) -> None:
    """Message-level drag. Simple cases only (no OLE drag&drop)."""
    post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _make_lparam(x1, y1))
    time.sleep(hold)
    for i in range(1, steps + 1):
        x = x1 + (x2 - x1) * i // steps
        y = y1 + (y2 - y1) * i // steps
        post(hwnd, WM_MOUSEMOVE, MK_LBUTTON, _make_lparam(x, y))
        time.sleep(0.012)
    time.sleep(hold)
    post(hwnd, WM_LBUTTONUP, 0, _make_lparam(x2, y2))


def msg_wheel(hwnd: int, delta: int = -120, screen_x: int | None = None,
              screen_y: int | None = None) -> None:
    """WM_MOUSEWHEEL -- NOTE: lParam uses SCREEN coordinates (Win32 quirk)."""
    if screen_x is None or screen_y is None:
        screen_x, screen_y = client_to_screen(hwnd, 0, 0)
    post(hwnd, WM_MOUSEWHEEL, delta & 0xFFFFFFFF, _make_lparam(screen_x, screen_y))


def msg_type(hwnd: int, text: str, replace: bool = False) -> None:
    """Type text into a control via messages.
    Edit controls: WM_SETTEXT when replace=True (fast & reliable),
    WM_CHAR stream otherwise (works for many custom controls)."""
    cls = get_class_name(hwnd).lower()
    if replace and cls in ("edit", "static"):
        wm_settext(hwnd, text)
        return
    for ch in text:
        if ch == "\n":
            post(hwnd, WM_CHAR, 0x0D, 0)
        else:
            post(hwnd, WM_CHAR, ord(ch) & 0xFFFF, 0)
        time.sleep(0.004)


def _vk_for(token: str) -> int:
    t = token.lower()
    if t in _VK and _VK[t] is not None:
        return _VK[t]
    if len(token) == 1:
        vk = user32.VkKeyExW(ord(token), user32.GetKeyboardLayout(0))
        if vk != 0xFF and vk != 0xFFFFFFFF:
            return vk & 0xFF
        raise SandboxError(f"cannot map key {token!r} to a VK code "
                           f"(layout-dependent char?)")
    raise SandboxError(f"unknown key name: {token!r}")


def msg_keys(hwnd: int, combo: str, times: int = 1) -> None:
    """Message-level key combo, e.g. 'ctrl+s', 'enter', 'ctrl+shift+t'.
    Caveat: controls that check GetKeyState() for modifiers may misbehave;
    menu accelerators via WM_COMMAND are more reliable (see send_command)."""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    mods = [p for p in parts if p.lower() in ("ctrl", "control", "shift",
                                              "alt", "win", "lwin", "rwin")]
    main = [p for p in parts if p not in mods]
    if not main:
        raise SandboxError("key combo needs a non-modifier key")
    use_alt = any(m.lower() in ("alt", "menu") for m in mods)
    down_msg = WM_SYSKEYDOWN if use_alt else WM_KEYDOWN
    up_msg = WM_SYSKEYUP if use_alt else WM_KEYUP

    def lp(vk: int, up: bool) -> int:
        scan = user32.MapVirtualKeyW(vk, 0)
        v = 1 | (scan << 16)
        if up:
            v |= (1 << 30) | (1 << 31)
        return v & 0xFFFFFFFF

    for _ in range(times):
        for m in mods:
            post(hwnd, down_msg, _vk_for(m), lp(_vk_for(m), False))
            time.sleep(0.01)
        for k in main:
            vk = _vk_for(k)
            post(hwnd, down_msg, vk, lp(vk, False))
            time.sleep(0.02)
            post(hwnd, up_msg, vk, lp(vk, True))
        for m in reversed(mods):
            post(hwnd, up_msg, _vk_for(m), lp(_vk_for(m), True))
        time.sleep(0.02)


def send_command(hwnd: int, cmd_id: int) -> None:
    """WM_COMMAND to a window (menu item / button notification).
    cmd_id == control id for buttons, menu command id for menus."""
    post(hwnd, WM_COMMAND, cmd_id & 0xFFFF, 0)


def get_control_id(hwnd: int) -> int:
    return user32.GetDlgCtrlID(ctypes.c_void_p(hwnd))


# --------------------------------------------------------- input (UIA)

def _uia_wrapper(pid: int, hwnd: int | None = None,
                 auto_id: str | None = None, title: str | None = None,
                 title_re: str | None = None, depth: int | None = None):
    """Locate an element with pywinauto UIA. Works across virtual desktops."""
    from pywinauto import Desktop
    spec = Desktop(backend="uia").window(handle=hwnd) if hwnd else \
        Desktop(backend="uia").window(process=pid)
    kwargs = {}
    if auto_id:
        kwargs["auto_id"] = auto_id
    if title:
        kwargs["title"] = title
    if title_re:
        kwargs["title_re"] = title_re
    if kwargs:
        spec = spec.child_window(**kwargs)
    return spec


def uia_invoke(pid: int, **kw) -> str:
    """Click via UIA pattern (Invoke/Select/Toggle), auto-detected.
    Most reliable for Qt/WPF/WinForms. No real input, no focus needed."""
    w = _uia_wrapper(pid, **kw).wrapper_object()
    for meth in ("invoke", "select", "toggle", "expand"):
        try:
            fn = getattr(w, meth, None)
            if fn and w.element_info and _supports_pattern(w, meth):
                fn()
                return meth
        except Exception:
            continue
    raise SandboxError("no Invoke/Select/Toggle/Expand pattern on element; "
                       "try message-level click at coordinates")


def _supports_pattern(wrapper, meth: str) -> bool:
    try:
        from pywinauto.application import WindowSpecification
        iface = {
            "invoke": "UIA_InvokePatternElement",
            "select": "UIA_SelectionItemPatternElement",
            "toggle": "UIA_TogglePatternElement",
            "expand": "UIA_ExpandCollapsePatternElement",
        }[meth]
        return hasattr(wrapper.element_info.element, iface)
    except Exception:
        return True  # best effort: try the call anyway


def uia_set_value(pid: int, value: str, **kw) -> None:
    """Type text via UIA Value pattern (set_value)."""
    w = _uia_wrapper(pid, **kw).wrapper_object()
    try:
        w.set_value(value)
    except Exception as e:
        raise SandboxError(f"set_value failed ({e}); try msg_type on the "
                           f"child Edit hwnd instead")


def uia_dump_tree(pid: int, hwnd: int | None = None, out_file: str | None = None,
                  depth: int | None = None) -> str:
    """print_control_identifiers() -> text (and optionally to file)."""
    spec = _uia_wrapper(pid, hwnd=hwnd)
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            spec.print_control_identifiers(max_depth=depth)
        except TypeError:  # older pywinauto
            spec.print_control_identifiers()
    text = buf.getvalue()
    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(text)
    return text


# ------------------------------------------------------------ job objects

def restrict_process(pid: int):
    """Attach process to a Job Object with KILL_ON_JOB_CLOSE so it cannot
    outlive the caller (guard against orphan AUT processes). Keep the
    returned handle alive in a fixture; closing it kills the tree.

    Best-effort: some hardened systems / AV hook SetInformationJobObject and
    reject the flag with ERROR_INVALID_PARAMETER -- then we return None and
    rely on session.stop() -> taskkill /T /F instead.
    """
    try:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        PROCESS_SET_QUOTA_AND_TERMINATE = 0x0101

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
                ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        hproc = kernel32.OpenProcess(PROCESS_SET_QUOTA_AND_TERMINATE, False, pid)
        if not job or not hproc:
            return None
        info = JOBOBJECT_BASIC_LIMIT_INFORMATION()
        info.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 2, ctypes.byref(info),
                                                ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            kernel32.CloseHandle(hproc)
            return None  # flag blocked (AV/hardened build) -- stop() covers us
        kernel32.AssignProcessToJobObject(job, hproc)
        kernel32.CloseHandle(hproc)
        return job  # keep reference alive!
    except Exception:
        return None


# ------------------------------------------------------------ session mgmt

def sandbox_root() -> str:
    return os.path.join(os.path.expanduser("~"), ".ui-sandbox")


# ------------------------------------------------------------- logging

def log_dir() -> str:
    """Run-log directory: <sandbox_root>/logs (resolves sandbox_root() on
    every call, so tests that redirect the sandbox home are honored)."""
    return os.path.join(sandbox_root(), "logs")


_LOG_PRUNED = False  # prune at most once per process


def sb_log(event: str, **fields) -> None:
    """Append one line to <sandbox_root>/logs/sb-YYYYMMDD.log. Never raises.

    Daily files: the per-command CLI processes append without locks or
    rotation, and pruning is a plain directory scan. On the first write of
    each process, logs older than 30 days are pruned automatically, so the
    log storage stays bounded without any maintenance."""
    global _LOG_PRUNED
    try:
        if not _LOG_PRUNED:
            _LOG_PRUNED = True
            prune_logs(30)
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        now = time.time()
        line = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
                + f".{int(now * 1000) % 1000:03d} {event}")
        kv = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        if kv:
            line += " " + kv
        with open(os.path.join(d, time.strftime("sb-%Y%m%d.log")),
                  "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never break the sandbox


def prune_logs(days: int = 30) -> list:
    """Delete *.log files in the log dir older than `days` (by mtime).
    Returns the removed file names. Best-effort, safe to call anytime."""
    removed = []
    try:
        cutoff = time.time() - days * 86400
        d = log_dir()
        if os.path.isdir(d):
            for name in os.listdir(d):
                if not name.lower().endswith(".log"):
                    continue
                path = os.path.join(d, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed.append(name)
                except OSError:
                    pass  # locked / vanished between list and remove
    except Exception:
        pass
    return removed


class SandboxSession:
    """One isolated app instance. Library usage (pytest) or via sb.py CLI.

    mode: "ghost" (default) | "offscreen" | "vd"
          ghost     = on-monitor but invisible/click-through/taskbar-less
                      (live repaint -> live screenshots; recommended)
          offscreen = beyond the virtual screen, zero footprint, but the app
                      stops repainting -- screenshots freeze at last frame
          vd        = offscreen + dedicated virtual desktop (pyvda); same
                      frozen-capture caveat as offscreen
    """

    def __init__(self, name: str = "default", mode: str = "ghost"):
        self.name = name
        self.mode = mode
        self.root = os.path.join(sandbox_root(), "sessions", name)
        self.data_dir = os.path.join(self.root, "data")
        self.shot_dir = os.path.join(self.root, "shots")
        self.state_file = os.path.join(self.root, "session.json")
        self.pid: int | None = None
        self.hwnd: int | None = None
        self.title: str = ""
        self.exe: str = ""
        self.proc: subprocess.Popen | None = None
        self._job = None
        self._sweep_thread: threading.Thread | None = None
        self._sweep_stop = threading.Event()
        self._sweep_lock = threading.Lock()
        self._user_fg: int = 0  # last foreground window NOT owned by the AUT
        self.swept_log: list = []  # windows isolated by sweep/watchdog so far

    # ---------------- lifecycle

    def start(self, app: str, args: str = "", title_re: str | None = None,
              timeout: float = 20.0, env_isolate: bool = True,
              cwd: str | None = None, auto_sweep: bool = True) -> int:
        """Launch the AUT isolated. Returns main hwnd.
        app: exe path; args: command line arguments (string, split by shlex);
        title_re: regex to pick the main window (else first visible window).
        auto_sweep=True (default) arms the popup watchdog: every window the
        AUT pops up later (dialogs / message boxes / menus) is isolated
        automatically within ~0.4s -- leaks to the user's screen are the
        #1 way this sandbox disturbs people, so this is on by default."""
        import shlex
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.shot_dir, exist_ok=True)
        # remember where the user was BEFORE launching the AUT, so the focus
        # stolen by the AUT's startup window can be given right back
        self._user_fg = get_foreground_window()

        env = os.environ.copy()
        if env_isolate:
            env["APPDATA"] = os.path.join(self.data_dir, "AppData", "Roaming")
            env["LOCALAPPDATA"] = os.path.join(self.data_dir, "AppData", "Local")
            env["TEMP"] = env["TMP"] = os.path.join(self.data_dir, "Temp")
            for p in (env["APPDATA"], env["LOCALAPPDATA"], env["TEMP"]):
                os.makedirs(p, exist_ok=True)

        cmdline = [app] + shlex.split(args)
        self.exe = app
        self.proc = subprocess.Popen(cmdline, env=env, cwd=cwd,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.pid = self.proc.pid
        self._job = restrict_process(self.pid)  # best-effort orphan guard
        sb_log("start", session=self.name, pid=self.pid,
               exe=os.path.basename(app), mode=self.mode)

        # arm the popup watchdog BEFORE waiting for the main window: its
        # EVENT_OBJECT_SHOW hook ghostifies the startup window within
        # milliseconds of first paint (instead of after one 0.25s poll tick
        # plus app startup time), so the visible flash + focus steal at
        # launch shrink from up to seconds to a blink. start_auto_sweep()
        # is idempotent; the tail call below is a no-op when already armed.
        if auto_sweep:
            self.start_auto_sweep()

        try:
            self.hwnd = self._wait_main_window(title_re, timeout,
                                               allow_ghosted=auto_sweep)
        except BaseException as e:
            # never leak the child when startup fails (e.g. wrong title regex)
            self.stop_auto_sweep()
            sb_log("start_failed", session=self.name, pid=self.pid,
                   err=repr(e)[:160])
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)],
                           capture_output=True)
            raise
        self.title = get_window_text(self.hwnd)
        with self._sweep_lock:  # the early-armed watchdog may be isolating it too
            self.isolate(self.hwnd)  # idempotent; re-checks the focus restore
        sb_log("isolated", session=self.name, hwnd=f"0x{self.hwnd:X}",
               title=self.title[:60])
        self.start_auto_sweep()
        self._save()
        return self.hwnd

    def _wait_main_window(self, title_re, timeout, allow_ghosted=False):
        rx = None
        if title_re:
            import re
            rx = re.compile(title_re)
        deadline = time.time() + timeout
        best = None
        while time.time() < deadline:
            for hwnd, pid, cls, title, visible in enum_windows():
                if pid != self.pid or not visible or not title:
                    continue
                ex = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
                if (ex & WS_EX_TOOLWINDOW) and not (
                        allow_ghosted and is_ghosted(hwnd)):
                    continue
                if rx:
                    if rx.search(title) or rx.search(cls):
                        return hwnd
                    if best is None:
                        best = hwnd
                else:
                    return hwnd
            if self.proc and self.proc.poll() is not None:
                raise SandboxError(f"AUT exited early (code="
                                   f"{self.proc.returncode})")
            time.sleep(0.25)
        if best:
            return best
        raise SandboxError(f"no main window for pid={self.pid} within "
                           f"{timeout}s (try --title regex)")

    def isolate(self, hwnd: int) -> None:
        """Apply the isolation primitives to a window.

        Focus-aware: when the AUT currently owns the foreground (its startup
        window, or a dialog that just popped up and grabbed it), the user's
        last foreground window gets the focus back right after isolation --
        the user keeps typing without ever noticing the sandbox."""
        fg = get_foreground_window()
        fg_was_aut = bool(fg) and get_window_pid(fg) == self.pid
        ensure_restored(hwnd)
        if self.mode == "offscreen":
            move_offscreen(hwnd)
        elif self.mode == "vd":
            move_offscreen(hwnd)
            info = move_to_virtual_desktop(hwnd)
            self._vd_number = info["number"]
            self._vd_created = info["created"]
        else:  # ghost (default)
            ghostify(hwnd)
        if fg_was_aut:
            self._restore_user_focus()
        sb_log("isolate", session=self.name, hwnd=f"0x{hwnd:X}",
               cls=get_class_name(hwnd), title=get_window_text(hwnd)[:60])

    def _restore_user_focus(self) -> bool:
        """Give the foreground back to the user's window (best-effort)."""
        tgt = self._user_fg
        if not tgt or not is_window(tgt) or get_window_pid(tgt) == self.pid:
            return False
        return restore_focus_to(tgt)

    def sweep(self) -> list:
        """Catch dialogs/message-boxes the AUT opened visibly and isolate them
        too. Call after actions that can pop dialogs.

        Also a focus safety net: when the AUT owns the foreground at sweep
        time (its own code called SetForegroundWindow -- WS_EX_NOACTIVATE
        does not block explicit calls), give it back to the user."""
        fg = get_foreground_window()
        if fg and get_window_pid(fg) != self.pid:
            self._user_fg = fg  # track where the user currently is
        elif fg:
            self._restore_user_focus()  # focus stolen again -> hand it back
        fixed = []
        for hwnd, pid, cls, title, visible in enum_windows():
            if pid != self.pid or hwnd == self.hwnd:
                continue
            if visible and visible_to_user(hwnd):
                self.isolate(hwnd)
                fixed.append((hwnd, cls, title))
        if fixed:
            sb_log("sweep", session=self.name,
                   hwnds=",".join(f"0x{h:X}({c})" for h, c, _t in fixed))
        self.swept_log.extend(fixed)
        return fixed

    def start_auto_sweep(self, interval: float = 0.4) -> threading.Thread:
        """Popup watchdog (auto-sweep): isolates every top-level window the
        AUT pops up (dialogs / MessageBoxes / popup menus) -- equivalent to
        calling sweep() forever, but event-driven:

        - a SetWinEventHook(EVENT_OBJECT_SHOW) fires the moment a window of
          the AUT becomes visible -> it is isolated within milliseconds
          (vs. one polling interval before), so the focus-stealing / visible
          flash window is reduced to a blink;
        - the periodic sweep() remains as a safety net (hook misses, IME
          filtering races, windows shown before the hook was installed).

        s.start() arms this by default. When you launch the AUT yourself
        (own Popen + manual s.isolate(hwnd), like SmartStore's e2e harness)
        you MUST call this once, or every later popup shows up on the
        user's screen with normal styling and focus stealing.
        Returns the daemon thread; stop with stop_auto_sweep()."""
        if self._sweep_thread and self._sweep_thread.is_alive():
            return self._sweep_thread
        self._sweep_stop = threading.Event()

        session = self

        @ctypes.WINFUNCTYPE(None, wt.HWND, wt.UINT, wt.HWND,
                            wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)
        def _on_show(_hook, event, hwnd, id_object, _id_child,
                     _thread, _time_ms):
            try:  # never raise inside a win-event callback
                if not hwnd or id_object != 0:  # OBJID_WINDOW only
                    return
                hwnd = int(hwnd) if not isinstance(hwnd, int) else hwnd
                if get_window_pid(hwnd) != session.pid:
                    return
                if event == EVENT_SYSTEM_FOREGROUND:
                    # an AUT window just became the foreground (e.g. the AUT's
                    # own SetForegroundWindow call, which WS_EX_NOACTIVATE
                    # cannot block) -> hand the focus back to the user NOW.
                    # Our own restore fires this event for the user window,
                    # which is filtered out by the pid check above (no loop).
                    sb_log("fg_restore", session=session.name,
                           hwnd=f"0x{hwnd:X}")
                    session._restore_user_focus()
                    return
                if hwnd == session.hwnd:
                    return
                if user32.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT) != hwnd:
                    return  # child control show events: irrelevant
                if _is_ime_class(get_class_name(hwnd)):
                    return
                if not is_window_visible(hwnd) or is_ghosted(hwnd):
                    return
                with session._sweep_lock:
                    session.isolate(hwnd)  # ghost + NOACTIVATE + focus back
                    session.swept_log.append(
                        (hwnd, get_class_name(hwnd), get_window_text(hwnd)))
            except Exception:
                pass  # periodic sweep below still catches it

        def _watch():
            hook = None
            try:
                hook = user32.SetWinEventHook(
                    EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_SHOW, None,
                    _on_show, 0, 0, WINEVENT_OUTOFCONTEXT)
            except Exception:
                hook = None  # hook unavailable -> polling still works
            try:
                msg = wt.MSG()
                while not session._sweep_stop.is_set():
                    # sleep `interval` OR until a win-event message arrives --
                    # MsgWaitForMultipleObjects wakes on input, so foreground
                    # steals are corrected in milliseconds, not per-interval
                    user32.MsgWaitForMultipleObjects(
                        0, None, False, int(interval * 1000), 0x04FF)  # QS_ALLINPUT
                    # dispatch pending win-events (callbacks fire in PeekMessage)
                    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0,
                                              PM_REMOVE):
                        if msg.message == WM_QUIT:
                            return
                    try:
                        with session._sweep_lock:
                            session.sweep()
                    except Exception:
                        pass  # transient win32 error: never kill the watchdog
            finally:
                if hook:
                    user32.UnhookWinEvent(hook)

        th = threading.Thread(target=_watch,
                              name=f"ui-sb-sweep-{self.name}", daemon=True)
        self._sweep_thread = th
        th.start()
        sb_log("watchdog_on", session=self.name, interval=interval)
        return th

    def stop_auto_sweep(self) -> None:
        """Stop the popup watchdog started by start_auto_sweep()."""
        self._sweep_stop.set()
        th = self._sweep_thread
        if th and th is not threading.current_thread():
            th.join(timeout=2.0)
        self._sweep_thread = None
        if th:
            sb_log("watchdog_off", session=self.name)

    def stop(self, kill: bool = False) -> None:
        """Graceful close (WM_CLOSE) then force-kill the tree if needed."""
        sb_log("stop", session=self.name, pid=self.pid, kill=kill)
        self.stop_auto_sweep()
        if self.proc and self.proc.poll() is None:
            if not kill and self.hwnd and is_window(self.hwnd):
                post(self.hwnd, WM_CLOSE)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill = True
            if kill and self.proc.poll() is None:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)],
                               capture_output=True)
        elif self.pid:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)],
                           capture_output=True, timeout=10)
        self._cleanup_vd()
        try:
            os.remove(self.state_file)
        except OSError:
            pass

    def _cleanup_vd(self):
        """Remove the sandbox-created virtual desktop (if we created one).
        Windows migrates any remaining windows to another desktop."""
        if not getattr(self, "_vd_created", False):
            return
        try:
            import pyvda
            vd = pyvda.VirtualDesktop(number=self._vd_number)
            if pyvda.VirtualDesktop.current().number == self._vd_number:
                pyvda.VirtualDesktop(number=1).go()
            vd.remove()
            self._vd_created = False
        except Exception:
            pass  # best effort; stray desktop can be removed manually

    # ---------------- state

    def _save(self):
        st = {"name": self.name, "pid": self.pid, "hwnd": self.hwnd,
              "title": self.title, "mode": self.mode, "exe": self.exe,
              "started": time.strftime("%Y-%m-%d %H:%M:%S"),
              "root": self.root,
              "user_fg": self._user_fg,  # focus-restore target: survives the
                                         # per-command process model of sb.py
              "vd_number": getattr(self, "_vd_number", None),
              "vd_created": getattr(self, "_vd_created", False)}
        os.makedirs(self.root, exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, name: str) -> "SandboxSession":
        sf = os.path.join(sandbox_root(), "sessions", name, "session.json")
        if not os.path.exists(sf):
            raise SandboxError(f"no session named {name!r} (see: sb status)")
        with open(sf, encoding="utf-8") as f:
            st = json.load(f)
        s = cls(name=st["name"], mode=st.get("mode", "ghost"))
        s.pid, s.hwnd = st["pid"], st["hwnd"]
        s.title, s.exe = st.get("title", ""), st.get("exe", "")
        s._user_fg = st.get("user_fg", 0) or 0
        s._vd_number = st.get("vd_number")
        s._vd_created = st.get("vd_created", False)
        if not is_window(s.hwnd):
            # main hwnd died; try to re-resolve from pid
            for hwnd, pid, cls, title, visible in enum_windows():
                if pid == s.pid and visible:
                    s.hwnd = hwnd
                    break
            else:
                raise SandboxError(f"session {name!r}: window gone "
                                   f"(pid={s.pid} alive?)")
        return s

    # ---------------- conveniences

    def windows(self) -> list:
        """Top-level windows of the AUT: (hwnd, class, title, user_visible).
        user_visible=True means it could disturb the user (not isolated)."""
        return [(h, c, t, visible_to_user(h)) for h, p, c, t, v in enum_windows()
                if p == self.pid and v]

    def find_child(self, parent: int | None = None, cls: str | None = None,
                   text_re: str | None = None, index: int = 0) -> int:
        import re
        rx = re.compile(text_re) if text_re else None
        parent = parent or self.hwnd
        hits = [h for (h, c, t) in enum_child_windows(parent)
                if (cls is None or c.lower() == cls.lower())
                and (rx is None or rx.search(t))]
        if not hits:
            raise SandboxError(f"no child matching cls={cls!r} text={text_re!r}")
        return hits[index]

    def screenshot(self, out: str | None = None, hwnd: int | None = None,
                   client_only: bool = False) -> str:
        hwnd = hwnd or self.hwnd
        out = out or os.path.join(self.shot_dir,
                                  time.strftime("%H%M%S_") + f"0x{hwnd:X}.png")
        return capture_window(hwnd, out, client_only=client_only)

    def text_of(self, hwnd: int | None = None) -> str:
        return wm_gettext(hwnd or self.hwnd)


def pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    kernel32.CloseHandle(h)
    return True


def list_sessions() -> list:
    root = os.path.join(sandbox_root(), "sessions")
    out = []
    if not os.path.isdir(root):
        return out
    for name in os.listdir(root):
        sf = os.path.join(root, name, "session.json")
        if os.path.exists(sf):
            try:
                with open(sf, encoding="utf-8") as f:
                    out.append(json.load(f))
            except Exception:
                pass
    return out


def prune_sessions() -> list:
    """Delete state files of sessions whose process is dead. Returns names."""
    removed = []
    for st in list_sessions():
        if not pid_alive(st["pid"]):
            try:
                os.remove(os.path.join(sandbox_root(), "sessions",
                                       st["name"], "session.json"))
                removed.append(st["name"])
            except OSError:
                pass
    return removed
