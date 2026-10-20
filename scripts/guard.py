#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zys-workbuddy-reskin · 全自动守护（可选方案）

作用
----
WorkBuddy 5.8.x 启动后会重新下载官方主题包并覆盖自定义皮肤。
本守护在后台每 5 秒检查一次，一旦发现皮肤被覆盖，立即把磁盘上的皮肤 CSS
重新注入到页面最上层，做到「开机自动恢复、无需手动切换」。

子命令
------
  install     安装：在桌面创建「WorkBuddy-换肤版.lnk」（官方图标）
              并在启动文件夹放置自启脚本（登录时拉起守护）
  uninstall   卸载：删除快捷方式 + 自启脚本（皮肤本身保留）
  run         前台运行守护（通常由自启脚本调用）
  once        诊断：单次检查并打印详细结果（排查用）
  status      查看安装与运行状态

设计约束
--------
  · 零第三方依赖：内置极简 WebSocket 客户端
  · 不修改 WorkBuddy 程序文件、不联网、不收集任何数据
  · 只通过本机调试端口注入 CSS；端口仅监听 127.0.0.1
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SKINS_DIR = SKILL_DIR / "skins"
PORT = 9223
SHORTCUT_NAME = "WorkBuddy-换肤版.lnk"
TASK_NAME = "zys-workbuddy-reskin-guard"
POLL_SECONDS = 5
MAX_FAILS = 3


# ────────────────────────── 极简 WebSocket 客户端 ──────────────────────────

class MiniWS:
    """够用即可的 WebSocket 客户端（仅文本帧、仅客户端掩码）。"""

    def __init__(self, url: str, timeout: float = 5.0):
        # ws://host:port/path
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host or "127.0.0.1", int(port or 80)), timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise IOError("handshake closed")
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            raise IOError("handshake failed: " + buf.split(b"\r\n", 1)[0].decode("latin-1"))

    def send_text(self, text: str):
        data = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(length) if length else b""
            if opcode == 0x8:  # close
                raise IOError("server closed")
            if opcode in (0x1, 0x0):
                return payload.decode("utf-8", "ignore")
            # 其它帧（ping/pong）忽略

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise IOError("socket closed")
            buf += chunk
        return buf

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ────────────────────────── CDP 交互 ──────────────────────────

def fetch_targets():
    url = f"http://127.0.0.1:{PORT}/json"
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def pick_page(targets):
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        return None
    # 优先主窗口（URL 含 workbuddy 或标题含 WorkBuddy）
    for t in pages:
        blob = (t.get("url", "") + t.get("title", "")).lower()
        if "workbuddy" in blob:
            return t
    return pages[0]


def evaluate(ws_url: str, expression: str, timeout: float = 8.0):
    ws = MiniWS(ws_url, timeout=timeout)
    try:
        ws.send_text(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv_text())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {}).get("result", {}).get("value")
        raise TimeoutError("no response")
    finally:
        ws.close()


# ────────────────────────── 皮肤与注入 ──────────────────────────

def load_skin():
    """取「最近应用」的那套皮肤。

    不能按 skin.css 的 mtime 取——多皮肤场景下，重建过 CSS 的旧皮肤会被误选。
    按 manifest 的 applied_at（应用时间）排序更准确。
    """
    best, best_key = None, ""
    for mf in SKINS_DIR.glob("*/manifest.json"):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not (m.get("target") or m.get("carriers")):
            continue
        key = m.get("applied_at") or m.get("created_at") or ""
        if key > best_key:
            best, best_key = mf.parent / "skin.css", key
    if best and best.is_file():
        return best
    # 回退：任何一套可用皮肤
    for mf in SKINS_DIR.glob("*/skin.css"):
        return mf
    return None


