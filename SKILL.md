---
name: ui-sandbox
description: Windows 隐藏 UI 测试沙箱 —— 在不影响真实鼠标/键盘、不打扰用户工作的前提下运行 GUI 程序并驱动它。核心技术：Ghost 模式（窗口留在显示器上持续重绘，但 alpha=1 视觉不可见 + 点击穿透 + 无任务栏/Alt-Tab 图标）+ 消息级输入模拟（WM_LBUTTONDOWN/BM_CLICK/WM_CHAR/WM_COMMAND/UIA Invoke，绝不移动真实光标）+ PrintWindow 离屏截屏。提供会话管理 CLI（sb.py start/windows/find/click/type/keys/cmd/text/shot/tree/sweep/stop）和 Python 库（pytest 集成）。触发词 — "测试沙箱/沙盒"、"UI 沙箱"、"不影响鼠标"、"后台跑 UI 测试"、"隐藏运行 GUI"、"消息级点击"、"隔离运行界面程序"、"ui sandbox"、"hidden ui automation"、"不抢鼠标键盘的自动化"。适用于调试时需要反复操作 GUI 程序（点击/输入/截图）但用户正在用电脑的场景。不适用于：纯 Web 自动化（用 webapp-testing）、无需 GUI 的测试、游戏/DirectX 独占全屏程序。
---

# ui-sandbox — Windows 隐藏 UI 测试沙箱

在用户正常使用电脑的同时，**隐藏运行**一个 GUI 程序，**用消息模拟**鼠标/键盘驱动它，**截图观察**结果——三者互不打扰。

## 解决什么问题

普通 UI 自动化（pyautogui / pywinauto `click_input()`）会**物理移动鼠标、抢焦点、弹窗口**，测试期间用户完全无法工作。本技能把被测程序（AUT）装进"沙箱"：

| 用户需求 | 实现方式 |
|---------|---------|
| 不影响真实鼠标/键盘 | 全部输入走 **消息级**（PostMessage/SendMessage/UIA 模式），物理光标零移动 |
| 隐藏运行 | **Ghost 模式**（默认）：窗口 alpha=1/255 视觉不可见 + `WS_EX_TRANSPARENT` 点击穿透 + 工具栏/Alt-Tab 无图标 |
| 基础功能：截屏/点击/键盘 | PrintWindow 截屏（隐藏窗口也能截到实时画面）；`click/type/keys/cmd` 全套消息级输入 |

## 三种隔离模式

| 模式 | 原理 | 截图 | 适用 |
|------|------|------|------|
| **ghost**（默认） | 窗口放在真实显示器角落、z 序垫底，但 alpha=1 不可见 + 点击穿透 | **实时**（窗口持续重绘，DWM 表面保持最新） | 需要截图反馈的绝大多数场景 |
| offscreen | 窗口移到所有显示器之外 | 冻结帧（离屏窗口不再重绘） | 只需要输入隔离、不看画面 |
| vd | offscreen + 专属虚拟桌面（pyvda，自动创建/回收） | 冻结帧 | 需要把 AUT 从当前桌面彻底移走时 |

> **关键发现（实测）**：离屏/其他虚拟桌面上的窗口**不再收到 WM_PAINT**，截图永远停留在最后一次可见时的画面。Ghost 模式让窗口留在显示器上参与绘制合成，才能截到实时内容——这是"隐藏"与"可截图"兼得的唯一路径。

## 文件结构

```
ui-sandbox/
├── SKILL.md
├── requirements.txt
├── scripts/
│   ├── sandbox.py   # 核心库（隔离原语 + 消息输入 + PrintWindow 截屏 + 会话管理）
│   └── sb.py        # CLI（会话状态存 ~/.ui-sandbox/sessions/<name>/session.json）
└── examples/
    ├── target_demo.py          # tkinter 演示程序（测试靶子）
    ├── target_dbg.py           # 带事件日志的调试靶子
    └── pytest_sandbox_example.py  # pytest 集成示例（隔离/截图/输入/自动 sweep 4 用例实测全过）
```

## 快速开始（CLI）

