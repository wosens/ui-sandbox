版本：v1.0.2

[2026-09-18] ghost 停靠点可配置（sb.py start --park x,y / SandboxSession a_Park，session.json 持久化，sweep/看门狗共用）；修复 keys 单字符键调用不存在的 VkKeyExW 崩溃（改 VkKeyScanExW） - scripts/sandbox.py, scripts/sb.py, SKILL.md
[2026-09-23] start 自动收割同名残留会话（按 pid 强杀+清 state，禁 /IM 全名杀，防宿主进程泄漏；日志 reaped_stale） - scripts/sandbox.py SKILL.md