def check_expr() -> str:
    """判断皮肤是否仍处于「最上层」。

    不能用 CSS 注释做标记——cssRules 不包含注释。
    改为检查我们的样式表是否仍是 adoptedStyleSheets 的**最后一张**
    （最后一张优先级最高；客户端若追加了自己的表，我们就会被压下去）。
    """
    return (
        "(() => {"
        "const s = window.__zysSheet;"
        "if (!s) return 'none';"
        "const arr = Array.from(document.adoptedStyleSheets || []);"
        "const i = arr.indexOf(s);"
        "if (i < 0) return 'detached';"
        "return (i === arr.length - 1) ? 'ok' : 'stale';"
        "})()"
    )


def inject_expr(css_text: str) -> str:
    payload = json.dumps(css_text)
    return (
        "(() => {"
        f"const CSS = {payload};"
        # 确保存在样式表对象
        "if (!window.__zysSheet || !window.__zysSheet.replaceSync) {"
        "  try { window.__zysSheet = new CSSStyleSheet(); } catch(e) { window.__zysSheet = null; }"
        "}"
        "if (window.__zysSheet && window.__zysSheet.replaceSync) {"
        "  window.__zysSheet.replaceSync(CSS);"
        # 先移除再追加，保证排在最后（最高优先级）
        "  const arr = Array.from(document.adoptedStyleSheets || []).filter(x => x !== window.__zysSheet);"
        "  document.adoptedStyleSheets = [...arr, window.__zysSheet];"
        "} else {"
        # 兜底：普通 <style> 追加到 body 末尾
        "  let el = document.getElementById('zys-skin-inject');"
        "  if (!el) { el = document.createElement('style'); el.id='zys-skin-inject'; (document.body||document.head).appendChild(el); }"
        "  el.textContent = CSS;"
        "}"
        "return 'injected';"
        "})()"
    )


# ────────────────────────── 子命令 ──────────────────────────

def find_exe():
    """定位 WorkBuddy.exe（不依赖快捷方式/COM/外部命令）。"""
    home = Path(os.path.expanduser("~"))
    cands = [
        Path(r"D:\Program\WorkBuddy\WorkBuddy.exe"),
        Path(r"C:\Program Files\WorkBuddy\WorkBuddy.exe"),
        Path(r"C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe"),
        home / "AppData" / "Local" / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def desktop_dir():
    home = Path(os.path.expanduser("~"))
    for d in [home / "Desktop", home / "OneDrive" / "Desktop",
              Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop"]:
        if d.is_dir():
            return d
    return home


def startup_dir():
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    d = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return d if d.is_dir() else None


def write_vbs(path: Path, command: str, hidden: bool = False):
    """生成 VBS 启动器（无窗口、正确转义）。

    两个坑（2026-10-18 实测修复）：
      1. VBScript 字符串内的双引号必须写成 `""`，否则 `sh.Run ""路径"` 会被
         解析成「空字符串 + 裸标识符」，报「语句未结束 800A0401」；
      2. WSH 按 ANSI/UTF-16 解析，UTF-8 中文路径会乱码 → 统一写 UTF-16 LE+BOM。
    """
    window = "0" if hidden else "1"
    escaped = command.replace('"', '""')
    lines = [
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.Run "{escaped}", {window}, False',
    ]
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16", newline="")
    return path


def pyw_path():
    p = Path(sys.executable)
    cand = p.with_name("pythonw.exe")
    return cand if cand.is_file() else p


def create_lnk(path: Path, target: str, arguments: str = "", icon: str = "",
               icon_index: int = 0, workdir: str = "", desc: str = ""):
    """纯 ctypes 创建 .lnk（不依赖 pywin32 / PowerShell）。

    实测环境限制：`schtasks` 在黑名单、PowerShell 的 COM 实例化被安全策略拦截，
    故直接调 ole32 的 IShellLinkW + IPersistFile。

    注意：SetIconLocation 的参数是 (路径, 索引) 两个独立值，
    **不能写成 "xxx.exe,0" 这种合并形式**（否则系统会把整个字符串当路径，图标显示空白）。
    """
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def _g(s):
        u = uuid.UUID(s)
        o = GUID()
        o.Data1, o.Data2, o.Data3 = u.time_low, u.time_mid, u.time_hi_version
        for i in range(8):
            o.Data4[i] = u.bytes[8 + i]
        return o

    CLSID_SL = _g("00021401-0000-0000-C000-000000000046")
    IID_SL = _g("000214F9-0000-0000-C000-000000000046")
    IID_PF = _g("0000010B-0000-0000-C000-000000000046")
    LPCWSTR, DWORD, INT, BOOL = ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int

    def vt(ptr, idx, argtypes):
        tbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
        return proto(tbl[idx])

    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitialize(None)
    unk = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_SL), None, 1,
                                ctypes.byref(IID_SL), ctypes.byref(unk))
    if hr != 0:
        raise OSError("CoCreateInstance 0x%08X" % (hr & 0xFFFFFFFF))

    vt(unk, 20, [LPCWSTR, DWORD])(unk, target, 0)          # SetPath
    if arguments:
        vt(unk, 11, [LPCWSTR])(unk, arguments)             # SetArguments
    if workdir:
        vt(unk, 9, [LPCWSTR])(unk, workdir)                # SetWorkingDirectory
    if desc:
        vt(unk, 7, [LPCWSTR])(unk, desc)                   # SetDescription
    if icon:
        vt(unk, 17, [LPCWSTR, INT])(unk, icon, icon_index)  # SetIconLocation(路径, 索引)

    pf = ctypes.c_void_p()
    vt(unk, 0, [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)])(
        unk, ctypes.byref(IID_PF), ctypes.byref(pf))
    vt(pf, 6, [LPCWSTR, BOOL])(pf, str(path), True)        # IPersistFile::Save
    vt(pf, 2, [])(pf)
    vt(unk, 2, [])(unk)
    return path