```bash
cd <skill>/scripts
python sb.py doctor                                    # 环境自检

# 1. 启动被测程序（-- 之后是程序命令行；选项必须放在 -- 之前）
python sb.py start demo --title "记事本|Notepad" -- C:\Windows\notepad.exe
python sb.py start qt   --title "MyApp" --mode ghost  -- D:\build\MyApp.exe

# 2. 观察
python sb.py windows demo --children     # 顶层窗口 + 子控件（hwnd/类名/ID/文本）
python sb.py tree demo --depth 2         # UIA 控件树（Qt/WPF 定位 auto_id 用）
python sb.py shot demo                   # PrintWindow 截图 → ~/.ui-sandbox/.../shots/

# 3. 驱动（全部消息级，不动物理鼠标）
python sb.py find   demo --class Edit                        # 找控件 hwnd
python sb.py click  demo --hwnd 0x5A51F58 --x 60 --y 25      # 坐标点击
python sb.py click  demo --hwnd 0x5A51F58 --button           # 标准 Button 控件（BM_CLICK）
python sb.py click  demo --auto-id btnLogin                  # UIA Invoke/Select（Qt/WPF 推荐）
python sb.py type   demo --class Edit --value "hello" --replace   # WM_SETTEXT
python sb.py type   demo --class Edit --value "abc"          # WM_CHAR 逐字符（插入到光标处）
python sb.py keys   demo --class Edit --keys "ctrl+end"      # 键组合
python sb.py cmd    demo --hwnd 0x... --id 0x1C              # WM_COMMAND（菜单/按钮 ID 直发）
python sb.py text   demo --class Edit                        # 读回控件文本（WM_GETTEXT）
python sb.py sweep  demo                                     # 把 AUT 已弹出的对话框一并隔离
python sb.py sweep  demo --watch --duration 600              # 驻留：期间所有新弹窗持续自动收拢

# 4. 结束（优雅关闭，超时强杀进程树；vd 模式自动回收虚拟桌面）
python sb.py stop demo
python sb.py status          # 会话列表（自动清理死会话）
```

**对话框泄漏（易踩坑，实测事故：AUT 弹出的选择框/修复框/右键菜单全程正常可见还抢焦点）**：`isolate(hwnd)` 只处理传入的主窗口；AUT 运行中弹出的对话框、MessageBox、右键菜单是**新的顶层窗口**，不做处理就以正常样式出现在用户屏幕上。三层兜底，按优先级：

1. **自动（默认）**：`s.start()` 已内置弹窗看门狗（`auto_sweep=True`）——WinEvent 钩子（`EVENT_OBJECT_SHOW`）在弹窗**可见的瞬间**（毫秒级）将其隔离，0.4s 轮询只作兜底；纯 CLI 流用 `sb.py sweep demo --watch --duration N` 驻留收拢（sb.py 每个子命令都是独立进程，`start` 里的看门狗活不过命令本身，驻留必须靠 `--watch`）。
2. **半自动**：`click/keys/cmd` 带 `--sweep`，动作后立即收拢一次。
3. **手动**：自管进程（自己 `Popen` 启动 AUT + 手动 `s.isolate(hwnd)`，如 SmartStore e2e）**必须**显式 `s.start_auto_sweep()` 一次；等待弹窗的轮询循环里也可每轮调 `s.sweep()`。

**焦点保护（实测事故：测试循环期间用户前台被不停抢走、键盘打进隐身窗口）**：隔离只做隐身是不够的——弹窗/启动窗口抢到前台后即使 ghost 化，前台仍停在隐身窗口上，用户所有键盘输入全被吞。现在三层防线全部内置（`isolate` / 看门狗自动生效，无需调用方做任何事）：

1. **不可激活**：隔离即加 `WS_EX_NOACTIVATE`，被隔离窗口从此无法被系统/用户交互激活成前台。
2. **立即归还**：`isolate()` 发现被隔离的窗口正占着前台（启动窗口、刚弹出的对话框）时，隔离后立即把前台还给用户之前的窗口（`AttachThreadInput` + `SetForegroundWindow`，跨进程可用）。
3. **毫秒级纠正**：看门狗挂 `EVENT_SYSTEM_FOREGROUND` 钩子——AUT 代码自己调 `SetForegroundWindow` 抢焦点（`WS_EX_NOACTIVATE` 挡不住显式调用）时，毫秒级自动归还；`sweep()` 每 tick 亦作兜底。归还目标动态跟随用户当前前台（用户切到别的窗口后，归还/跟踪目标随之更新，绝不把用户硬拉回旧窗口）。

## Python 库 / pytest 用法

```python
import sandbox as sb

s = sb.SandboxSession(name="mytest", mode="ghost")
hwnd = s.start(r"D:\build\MyApp.exe", title_re="MyApp")   # 默认已开启弹窗自动收拢
edit = s.find_child(cls="Edit")
sb.wm_settext(edit, "hello")          # 或 s.find_child + msg_type/msg_keys/msg_click
s.screenshot()                        # → session shots 目录
s.sweep()                             # 手动收拢一次（等待弹窗的轮询循环里用）
# 若自己 Popen 启动 AUT 再手动 s.isolate(hwnd)：必须再调 s.start_auto_sweep()
s.stop(kill=True)
```

完整示例见 `examples/pytest_sandbox_example.py`（notepad 全链路：隔离断言、截图内容断言、输入回读断言、弹窗自动收拢断言）。

## 输入方式选型（按可靠性排序）

