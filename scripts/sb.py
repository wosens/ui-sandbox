# -*- coding: utf-8 -*-
# Copyright (c) 2026 梅文海. All rights reserved. Created: 2026-08-28.
"""sb.py -- ui-sandbox CLI.

Examples:
    python sb.py doctor
    python sb.py start demo -- "C:\\Windows\\notepad.exe" --title "记事本|Notepad"
    python sb.py status
    python sb.py windows demo
    python sb.py find demo --class Edit
    python sb.py click demo --hwnd 0x1A0B2E --x 60 --y 25
    python sb.py click demo --auto-id btnLogin          (UIA invoke)
    python sb.py type demo --class Edit --text "hello" --replace
    python sb.py keys demo --hwnd 0x1A0B2E --keys "ctrl+s"
    python sb.py cmd demo --hwnd 0x1A0B2E --id 2
    python sb.py text demo --class Edit
    python sb.py shot demo                              (default shots dir)
    python sb.py tree demo --depth 2
    python sb.py sweep demo                               # 一次性收拢
    python sb.py sweep demo --watch --duration 600        # 驻留：持续收拢新弹窗
    python sb.py stop demo [--kill]
    python sb.py logs [--prune-days N] [--tail N]         # 运行日志：查看/清理（每天自动清 30 天前）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox as sb  # noqa: E402


def _fmt_hwnd(h: int) -> str:
    return f"0x{h:X}(={h})"


def cmd_doctor(a):
    print("== ui-sandbox doctor ==")
    print(f"python      : {sys.version.split()[0]}")
    print(f"OS          : {sys.platform}")
    ok = lambda b: "OK " if b else "MISSING"  # noqa: E731
    for mod, label, need in (("win32api", "pywin32", True),
                             ("PIL", "Pillow", True),
                             ("pywinauto", "pywinauto", False),
                             ("pyvda", "pyvda(vd mode)", False)):
        try:
            __import__(mod)
            print(f"{label:<14}: {ok(True)}")
        except ImportError:
            print(f"{label:<14}: {ok(False)}" + ("" if not need else "  <-- REQUIRED"))
    vs = sb.virtual_screen_rect()
    print(f"virtual screen: {vs}")
    try:
        import pyvda
        print(f"virtual desktops: {len(pyvda.get_virtual_desktops())}")
    except Exception:
        pass
    print(f"sandbox root: {sb.sandbox_root()}")


def cmd_start(a):
    sPark = None
    if getattr(a, "park", None):
        parts = a.park.replace(" ", "").split(",")
        if len(parts) != 2:
            print("ERROR: --park expects 'x,y' (e.g. --park 500,-3000)",
                  file=sys.stderr)
            sys.exit(2)
        try:
            sPark = (int(parts[0]), int(parts[1]))
        except ValueError:
            print("ERROR: --park expects integer 'x,y'", file=sys.stderr)
            sys.exit(2)
    s = sb.SandboxSession(name=a.name, mode=a.mode, a_Park=sPark)
    hwnd = s.start(a.app[0], args=" ".join(a.app[1:]) if len(a.app) > 1 else "",
                   title_re=a.title, timeout=a.timeout,
                   env_isolate=not a.no_env_isolate, cwd=a.cwd)
    rect = sb.get_window_rect(hwnd)
    print(f"started session={a.name} pid={s.pid} hwnd={_fmt_hwnd(hwnd)}")
    print(f"  title : {s.title}")
    print(f"  rect  : {rect}")
    print(f"  mode  : {s.mode}  isolated={not sb.visible_to_user(hwnd)}")
    print(f"  park  : {tuple(s._park) if s._park else '(default: virtual screen top-left + 8px)'}")
    print(f"  dir   : {s.root}")
    print("  note  : 弹窗看门狗随本命令退出即停止; CLI 流请挂 'sweep <name> --watch'"
          " 驻留收拢, 或改用库流(s.start 默认全程看门狗)")


def _sess(a):
    return sb.SandboxSession.load(a.name)


def cmd_status(a):
    pruned = sb.prune_sessions()
    if pruned:
        print(f"pruned dead sessions: {', '.join(pruned)}")
    rows = sb.list_sessions()
    if not rows:
        print("no active sessions")
        return
    for st in rows:
        try:
            alive = sb.pid_alive(st["pid"])
        except Exception:
            alive = False
        print(f"- {st['name']:<12} pid={st['pid']:<8} hwnd={_fmt_hwnd(st.get('hwnd', 0))} "
              f"alive={alive} mode={st.get('mode')} exe={os.path.basename(st.get('exe', ''))}")


def cmd_windows(a):
    s = _sess(a)
    for h, c, t, userv in s.windows():
        print(f"hwnd={_fmt_hwnd(h):<16} class={c:<24} user_visible={userv} title={t!r}")
        if a.children:
            for ch, cc, tt in sb.enum_child_windows(h):
                print(f"  child hwnd={_fmt_hwnd(ch):<14} class={cc:<20} "
                      f"id={sb.get_control_id(ch):<6} text={tt!r}")


def cmd_find(a):
    s = _sess(a)
    parent = int(a.parent, 0) if a.parent else None
    try:
        h = s.find_child(parent=parent, cls=a.cls, text_re=a.text, index=a.index)
    except sb.SandboxError as e:
        print(f"NOT FOUND: {e}")
        sys.exit(2)
    print(f"hwnd={_fmt_hwnd(h)} class={sb.get_class_name(h)} "
          f"text={sb.get_window_text(h)!r} ctrl_id={sb.get_control_id(h)}")


def _resolve_target(s, a):
    """--hwnd | --auto-id/--title(UIA leaf) | --class/--text(child hwnd) | main."""
    if a.hwnd:
        return int(a.hwnd, 0)
    if a.auto_id or a.uia_title:
        return ("uia", {"auto_id": a.auto_id} if a.auto_id else
                {"title_re": a.uia_title})
    if a.cls or a.text:
        return s.find_child(cls=a.cls, text_re=a.text, index=a.index)
    return s.hwnd


def cmd_click(a):
    s = _sess(a)
    target = _resolve_target(s, a)
    if isinstance(target, tuple):  # UIA
        meth = sb.uia_invoke(s.pid, **target[1])
        print(f"uia {meth}() done")
    else:
        if a.button and not (a.x or a.y):
            sb.bm_click(target)
            print(f"BM_CLICK -> hwnd={_fmt_hwnd(target)}")
        else:
            sb.msg_click(target, a.x or 0, a.y or 0, button=a.button_ or "left",
                         double=a.double)
            print(f"msg click ({a.x},{a.y}) -> hwnd={_fmt_hwnd(target)}")
    if a.sweep:
        fixed = s.sweep()
        if fixed:
            print(f"swept {len(fixed)} new window(s) offscreen: {fixed}")


def cmd_type(a):
    s = _sess(a)
    target = _resolve_target(s, a)
    if isinstance(target, tuple):
        sb.uia_set_value(s.pid, a.value, **target[1])
        print("uia set_value() done")
    else:
        sb.msg_type(target, a.value, replace=a.replace)
        print(f"typed {len(a.value)} chars -> hwnd={_fmt_hwnd(target)}")


def cmd_keys(a):
    s = _sess(a)
    hwnd = int(a.hwnd, 0) if a.hwnd else (s.find_child(cls=a.cls, text_re=a.text)
                                          if (a.cls or a.text) else s.hwnd)
    sb.msg_keys(hwnd, a.keys, times=a.times)
    print(f"keys {a.keys!r} x{a.times} -> hwnd={_fmt_hwnd(hwnd)}")
    if a.sweep:
        fixed = s.sweep()
        if fixed:
            print(f"swept: {fixed}")


def cmd_cmd(a):
    s = _sess(a)
    hwnd = int(a.hwnd, 0) if a.hwnd else s.hwnd
    sb.send_command(hwnd, a.id)
    print(f"WM_COMMAND id={a.id} -> hwnd={_fmt_hwnd(hwnd)}")
    if a.sweep:
        fixed = s.sweep()
        if fixed:
            print(f"swept: {fixed}")


def cmd_text(a):
    s = _sess(a)
    hwnd = int(a.hwnd, 0) if a.hwnd else (s.find_child(cls=a.cls, text_re=a.text)
                                          if (a.cls or a.text) else s.hwnd)
    print(sb.wm_gettext(hwnd))


def cmd_shot(a):
    s = _sess(a)
    hwnd = int(a.hwnd, 0) if a.hwnd else s.hwnd
    path = s.screenshot(out=a.out, hwnd=hwnd, client_only=a.client)
    print(f"saved: {path}")


def cmd_tree(a):
    s = _sess(a)
    hwnd = int(a.hwnd, 0) if a.hwnd else s.hwnd
    out = os.path.join(s.root, "tree.txt") if a.out is None else a.out
    txt = sb.uia_dump_tree(s.pid, hwnd=hwnd, out_file=out, depth=a.depth)
    lines = txt.splitlines()
    head = "\n".join(lines[: a.head])
    print(head if head else "(empty tree)")
    print(f"... full tree ({len(lines)} lines) -> {out}")


def cmd_sweep(a):
    s = _sess(a)
    if not a.watch:
        fixed = s.sweep()
        print(f"isolated {len(fixed)} window(s): {fixed}" if fixed
              else "nothing to sweep")
        return
    # 驻留模式：复用库层看门狗（WinEvent 即时隔离 + interval 轮询兜底，
    # 与 s.start() 自带的 auto-sweep 同一实现），本进程只负责驻留与报告。
    print(f"watching session={a.name} for {a.duration:g}s "
          f"(win-event + poll {a.interval:g}s), Ctrl+C to stop...", flush=True)
    th = s.start_auto_sweep(a.interval)
    hits = 0
    deadline = time.time() + a.duration
    try:
        while time.time() < deadline:
            time.sleep(min(a.interval, 0.5))
            if not sb.pid_alive(s.pid):
                print("AUT exited")
                break
            while hits < len(s.swept_log):
                _h, _c, title = s.swept_log[hits]
                hits += 1
                print(f"[{time.strftime('%H:%M:%S')}] swept: {title!r}",
                      flush=True)
            leaks = [w for w in s.windows() if w[3]]
            if leaks:
                print(f"[{time.strftime('%H:%M:%S')}] WARNING: {len(leaks)} "
                      f"window(s) still visible to user", flush=True)
            if not th.is_alive():
                print("watchdog thread died; watch done")
                break
    finally:
        s.stop_auto_sweep()
    print(f"watch done: {hits} window(s) isolated")


def cmd_stop(a):
    s = _sess(a)
    s.stop(kill=a.kill)
    print(f"stopped session={a.name}")


def cmd_logs(a):
    """List run logs; optionally prune old files and/or tail the newest."""
    if a.prune_days is not None:
        removed = sb.prune_logs(a.prune_days)
        print(f"pruned {len(removed)} log file(s) older than "
              f"{a.prune_days}d" + (f": {', '.join(removed)}" if removed else ""))
    d = sb.log_dir()
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(".log")) \
        if os.path.isdir(d) else []
    if not files:
        print(f"no log files in {d}")
        return
    now = time.time()
    for name in files:
        p = os.path.join(d, name)
        print(f"- {name}  {os.path.getsize(p)} bytes  "
              f"age={(now - os.path.getmtime(p)) / 86400:.1f}d")
    if a.tail:
        p = os.path.join(d, files[-1])
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        print(f"-- tail {min(a.tail, len(lines))}/{len(lines)} of "
              f"{os.path.basename(p)} --")
        for ln in lines[-a.tail:]:
            print(ln.rstrip())


def main():
    p = argparse.ArgumentParser(prog="sb", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    # `start` is parsed manually: everything after `--` belongs to the app
    # command line, everything before belongs to sb.py options.
    if len(sys.argv) > 1 and sys.argv[1] == "start":
        raw = sys.argv[2:]
        app_args = []
        if "--" in raw:
            i = raw.index("--")
            app_args, raw = raw[i + 1:], raw[:i]
        sp = argparse.ArgumentParser(
            prog="sb start",
            epilog="example: sb start demo --mode vd --title Notepad -- "
                   "C:\\Windows\\notepad.exe file.txt")
        sp.add_argument("name")
        sp.add_argument("--title", help="regex matching the main window title/class")
        sp.add_argument("--mode", default="ghost",
                        choices=["ghost", "offscreen", "vd"])
        sp.add_argument("--timeout", type=float, default=20)
        sp.add_argument("--cwd")
        sp.add_argument("--park",
                        help="ghost 停靠点 'x,y'（屏幕坐标），如 --park 500,-3000 "
                             "停到主屏正上方离屏处；默认虚拟屏左上+8px。"
                             "实测 notepad/Qt 离屏仍可实时截图；"
                             "Electron/独占GPU 程序可能冻结，用默认值")
        sp.add_argument("--no-env-isolate", action="store_true",
                        help="keep real APPDATA/TEMP (some apps need real env)")
        try:
            a = sp.parse_args(raw)
        except SystemExit:
            sys.exit(2)
        if not app_args:
            print("ERROR: missing app command after `--`  "
                  "(example: sb start demo -- C:\\Windows\\notepad.exe)",
                  file=sys.stderr)
            sys.exit(2)
        a.app = app_args
        try:
            cmd_start(a)
        except sb.SandboxError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        return

    sp = sub.add_parser("status").set_defaults(fn=cmd_status)

    sp = sub.add_parser("windows", help="list AUT top-level (+child) windows")
    sp.add_argument("name")
    sp.add_argument("--children", action="store_true")
    sp.set_defaults(fn=cmd_windows)

    def target_args(sp):
        sp.add_argument("--hwnd")
        sp.add_argument("--auto-id", help="UIA AutomationId (uia mode)")
        sp.add_argument("--uia-title", help="UIA title regex (uia mode)")
        sp.add_argument("--class", dest="cls", help="child window class")
        sp.add_argument("--text", dest="text", help="child window text regex")
        sp.add_argument("--index", type=int, default=0)

    sp = sub.add_parser("find", help="find a child control hwnd")
    sp.add_argument("name")
    sp.add_argument("--class", dest="cls")
    sp.add_argument("--text", help="regex on control text")
    sp.add_argument("--parent")
    sp.add_argument("--index", type=int, default=0)
    sp.set_defaults(fn=cmd_find)

    sp = sub.add_parser("click")
    sp.add_argument("name")
    target_args(sp)
    sp.add_argument("-x", "--x", type=int)
    sp.add_argument("-y", "--y", type=int)
    sp.add_argument("--button", help="BM_CLICK a standard Button control")
    sp.add_argument("--mouse", dest="button_", choices=["left", "right", "middle"],
                    default="left")
    sp.add_argument("--double", action="store_true")
    sp.add_argument("--sweep", action="store_true", help="isolate popped dialogs")
    sp.set_defaults(fn=cmd_click)

    sp = sub.add_parser("type")
    sp.add_argument("name")
    target_args(sp)
    sp.add_argument("--value", "-t", required=True, help="text to type")
    sp.add_argument("--replace", action="store_true",
                    help="WM_SETTEXT instead of WM_CHAR stream")
    sp.set_defaults(fn=cmd_type)

    sp = sub.add_parser("keys")
    sp.add_argument("name")
    target_args(sp)
    sp.add_argument("--keys", required=True, help="e.g. 'ctrl+s', 'enter'")
    sp.add_argument("--times", type=int, default=1)
    sp.add_argument("--sweep", action="store_true")
    sp.set_defaults(fn=cmd_keys)

    sp = sub.add_parser("cmd", help="send WM_COMMAND (menu/button id)")
    sp.add_argument("name")
    sp.add_argument("--hwnd")
    sp.add_argument("--id", type=lambda v: int(v, 0), required=True)
    sp.add_argument("--sweep", action="store_true",
                    help="isolate popped dialogs right after the command")
    sp.set_defaults(fn=cmd_cmd)

    sp = sub.add_parser("text", help="read control text (WM_GETTEXT)")
    sp.add_argument("name")
    sp.add_argument("--hwnd")
    sp.add_argument("--class", dest="cls")
    sp.add_argument("--text")
    sp.set_defaults(fn=cmd_text)

    sp = sub.add_parser("shot", help="PrintWindow screenshot")
    sp.add_argument("name")
    sp.add_argument("--hwnd")
    sp.add_argument("-o", "--out")
    sp.add_argument("--client", action="store_true")
    sp.set_defaults(fn=cmd_shot)

    sp = sub.add_parser("tree", help="dump UIA control tree")
    sp.add_argument("name")
    sp.add_argument("--hwnd")
    sp.add_argument("--depth", type=int)
    sp.add_argument("--head", type=int, default=60)
    sp.add_argument("-o", "--out")
    sp.set_defaults(fn=cmd_tree)

    sp = sub.add_parser("sweep", help="isolate dialogs that popped on screen")
    sp.add_argument("name")
    sp.add_argument("--watch", action="store_true",
                    help="keep this process alive and isolate popups "
                         "continuously (CLI-side auto-sweep)")
    sp.add_argument("--duration", type=float, default=3600.0,
                    help="watch duration in seconds (with --watch)")
    sp.add_argument("--interval", type=float, default=0.4,
                    help="poll interval in seconds (with --watch)")
    sp.set_defaults(fn=cmd_sweep)

    sp = sub.add_parser("stop")
    sp.add_argument("name")
    sp.add_argument("--kill", action="store_true")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("logs", help="list run logs; prune old / tail newest")
    sp.add_argument("--prune-days", type=int, default=None,
                    help="delete log files older than N days "
                         "(auto-pruned at 30d on every run anyway)")
    sp.add_argument("--tail", type=int, default=0,
                    help="also print the last N lines of the newest log")
    sp.set_defaults(fn=cmd_logs)

    a = p.parse_args()
    sb.sb_log("cli", op=getattr(a.fn, "__name__", "?").replace("cmd_", ""),
              session=getattr(a, "name", None))
    try:
        a.fn(a)
    except sb.SandboxError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