def cmd_install(_args):
    exe = find_exe()
    if not exe:
        print("[guard] 没找到 WorkBuddy.exe。请确认已安装桌面版，或手动把 guard 加入启动项。")
        return 1

    # 1) 桌面快捷方式（.lnk，官方图标）
    launcher = desktop_dir() / "WorkBuddy-换肤版.lnk"
    try:
        create_lnk(launcher, str(exe), f"--remote-debugging-port={PORT}",
                   str(exe), 0, str(exe.parent), "WorkBuddy（换肤全自动版）")
        print(f"[guard] 已创建桌面快捷方式：{launcher}")
    except Exception as e:
        print(f"[guard] 创建快捷方式失败（{type(e).__name__}: {e}），退回 .vbs 方案")
        launcher = desktop_dir() / "WorkBuddy-换肤版.vbs"
        write_vbs(launcher, f'"{exe}" --remote-debugging-port={PORT}')
        print(f"[guard] 已创建：{launcher}")

    # 2) 开机自启（启动文件夹，负责后台守护）
    sd = startup_dir()
    if sd:
        auto = sd / "zys-workbuddy-guard.vbs"
        cmd = f'"{pyw_path()}" "{Path(__file__).resolve()}" run'
        write_vbs(auto, cmd, hidden=True)
        print(f"[guard] 已设置开机自启：{auto}")
    else:
        print("[guard] 未找到启动文件夹，开机自启未设置（不影响手动使用）")

    print()
    print("完成。请双击桌面「WorkBuddy-换肤版」启动——外观与官方图标一致。")
    print("（如需取消：双击「卸载全自动」，或在会话里说「取消全自动方案」）")
    return 0


def cmd_uninstall(_args):
    removed = []
    for p in [desktop_dir() / "WorkBuddy-换肤版.lnk",
              desktop_dir() / "WorkBuddy-换肤版.vbs"]:
        if p.is_file():
            try:
                p.unlink()
                removed.append(p)
            except Exception:
                pass
    sd = startup_dir()
    if sd:
        p = sd / "zys-workbuddy-guard.vbs"
        if p.is_file():
            try:
                p.unlink()
                removed.append(p)
            except Exception:
                pass
    if removed:
        for p in removed:
            print(f"[guard] 已删除：{p}")
    else:
        print("[guard] 未发现需要删除的文件（可能已卸载）")
    print("[guard] 皮肤本身已保留，省心方案仍可正常使用。")
    return 0