1. **UIA 模式**（`--auto-id` / `uia_invoke` / `uia_set_value`）— Qt/WPF/WinForms 首选，控件自报告状态，无坐标依赖
2. **WM_COMMAND / BM_CLICK**（`cmd` / `click --button`）— Win32/MFC 标准 Button/菜单，最稳
3. **WM_SETTEXT**（`type --replace`）— Edit 类控件整段替换
4. **WM_CHAR 流**（`type`）— 逐字符，插入光标处，多数控件适用
5. **坐标 WM_LBUTTONDOWN/UP**（`click -x -y`）— 最后手段，自绘控件用；坐标是**客户区坐标**（用 `tree`/`windows --children` 查矩形换算）

## 与 windows-desktop-e2e 技能的关系

本技能 = 该技能体系缺失的 **"Tier 2.5"**：Tier 1（文件系统隔离）/ Tier 2（Job Object）不隔离输入，Tier 3（Windows Sandbox）太重。定位策略、页面对象模式、等待模式直接沿用 windows-desktop-e2e；把其中的 `click_input()`（物理输入）换成这里的消息级 API 即可获得"边工作边跑测试"的能力。

## 已知限制（实测验证）

| 限制 | 原因 | 对策 |
|------|------|------|
| **Tkinter 等 GetCursorPos 路由的自绘工具包**对消息点击无响应 | Tk 的 GenerateXEvent 用真实光标位置路由事件（实测证实：光标在窗口上时消息点击才生效） | 换 UIA；或该类程序不适用本技能 |
| 硬件加速/DirectX 独占全屏程序截图全黑 | 层叠窗口/离屏不参与常规合成 | 文档化排除（用 windows-desktop-e2e Tier 3） |
| 自绘控件检查 `GetKeyState()`/`GetCursorPos()` 的交互可能失灵 | 消息级输入不改变真实键鼠状态 | 优先 WM_COMMAND/UIA 等无状态依赖路径 |
| 某些安全软件拦截 Job Object KILL_ON_JOB_CLOSE（本机实测被拦，错误 87） | EDR/加固系统钩子 | 已降级为 best-effort；孤儿进程靠 `stop` 的 `taskkill /T` 兜底，start 失败也会杀子进程 |
| offscreen/vd 模式截图冻结 | 离屏窗口停止重绘（实测证实） | 需要截图就用默认 ghost 模式 |
| 弹窗收拢前可能闪现一瞬（毫秒级，多数情况下用户无感） | WinEvent `EVENT_OBJECT_SHOW` 事件驱动收拢；钩子不可用时退化为一个轮询间隔（默认 0.4s） | 要求严格可调小 `start_auto_sweep(interval)` |
| AUT 代码显式调 `SetForegroundWindow` 时前台仍可能被抢走几十毫秒 | `WS_EX_NOACTIVATE` 不拦截显式调用 | 已由 `EVENT_SYSTEM_FOREGROUND` 钩子毫秒级归还 + `sweep` 每 tick 兜底（实测 3ms 采样探针抓不到任何一次被抢） |
| 搜狗等输入法的 SoPY_* 辅助窗口会跟 AUT 同 pid 出现 | IME 注入 | 已自动过滤，不会误隔离/误报 |

## 运行日志

日志存放在 `~/.ui-sandbox/logs/`，按天一个文件（`sb-YYYYMMDD.log`，UTF-8）。记录关键事件：`start/isolated/stop`（会话生命周期）、`isolate`（每个被隔离的窗口，含看门狗收拢的弹窗）、`fg_restore`（AUT 抢焦点后被强制归还）、`sweep`（轮询收拢所得）、`cli`（每条 sb.py 命令）——事后可回溯"什么时候弹了什么窗、有没有抢到用户焦点"。

**自动清理**：每个进程首次写日志时自动删除 30 天前的旧日志（无需维护，日志不会无限膨胀）。手动清理/查看：

```bash
python sb.py logs                  # 列出全部日志文件（大小/天龄）
python sb.py logs --prune-days 30  # 手动清理 30 天前旧日志
python sb.py logs --tail 50        # 附带查看最新日志的最后 50 行
```

## 故障排查

- `doctor` 检查依赖（pywin32/Pillow 必需；pywinauto/pyvda 可选）
- 截图全白/全黑：ghost 模式下全白多为截图时机太早（加 `time.sleep`）；全黑→硬件渲染，见上表
- `start` 报 "no main window"：`--title` 正则要匹配窗口标题或类名；或程序启动慢，加 `--timeout 30`
- 点了没反应：先 `text`/`tree` 确认控件存在 → 换更可靠的输入方式（见选型表）→ `sweep` 看是否有隐藏对话框挡着
- 所有命令对每个动作独立起进程，会话状态在 `~/.ui-sandbox/sessions/<name>/`

---

版权声明：作者：梅文海（2026-08-28）