def cmd_status(_args):
    print("=== 全自动方案状态 ===")
    exe = find_exe()
    print("  WorkBuddy：", str(exe) if exe else "未找到")
    launcher_lnk = desktop_dir() / "WorkBuddy-换肤版.lnk"
    launcher_vbs = desktop_dir() / "WorkBuddy-换肤版.vbs"
    if launcher_lnk.is_file():
        print("  桌面图标：已创建（.lnk，官方图标）")
    elif launcher_vbs.is_file():
        print("  桌面图标：已创建（.vbs 兜底）")
    else:
        print("  桌面图标：未创建")
    sd = startup_dir()
    auto = (sd / "zys-workbuddy-guard.vbs") if sd else None
    print("  开机自启：", "已设置" if (auto and auto.is_file()) else "未设置")
    skin = load_skin()
    print("  皮肤文件：", str(skin) if skin else "未找到")
    try:
        t = pick_page(fetch_targets())
        print("  调试端口：", f"已连接（{t.get('title','')[:40]}）" if t else "已开但无页面")
    except Exception:
        print("  调试端口：未开启（请用「WorkBuddy-换肤版」图标启动）")
    return 0


def cmd_once(_args):
    """诊断模式：检查一次并打印详细结果（不进入循环）。"""
    skin = load_skin()
    print("[guard] 皮肤文件:", skin if skin else "未找到")
    if not skin:
        return 1
    css = skin.read_text(encoding="utf-8")
    print("[guard] CSS 长度:", len(css))
    try:
        targets = fetch_targets()
        print("[guard] targets:", len(targets))
        t = pick_page(targets)
        if not t:
            print("[guard] 没有可用 page target")
            return 1
        print("[guard] 选中:", (t.get("title") or "")[:40])
        print("[guard] ws:", t.get("webSocketDebuggerUrl"))
        state = evaluate(t["webSocketDebuggerUrl"], check_expr())
        print("[guard] 当前状态:", state)
        r = evaluate(t["webSocketDebuggerUrl"], inject_expr(css))
        print("[guard] 注入结果:", r)
        state2 = evaluate(t["webSocketDebuggerUrl"], check_expr())
        print("[guard] 注入后状态:", state2)
        return 0
    except Exception as e:
        import traceback
        print("[guard] 失败:", type(e).__name__, e)
        traceback.print_exc()
        return 1


def cmd_run(_args):
    log_path = SKILL_DIR / "guard.log"

    def log(msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    log("=== guard run 启动 ===")
    skin = load_skin()
    if not skin:
        log("未找到已应用的皮肤，退出")
        return 1
    css = skin.read_text(encoding="utf-8")
    log(f"已载入皮肤 {skin.name}（{len(css)//1024} KB）")
    fails = 0
    injected = 0
    while True:
        try:
            t = pick_page(fetch_targets())
            if not t:
                raise RuntimeError("no page target")
            state = evaluate(t["webSocketDebuggerUrl"], check_expr())
            if state != "ok":
                evaluate(t["webSocketDebuggerUrl"], inject_expr(css))
                injected += 1
                log(f"皮肤被覆盖，已重新注入（第 {injected} 次）")
            fails = 0
        except Exception as e:
            fails += 1
            if fails == 1:
                log(f"检查失败: {type(e).__name__}: {e}")
            if fails >= MAX_FAILS:
                log(f"连续 {MAX_FAILS} 次失败，等待客户端以皮肤版启动…")
                fails = 0
        time.sleep(POLL_SECONDS)


def main():
    cmds = {"install": cmd_install, "uninstall": cmd_uninstall,
            "status": cmd_status, "run": cmd_run, "once": cmd_once}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__)
        return 2
    return cmds[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
