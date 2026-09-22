#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zys-workbuddy-reskin — WorkBuddy 借壳换肤工具（单文件实现）

原理：WorkBuddy 的内置主题缓存于 ~/.workbuddy/appearance-resources/<resourceKey>-<updatedAt>/
主进程 getLocalResource 命中本地目录即返回、不校验内容与云端 zip 的一致性。
把自包含皮肤 CSS（hero 图 base64 内嵌）覆盖到目标主题目录的全部 skin.css，
再在官方 设置→外观 中选中该主题，官方即通过自身合法路径应用我们的皮肤，
且选中后会写入 lastApplied.css 缓存 → 跨重启存活（reconcile 校验通过）。

子命令：
  check             环境预检：平台/安装目录/客户端版本/外观功能支持（只读，exit 0=支持 3=不支持）
  detect            探测外观缓存目录、列出主题与替换状态（只读）
  make              图片 → 压缩/取色/生成 CSS（落在 skins/<name>/，不碰官方目录）
  rebuild           用已有 hero 与配色重建 skin.css（CSS 模板升级后，无需重新给图）
  apply             备份目标主题目录 → 覆盖其全部 skin.css
  status            列出已安装的壳与目标目录当前状态（按该主题「最新目录」判定，非记录目录）
  doctor            健康自检：扫描全部皮肤并报告失效原因；--fix 一键批量修复（含自动备份）
  redo              从 skins/<name>/skin.css 重新覆盖到该主题当前最新目录
  rollback          从 backups/ 恢复官方原样
"""

import argparse
import base64
import colorsys
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_DIR = Path(__file__).resolve().parent.parent
SKINS_DIR = SKILL_DIR / "skins"
BACKUPS_DIR = SKILL_DIR / "backups"
APPEARANCE_DIR = Path.home() / ".workbuddy" / "appearance-resources"

SKIN_MARK = "WORKBUDDY_SKIN:"
# 官方 512KB CSS 缓存上限（app.asar 实测 APPEARANCE_CSS_MAX_LEN）
CSS_MAX_BYTES = 512 * 1024
# 安全余量：以 500KB 为工程上限
CSS_TARGET_BYTES = 500 * 1024

# ── 客户端兼容性预检 ────────────────────────────────────────────────────
# 外观（主题皮肤）功能于 WorkBuddy 5.5.3 正式上线（官方更新日志与媒体实测确认，
# 2026-09-08 集中上线首批主题）。更早版本无 appearance facade，本 skill 不适用。
# 注意：小程序端与手机 App 端均无外观功能，且本 skill 只能运行于桌面端 Agent。
SUPPORT_MIN_VERSION = "5.5.3"
# app.asar 中的外观功能特征字符串（版本号判断可能被改名/误判，特征检测更可靠；
# 两者并用，任一支持即视为支持）。EnableAppearance 为云端灰度开关的 feature key，
# 存在于客户端只说明"客户端有此能力"，云端开关/账号版位仍可能关闭它。
FEATURE_MARKERS = (b"appearance-resources", b"EnableAppearance")

# 常见安装位置（Windows；macOS 见 find_install_dir）
INSTALL_CANDIDATES = [
    Path("D:/Program/WorkBuddy"),
    Path("C:/Program Files/WorkBuddy"),
    Path("C:/Program Files (x86)/WorkBuddy"),
    Path.home() / "AppData/Local/Programs/WorkBuddy",
]
MAC_INSTALL = Path("/Applications/WorkBuddy.app/Contents/Resources")

# ── resourceKey → 官方主题名映射（2026-09-11 用户实测校正；五项全部 confirmed）──
# 校正记录（重要，勿重蹈）：初版依据「子皮肤资源名」反推映射（mint→涟漪、purple→同行、
# 猫咪视频包→伙伴），经用户逐一核对本地缓存目录后**证伪**——子皮肤标识与产品主题名并非
# 一一对应，该推断法不可靠。现行五项映射均由用户实测确认，不得再改；如需扩展新主题，
# 一律以用户在 设置→外观 启用该主题后的实测结果（或目标目录中文自述名）为准。
THEME_MAP = {
    "confirmed": {
        "tkboqn": "经典QQ",  # 子皮肤 workbuddy-skin-qqblue
        "tkbdzr": "涟漪",    # 子皮肤 workbuddy-skin-blue-video-fallback
        "tkbera": "有风",    # 子皮肤 workbuddy-skin-green
        "tkbe90": "同行",    # 子皮肤 workbuddy-skin-mint
        "tkbdx8": "伙伴",    # 子皮肤 workbuddy-skin-purple
    },
    # 兼容旧流程保留；五项已全部确认，此表常态为空
    "probable": {},
    # 限时/联名主题（官方可能下架，不推荐作为壳）；括号内为本地目录自述名佐证。
    # 名称不带「（限时）」后缀——detect 输出会自动追加「（限时款，勿选）」标注。
    "known_extra": {
        "tkhlwz": "张韶涵联名",              # 张韶涵主题皮肤-v11
        "tkhm0p": "Jane金",                 # workbuddy-skin-jane-gold
        "tkmw7j": "激战金秋-龙狮城",         # WorkBuddy皮肤_激战金秋-龙狮城_v10
    },
}

DEFAULT_COLORS = {
    "accent": "#A08A52",
    "secondary": "#C9825F",
    "surface": "#F6F4EF",
    "text": "#2F2B26",
}

# ── 通用工具 ────────────────────────────────────────────────────────────

def die(msg, code=1):
    print(f"[reskin] ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def require_pillow():
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        print(
            "[reskin] 需要 Pillow 图像库。请执行以下任一命令安装：\n"
            "  1) python -m pip install --user pillow\n"
            "  2) 若使用 WorkBuddy 自带 Python，请用其完整路径，例如：\n"
            '     "C:\\Users\\<你>\\.workbuddy\\binaries\\python\\versions\\<版本>\\python.exe" -m pip install pillow\n'
            "安装完成后重试。",
            file=sys.stderr,
        )
        sys.exit(2)


def hex_ok(v):
    return isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) is not None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def css_escape_id(name):
    return re.sub(r"[^a-z0-9_-]", "", name.lower()) or "custom"


# ── check（环境预检）────────────────────────────────────────────────────

def find_install_dir(explicit=None):
    """定位桌面端安装目录（含 resources/app.asar 的目录）。"""
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if os.name == "nt":
        cands += INSTALL_CANDIDATES
    elif sys.platform == "darwin":
        cands.append(MAC_INSTALL.parent.parent)
    for c in cands:
        res = c / "resources" / "app.asar"
        if res.is_file():
            return c
    return None


def read_product_version(exe_path):
    """Windows 下读 exe 的 ProductVersion（非 Windows 返回 None）。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import struct

        path = str(exe_path)
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, data)
        val = ctypes.c_void_p()
        n = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(
            data, "\\VarFileInfo\\Translation", ctypes.byref(val), ctypes.byref(n)
        ):
            return None
        lang, cp = struct.unpack("HH", ctypes.string_at(val.value, 4))
        sub = f"\\StringFileInfo\\{lang:04X}{cp:04X}\\ProductVersion"
        if not ctypes.windll.version.VerQueryValueW(data, sub, ctypes.byref(val), ctypes.byref(n)):
            return None
        return ctypes.wstring_at(val.value, n.value - 1)
    except Exception:
        return None


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) if v else ()


def asar_feature_support(asar_path):
    """分块扫描 app.asar，返回命中的特征字符串集合。"""
    found = set()
    chunk_size = 8 * 1024 * 1024
    overlap = 64
    try:
        with open(asar_path, "rb") as f:
            prev = b""
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                buf = prev + chunk
                for m in FEATURE_MARKERS:
                    if m in buf:
                        found.add(m.decode())
                prev = chunk[-overlap:]
    except OSError:
        return None
    return found


def preflight(install_dir_arg=None):
    """执行环境预检，返回 (verdict_dict, exit_code)。exit 0=支持, 3=不支持。"""
    v = {"platform": sys.platform, "os": os.name}
    # 1) 平台：手机 App / 小程序端无法运行本 skill（也根本没有外观功能）
    if os.name != "nt" and sys.platform != "darwin":
        v["verdict"] = "unsupported_platform"
        v["message"] = ("本 skill 仅支持 WorkBuddy 桌面端（Windows / macOS）。"
                        "小程序端与手机 App 端没有外观主题功能，也无法运行本 skill。")
        return v, 3
    # 2) 安装目录
    inst = find_install_dir(install_dir_arg)
    v["install_dir"] = str(inst) if inst else None
    if inst is None:
        v["verdict"] = "install_not_found"
        v["message"] = ("未找到 WorkBuddy 桌面端安装目录。请确认已安装桌面版，"
                        "或用 --install-dir 指定 WorkBuddy.exe 所在目录。")
        return v, 3
    asar = inst / "resources" / "app.asar"
    # 3) 版本号（展示用）
    exe = inst / "WorkBuddy.exe"
    ver = read_product_version(exe)
    v["client_version"] = ver
    if ver:
        vt = version_tuple(ver)
        minv = version_tuple(SUPPORT_MIN_VERSION)
        v["version_ok"] = vt >= minv
    else:
        v["version_ok"] = None  # 读不到版本号时不凭此判死
    # 4) asar 特征检测（比版本号更可靠）
    markers = asar_feature_support(asar)
    v["feature_markers"] = sorted(markers) if markers is not None else None
    if markers is None:
        v["verdict"] = "asar_unreadable"
        v["message"] = f"无法读取 {asar}（权限或路径异常），无法判定功能支持。"
        return v, 3
    client_supports = len(markers) == len(FEATURE_MARKERS)
    v["client_supports_appearance"] = client_supports
    if not client_supports:
        v["verdict"] = "client_too_old"
        v["message"] = (f"当前客户端（{ver or '版本未知'}）未包含外观主题功能"
                        f"（该功能自 {SUPPORT_MIN_VERSION} 起提供）。请先升级 WorkBuddy 桌面端"
                        f"至 {SUPPORT_MIN_VERSION} 或更高版本，再使用本 skill。")
        return v, 3
    # 5) 运行时证据：外观缓存目录
    theme_count = len(list_theme_dirs())
    v["appearance_dir"] = str(APPEARANCE_DIR)
    v["theme_dir_count"] = theme_count
    if APPEARANCE_DIR.is_dir() and theme_count > 0:
        v["verdict"] = "supported"
        v["message"] = (f"客户端 {ver or ''} 支持外观主题功能，且已缓存 {theme_count} 个主题，"
                        "可以换肤。")
        return v, 0
    # 客户端支持但从未用过主题：还需云端灰度开关与账号版位两道检查（客户端无法程序化
    # 读取），引导用户自查「设置 → 外观」入口。
    v["verdict"] = "supported_not_activated"
    v["message"] = (f"客户端 {ver or ''} 具备外观功能，但尚未使用过任何主题"
                    "（未发现本地缓存）。请让用户打开 设置→外观："
                    "① 有主题入口 → 随意选中一款主题后重跑 detect；"
                    "② 无外观入口 → 可能是云端灰度未放开或账号非个人版"
                    "（外观功能仅对个人版开放，企业版/ultimate/exclusive 不支持）。")
    return v, 0


def cmd_check(args):
    v, code = preflight(args.install_dir)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    sys.exit(code)


# ── detect ──────────────────────────────────────────────────────────────

# 外观缓存目录命名：theme-<resourceKey>-<updatedAt(ms)>
DIR_NAME_RE = re.compile(r"(theme-[a-z0-9]+)-(\d+)")

# 主题包内的主样式文件名。5.5.x 为 skin.css；**5.6.2 起官方改用 skin.v2.css**
# （2026-09-22 实测：同一主题目录内出现 skin.v2.css，客户端优先加载它；漏覆盖即皮肤失效）。
# 用正则一并匹配，避免客户端再次改名或追加版本后缀时漏覆盖。
SKIN_CSS_RE = re.compile(r"^skin(\.v\d+)?\.css$", re.IGNORECASE)


def find_skin_css(theme_dir):
    """列出主题目录内全部待覆盖的皮肤样式文件（skin.css / skin.v2.css / …）。"""
    return [p for p in theme_dir.rglob("*.css") if SKIN_CSS_RE.match(p.name)]


def dir_timestamp(dir_name):
    """提取目录名尾部的 updatedAt（毫秒）；取不到返回 -1（排序时排最前）。"""
    m = DIR_NAME_RE.fullmatch(dir_name)
    return int(m.group(2)) if m else -1


def is_reskinned(theme_dir):
    """该主题目录是否已**整套**替换为自定义皮肤。

    要求找到的每个 skin*.css 都带标记：只覆盖了部分文件（例如漏掉根级 skin.v2.css）
    必须判为未替换，否则会把失效误报成正常——2026-09-22 即因此暴露。
    """
    files = find_skin_css(theme_dir)
    if not files:
        return False
    try:
        return all(SKIN_MARK in p.read_text(encoding="utf-8", errors="ignore")[:200]
                   for p in files)
    except Exception:
        return False


def latest_theme_dir(key, dirs=None):
    """按 resourceKey 取该主题**当前最新**的缓存目录（不依赖字典序）。

    key 可带或不带 `theme-` 前缀。返回 list_theme_dirs() 的元素或 None。
    官方更新主题时会新建 updatedAt 更大的目录，客户端改用新目录——因此
    「最新目录」才是客户端实际加载的那个，健康判定必须基于它。
    """
    k = key if key.startswith("theme-") else f"theme-{key}"
    pool = dirs if dirs is not None else list_theme_dirs()
    cands = [d for d in pool if d["resource_key"] == k]
    if not cands:
        return None
    return max(cands, key=lambda x: dir_timestamp(x["dir_name"]))


def list_theme_dirs():
    """返回 [ {resource_key, dir_name, mtime, skin_label, reskinned} ]"""
    if not APPEARANCE_DIR.is_dir():
        return []
    out = []
    for d in sorted(APPEARANCE_DIR.iterdir()):
        m = DIR_NAME_RE.fullmatch(d.name)
        if not d.is_dir() or not m:
            continue
        key = m.group(1)
        label = ""
        cjk_hint = ""
        # 从子目录名推断皮肤标识（workbuddy-skin-xxx / 中文包名）
        subs = [p.name for p in d.iterdir() if p.is_dir()]
        if subs:
            label = subs[0]
            # 官方限时主题常带中文自述目录名（如「张韶涵主题皮肤-v11」），
            # 可作为未知 resourceKey 的名称佐证，避免仅靠推断误认主题
            for s in subs:
                if re.search(r"[\u4e00-\u9fff]", s):
                    cjk_hint = s
                    break
        reskinned = is_reskinned(d)
        out.append({
            "resource_key": key,
            "dir_name": d.name,
            "mtime": int(d.stat().st_mtime),
            "mtime_h": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
            "skin_label": label,
            "cjk_hint": cjk_hint,
            "reskinned": reskinned,
        })
    return out


def cmd_detect(_args):
    # 先做环境预检（客户端是否支持外观功能），不支持则终止并给出指引
    v, code = preflight(None)
    print(f"[reskin] 预检：{v['message']}")
    if code != 0:
        sys.exit(code)
    if not APPEARANCE_DIR.is_dir():
        die(f"未找到外观缓存目录：{APPEARANCE_DIR}\n请确认 WorkBuddy 已安装，并在 设置→外观 中启用过任意主题一次。")
    dirs = list_theme_dirs()
    name_of = {}
    for k, v in THEME_MAP["confirmed"].items():
        name_of[f"theme-{k}"] = (v, "confirmed")
    for k, v in THEME_MAP["probable"].items():
        name_of.setdefault(f"theme-{k}", (v, "probable"))
    for k, v in THEME_MAP["known_extra"].items():
        name_of.setdefault(f"theme-{k}", (v, "limited"))
    dirs.sort(key=lambda x: -x["mtime"])
    print(f"外观缓存目录：{APPEARANCE_DIR}")
    print(f"共 {len(dirs)} 个主题目录（按最近使用排序）：\n")
    for d in dirs:
        label, conf = name_of.get(d["resource_key"], ("?", "unknown"))
        flag = "  ← 已替换为自定义皮肤" if d["reskinned"] else ""
        note = {"confirmed": "", "probable": "（推断，请向用户确认）",
                "limited": "（限时款，勿选）", "unknown": "（未知主题）"}[conf]
        if conf == "unknown" and d.get("cjk_hint"):
            label, note = d["cjk_hint"], "（本地目录自述名，供参考；请与用户确认）"
        print(f"  {d['dir_name']}  mtime={d['mtime_h']}  子皮肤={d['skin_label']}")
        print(f"      → {label}{note}{flag}")
    print("\n提示：若目标主题不在列表或映射存疑，让用户在 设置→外观 点选一次该主题"
          "（触发缓存下载/更新目录 mtime），然后重新运行 detect。")


# ── 取色 ────────────────────────────────────────────────────────────────

def sample_image(im, size=160):
    small = im.resize((size, size))
    px = small.load()
    return [px[x, y] for y in range(size) for x in range(size)]


def pick_accent(pixels):
    """HSV 30° 桶统计，饱和度加权；返回 (hex, confidence)"""
    buckets = {}
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if not (0.22 <= s <= 0.85 and 0.30 <= v <= 0.92):
            continue
        bk = int(h * 12) % 12
        w = s
        e = buckets.setdefault(bk, [0.0, [], []])
        e[0] += w
        e[1].append((r, g, b))
        e[2].append(s)
    if not buckets:
        return None, 0.0
    bk = max(buckets, key=lambda k: buckets[k][0])
    total_w, cols, sats = buckets[bk]
    conf = clamp(total_w / (len(pixels) * 0.5), 0, 1)
    avg = tuple(round(sum(c[i] for c in cols) / len(cols)) for i in range(3))
    return "#%02X%02X%02X" % avg, conf


def lighten_to(hex_color, l_target=0.93):
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l_target, min(s, 0.10))
    return "#%02X%02X%02X" % (round(r2 * 255), round(g2 * 255), round(b2 * 255))


def shift_hue(hex_color, deg, s_mul=0.9, l_mul=1.0):
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    r2, g2, b2 = colorsys.hls_to_rgb((h + deg / 360) % 1.0, clamp(l * l_mul, 0, 1), clamp(s * s_mul, 0, 1))
    return "#%02X%02X%02X" % (round(r2 * 255), round(g2 * 255), round(b2 * 255))


def luminance(hex_color):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def derive_colors(im):
    """从图片派生四色；置信不足或对比度不足时逐项回退默认。"""
    pixels = sample_image(im)
    colors, notes = {}, []
    accent, conf = pick_accent(pixels)
    if accent and conf >= 0.15:
        colors["accent"] = accent
        notes.append(f"accent 取自图片主色（置信度 {conf:.2f}）")
    else:
        colors["accent"] = DEFAULT_COLORS["accent"]
        notes.append("accent 置信不足，回退默认暖金")
    colors["secondary"] = shift_hue(colors["accent"], 25, s_mul=0.95, l_mul=0.95)
    surface_src = lighten_to(colors["accent"], 0.93)
    colors["surface"] = surface_src
    text_cand = shift_hue(colors["accent"], 0, s_mul=0.35, l_mul=0.16)
    colors["text"] = text_cand if contrast(colors["surface"], text_cand) >= 7.0 else DEFAULT_COLORS["text"]
    if contrast(colors["surface"], colors["text"]) < 7.0:
        colors["text"] = DEFAULT_COLORS["text"]
        notes.append("text 对比度不足，回退默认深棕")
    return colors, notes


# ── CSS 模板（移植自 skin-studio src/skin-css.mjs，规则一字不差）────────

CSS_TEMPLATE = """/* %%MARK%%%%ID%% | generated by zys-workbuddy-reskin */
body[data-application-name=workbuddy] {
  --wb-accent: %%ACCENT%%;
  --wb-secondary: %%SECONDARY%%;
  --wb-surface: %%SURFACE%%;
  --wb-text: %%TEXT%%;

  --cb-bg-primary: var(--wb-surface) !important;
  --cb-bg-secondary: color-mix(in srgb, var(--wb-surface) 94%, transparent) !important;
  --cb-panel-bg-primary: color-mix(in srgb, var(--wb-surface) 88%, transparent) !important;
  --cb-team-member-card-background: color-mix(in srgb, var(--wb-surface) 88%, transparent) !important;

  --cb-text-primary: var(--wb-text) !important;
  --cb-text-secondary: color-mix(in srgb, var(--wb-text) 70%, transparent) !important;
  --cb-text-disabled: color-mix(in srgb, var(--wb-text) 42%, transparent) !important;
  --cb-text-link: var(--wb-accent) !important;
  --cb-text-error-active: var(--wb-accent) !important;

  --cb-vscode-editor-background: var(--wb-surface) !important;
  --cb-vscode-sideBar-background: color-mix(in srgb, var(--wb-surface) 90%, transparent) !important;
  --cb-vscode-foreground: var(--wb-text) !important;
  --cb-vscode-editor-foreground: var(--wb-text) !important;
  --cb-vscode-descriptionForeground: color-mix(in srgb, var(--wb-text) 70%, transparent) !important;
  --cb-vscode-titleBar-activeBackground: var(--wb-accent) !important;
  --cb-vscode-titleBar-activeForeground: #ffffff !important;
  --cb-vscode-titleBar-inactiveBackground: color-mix(in srgb, var(--wb-accent) 80%, var(--wb-surface)) !important;
  --cb-vscode-titleBar-inactiveForeground: color-mix(in srgb, #ffffff 70%, transparent) !important;
  --cb-titlebar-control-hover-background: color-mix(in srgb, var(--wb-accent) 16%, transparent) !important;
  --cb-vscode-input-background: color-mix(in srgb, var(--wb-surface) 88%, transparent) !important;
  --cb-vscode-dropdown-background: color-mix(in srgb, var(--wb-surface) 94%, transparent) !important;
  --cb-vscode-list-hoverBackground: color-mix(in srgb, var(--wb-accent) 16%, transparent) !important;
  --cb-vscode-toolbar-hoverBackground: color-mix(in srgb, var(--wb-accent) 16%, transparent) !important;
  --cb-vscode-scrollbarSlider-background: color-mix(in srgb, var(--wb-accent) 30%, transparent) !important;
  --cb-vscode-scrollbarSlider-hoverBackground: color-mix(in srgb, var(--wb-accent) 50%, transparent) !important;
  --cb-vscode-textLink-foreground: var(--wb-accent) !important;
  --cb-vscode-widget-border: color-mix(in srgb, var(--wb-accent) 45%, transparent) !important;
  --cb-vscode-panel-border: color-mix(in srgb, var(--wb-accent) 30%, transparent) !important;

  --cb-button-dark-background: var(--wb-accent) !important;
  --cb-button-dark-foreground: #ffffff !important;
  --cb-button-dark-hover-background: color-mix(in srgb, var(--wb-accent) 85%, #000000) !important;
  --cb-vscode-button-background: var(--wb-accent) !important;
  --cb-vscode-button-foreground: #ffffff !important;
  --cb-vscode-button-hoverBackground: color-mix(in srgb, var(--wb-accent) 85%, #000000) !important;

  --cb-stroke-secondary: color-mix(in srgb, var(--wb-accent) 45%, transparent) !important;
  --cb-markdown-hr-border-color: color-mix(in srgb, var(--wb-accent) 30%, transparent) !important;
}

/* ── 背景图载体（跨版本兼容）─────────────────────────────────────────
   v1（≤5.5.x）：整屏背景挂在 #root。
   v2（5.6.2+）：客户端改用新容器——页面主区为 .wb-home-route（新路由 modules/home）
   或 .main-content--welcome（旧路由），最外层为 .teams-container。
   实测 5.6.2 官方主题包中 #root / [data-view-id] 出现 0 次，故必须并列书写。 */
/* 背景图统一存于 CSS 变量：base64 约 350KB，若在多个规则块内联会突破官方
   512KB 上限，故只在此声明一次，其余规则一律 var() 引用。 */
:root, body { --zys-skin-hero: url("%%HERO%%"); }

#root,
:root .teams-container,
body .teams-container,
:root .workbuddy-app,
:root .teams-main-content,
:root [data-view-id="main-content"],
:root .wb-home-route,
:root .teams-content-wrapper .teams-main-content .main-content--welcome {
  color: var(--wb-text) !important;
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--wb-surface) 96%, transparent) 0 22%, transparent 46%),
    linear-gradient(180deg, transparent 0 45%, color-mix(in srgb, var(--wb-surface) 78%, transparent) 78% 100%),
    var(--zys-skin-hero) right center / cover no-repeat fixed !important;
}

:root .wb-home-route > .workbuddy-topbar,
:root .wb-home-route .wb-home-cloud-header { background: var(--wb-surface) !important; }

/* ── v2 独立视图页面 ────────────────────────────────────────────────
   以下页面不在 [data-view-id] 体系内，主容器自带不透明背景、会挡住外层图，
   故单独为它们承载背景图（2026-09-22 实测：「助理」与「定时任务」曾被挡）。
     · 助理              → claw 工作区
     · 定时任务          → automation
     · 专家·技能·连接器  → expert-center */
:root .automation-main-page,
:root .code-buddy-automation,
:root .automation-workspace__content,
:root .claw-workspace__main,
:root [class*="claw-workspace"],
:root .expert-center-light .ec-page-layout .ec-main-content,
:root .ec-main-content {
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--wb-surface) 96%, transparent) 0 22%, transparent 46%),
    linear-gradient(180deg, transparent 0 45%, color-mix(in srgb, var(--wb-surface) 78%, transparent) 78% 100%),
    var(--zys-skin-hero) right center / cover no-repeat fixed !important;
}

.conversation-list,
.main-content,
.main-content--welcome,
.sidebar-next { background: transparent !important; }

[data-view-id=sidebar] {
  background: color-mix(in srgb, var(--wb-surface) 88%, transparent) !important;
  border-right: 1px solid color-mix(in srgb, var(--wb-accent) 45%, transparent) !important;
  backdrop-filter: blur(20px) saturate(1.12);
}

/* 注：v2（5.6.2+）下 [data-view-id="main-content"] 由「背景图载体」承载背景图，
   不再置透明；v1 场景该元素本就透明（图在外层 #root），两层同为 fixed 同图不冲突。 */

[data-view-id=main-content] .workbuddy-topbar,
div[data-testid=conversation-topbar] {
  background: var(--wb-surface) !important;
  background-color: var(--wb-surface) !important;
  opacity: 1 !important;
  border-bottom: 1px solid color-mix(in srgb, var(--wb-accent) 30%, transparent) !important;
}

div#workbuddy-menubar-container {
  background: var(--wb-surface) !important;
  background-color: var(--wb-surface) !important;
  opacity: 1 !important;
  border-bottom: 1px solid color-mix(in srgb, var(--wb-accent) 30%, transparent) !important;
}

main.wb-home-route { background: transparent !important; }

.conversation-shell {
  background: color-mix(in srgb, var(--wb-surface) 33%, transparent) !important;
}

[data-view-id=detail-panel] {
  background: color-mix(in srgb, var(--wb-surface) 88%, transparent) !important;
  backdrop-filter: blur(18px) saturate(1.08);
}
%%BRAND%%"""

BRAND_TMPL = """
#root::before {
  position: fixed;
  z-index: 20;
  top: 60px;
  left: max(300px, 22vw);
  content: %%BRAND%%;
  color: var(--wb-accent);
  font: 800 clamp(16px, 2vw, 30px)/1.2 ui-rounded, system-ui;
  text-shadow: 0 2px 10px white;
  pointer-events: none;
}

#root::after {
  position: fixed;
  z-index: 20;
  top: 104px;
  left: max(300px, 22vw);
  max-width: 42vw;
  content: %%HEADLINE%%;
  color: var(--wb-text);
  font: 750 clamp(18px, 2.7vw, 42px)/1.15 ui-rounded, system-ui;
  text-shadow: 0 2px 12px white;
  pointer-events: none;
}
"""


def build_css(skin_id, colors, hero_data_url, brand="", headline=""):
    if brand or headline:
        brand_block = BRAND_TMPL.replace("%%BRAND%%", json.dumps(brand or " "))
        brand_block = brand_block.replace("%%HEADLINE%%", json.dumps(headline or " "))
    else:
        brand_block = ""
    css = CSS_TEMPLATE
    css = css.replace("%%MARK%%", SKIN_MARK)
    css = css.replace("%%ID%%", skin_id)
    css = css.replace("%%ACCENT%%", colors["accent"])
    css = css.replace("%%SECONDARY%%", colors["secondary"])
    css = css.replace("%%SURFACE%%", colors["surface"])
    css = css.replace("%%TEXT%%", colors["text"])
    css = css.replace("%%HERO%%", hero_data_url)
    css = css.replace("%%BRAND%%", brand_block)
    return css


# ── make ────────────────────────────────────────────────────────────────

def cmd_make(args):
    require_pillow()
    from PIL import Image, ImageOps

    src = Path(args.image)
    if not src.is_file():
        die(f"图片不存在：{src}")
    name = css_escape_id(args.name)
    out_dir = SKINS_DIR / name
    out_dir.mkdir(parents=True, exist_ok=True)

    im = Image.open(src)
    im = ImageOps.exif_transpose(im)
    if im.mode != "RGB":
        im = im.convert("RGB")

    # 亮度评估（均值）
    px = sample_image(im)
    mean_v = sum((p[0] + p[1] + p[2]) / 3 for p in px) / len(px)
    if mean_v < 90:
        print(f"[reskin] WARN: 图片平均亮度偏低（{mean_v:.0f}/255），建议换用明亮、低饱和的图片，否则界面可能偏暗。")

    colors, notes = derive_colors(im)
    for k, v in args.__dict__.items():
        if k in DEFAULT_COLORS and v:
            if not hex_ok(v):
                die(f"--{k} 需为 #RRGGBB 格式")
            colors[k] = v
            notes.append(f"{k} 手动指定 {v}")

    target_w = args.width
    q = args.quality
    hero_bytes = None
    data_url = ""
    for attempt in range(6):
        w, h = im.size
        if w > target_w:
            im2 = im.resize((target_w, round(h * target_w / w)), Image.LANCZOS)
        else:
            im2 = im
        tmp = out_dir / "_tmp_hero.webp"
        im2.save(tmp, "WEBP", quality=q, method=4)
        hero_bytes = tmp.read_bytes()
        data_url = "data:image/webp;base64," + base64.b64encode(hero_bytes).decode()
        css = build_css(name, colors, data_url, args.brand, args.headline)
        if len(css.encode("utf-8")) <= CSS_TARGET_BYTES:
            break
        tmp.unlink(missing_ok=True)
        if q > 50:
            q = max(50, q - 10)
        else:
            target_w = int(target_w * 0.85)
    else:
        die("无法压缩到 500KB 以内，请提供更简洁的图片。")

    final_css = out_dir / "skin.css"
    final_css.write_text(css, encoding="utf-8")
    (out_dir / "hero.webp").write_bytes(hero_bytes)
    tmp.unlink(missing_ok=True)  # 清理压缩循环的临时文件
    shutil.copy2(src, out_dir / f"source{src.suffix.lower()}")
    manifest = {
        "name": name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_image": str(src),
        "colors": colors,
        "notes": notes,
        "hero": {"width": target_w, "quality": q, "bytes": len(hero_bytes)},
        "css_bytes": len(css.encode("utf-8")),
        "target": None,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reskin] 皮肤已生成：{out_dir}")
    print(f"  配色：{json.dumps(colors, ensure_ascii=False)}")
    for n in notes:
        print(f"  · {n}")
    print(f"  hero：宽{target_w}px / q{q} / {len(hero_bytes)//1024}KB；CSS 总计 {manifest['css_bytes']//1024}KB（上限 500KB）")
    print("  下一步：reskin.py apply --name %s --theme-key <resourceKey>" % name)


def cmd_rebuild(args):
    """用皮肤目录内已有的 hero 与配色重新生成 skin.css（模板升级后重建）。

    不重新取色、不重新压缩——完全复用 make 时定稿的素材与颜色，结果稳定可预期。
    用途：CSS 模板升级（例如适配客户端新版皮肤机制）后批量重建已有皮肤，
    无需用户重新提供图片。
    """
    import base64 as _b64

    manifest, skin_dir = load_manifest(args.name)
    hero_path = skin_dir / "hero.webp"
    if not hero_path.is_file():
        die(f"未找到 hero 素材：{hero_path}（无法重建，请重新 make）")
    colors = manifest.get("colors") or DEFAULT_COLORS
    data_url = "data:image/webp;base64," + _b64.b64encode(hero_path.read_bytes()).decode()
    css = build_css(manifest.get("name", args.name), colors, data_url,
                    manifest.get("brand", ""), manifest.get("headline", ""))
    size = len(css.encode("utf-8"))
    if size > CSS_TARGET_BYTES:
        die(f"重建后 CSS 超限（{size // 1024}KB > 500KB），请重新 make 压缩 hero。")
    css_path = skin_dir / "skin.css"
    if args.dry_run:
        print(f"[reskin] DRY-RUN：将重建 {css_path}（{size // 1024}KB，配色 {colors.get('accent')}）")
        return
    css_path.write_text(css, encoding="utf-8")
    manifest["css_bytes"] = size
    manifest["rebuilt_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (skin_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reskin] 已重建 {css_path}（{size // 1024}KB，配色 {colors.get('accent')}）")
    if args.apply:
        r = reskin_target(args.name)
        print(f"[reskin] 已同步覆盖 {r['n_files']} 个文件 → {r['theme_dir'].name}")
        print("生效方式：设置 → 外观 先选其他主题、再重新选中该主题（通常无需重启）。")
    else:
        print("下一步：python scripts/reskin.py redo --name %s" % args.name)


# ── apply / redo / rollback / status ───────────────────────────────────

def load_manifest(name):
    p = SKINS_DIR / css_escape_id(name) / "manifest.json"
    if not p.is_file():
        die(f"未找到皮肤 {name} 的 manifest：{p}\n请先运行 make。")
    return json.loads(p.read_text(encoding="utf-8")), p.parent


def find_theme_dir(theme_key):
    """定位该主题**当前最新**的缓存目录。

    按目录名尾部的 updatedAt 数值比较（而非字典序）——官方更新主题会新建
    updatedAt 更大的目录，客户端随即改用新目录，旧目录里的皮肤被旁路。
    """
    if not APPEARANCE_DIR.is_dir():
        die(f"未找到外观缓存目录：{APPEARANCE_DIR}")
    best, best_ts = None, -1
    for d in APPEARANCE_DIR.glob(f"theme-{theme_key}-*"):
        if not d.is_dir():
            continue
        ts = dir_timestamp(d.name)
        if ts > best_ts:
            best, best_ts = d, ts
    return best


def backup_theme(theme_dir):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = BACKUPS_DIR / f"{theme_dir.name}.orig_{stamp}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(theme_dir, dest)
    return dest


def overwrite_css(theme_dir, css_path):
    css_text = css_path.read_text(encoding="utf-8")
    targets = find_skin_css(theme_dir)
    if not targets:
        die(f"目标目录中未找到皮肤样式文件（skin.css / skin.v2.css）：{theme_dir}")
    for t in targets:
        t.write_text(css_text, encoding="utf-8")
    return targets


def cmd_apply(args):
    manifest, skin_dir = load_manifest(args.name)
    theme_key = re.sub(r"^theme-", "", args.theme_key)
    theme_dir = find_theme_dir(theme_key)
    if theme_dir is None:
        die(f"resourceKey theme-{theme_key} 无本地缓存目录——该主题可能尚未启用。\n"
            "请让用户在 设置→外观 点选一次该主题（触发下载），然后重试。")
    css_path = skin_dir / "skin.css"
    if not css_path.is_file():
        die(f"皮肤 CSS 不存在：{css_path}，请先 make。")
    if not is_reskinned(theme_dir):
        bak = backup_theme(theme_dir)
        print(f"[reskin] 已备份官方原目录 → {bak}")
        manifest.setdefault("backups", []).append(str(bak))
    else:
        print("[reskin] 目标目录已是自定义皮肤，跳过重复备份（官方原样备份保留首次的）。")
    if args.dry_run:
        tgts = list(theme_dir.rglob("skin.css"))
        print(f"[reskin] DRY-RUN：将覆盖 {len(tgts)} 个文件：")
        for t in tgts:
            print("   ", t)
        return
    tgts = overwrite_css(theme_dir, css_path)
    manifest["target"] = {"resource_key": f"theme-{theme_key}", "dir_name": theme_dir.name,
                          "official_label": args.label or THEME_MAP["confirmed"].get(theme_key, "")}
    manifest["applied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (skin_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[reskin] 已覆盖 {len(tgts)} 个 skin.css 到 {theme_dir.name}")
    print(f"[reskin] 主题官方名：{manifest['target']['official_label'] or '(未知)'}")
    print("\n=== 请用户验收 ===")
    print("1. 设置 → 外观：先选其他主题，再重新选中上述主题")
    print("   （该动作会触发客户端刷新主题缓存，实测无需重启即可生效）")
    print("2. 若界面仍未刷新，再重启 WorkBuddy")
    print("3. 确认首页/会话页呈现新皮肤；如不满意可提出调整（换图/调色/回滚）")


# 皮肤健康状态
ST_OK = "ok"                          # 最新目录已是自定义皮肤，且记录一致
ST_RECORD_STALE = "record_stale"      # 皮肤有效，但 manifest 记录的目录已过期（需同步）
ST_OFFICIAL_UPDATE = "official_update"  # 官方更新了该主题 → 新目录为官方原版，皮肤被旁路
ST_OVERWRITTEN = "overwritten"        # 同一目录被官方覆盖回原样
ST_NO_THEME_DIR = "no_theme_dir"      # 该主题已无本地缓存（LRU 淘汰 / 从未启用）
ST_UNAPPLIED = "unapplied"            # 尚未 apply

HEALTH_DESC = {
    ST_OK: "正常",
    ST_RECORD_STALE: "皮肤有效，但 manifest 记录的目录已过期（同步记录即可，不影响使用）",
    ST_OFFICIAL_UPDATE: "失效 · 官方更新了该主题（新目录为官方原版），自定义皮肤被旁路",
    ST_OVERWRITTEN: "失效 · 目标目录被官方覆盖回原样",
    ST_NO_THEME_DIR: "失效 · 该主题已无本地缓存（被 LRU 淘汰），需先在 设置→外观 启用一次该主题",
    ST_UNAPPLIED: "未 apply",
}

# 可自动修复的状态：重新覆盖（幂等；覆盖前若目标为官方原版会自动备份）
FIXABLE = (ST_RECORD_STALE, ST_OFFICIAL_UPDATE, ST_OVERWRITTEN)


def assess_skin(manifest, theme_dirs=None):
    """判定单个皮肤的健康状态。

    判定基准是**该主题当前最新的目录**（即客户端实际加载的那个），而非
    manifest 里记录的 dir_name——记录会随官方主题更新而过期，这正是
    「皮肤静默失效但 status 仍显示正常」的根因。
    """
    name = manifest.get("name", "?")
    t = manifest.get("target")
    if not t:
        return {"status": ST_UNAPPLIED, "name": name, "label": "", "latest": None,
                "recorded": None, "recorded_stale": False}
    key = t["resource_key"]
    label = (t.get("official_label")
             or THEME_MAP["confirmed"].get(key.replace("theme-", ""), "")
             or "(未知)")
    pool = theme_dirs if theme_dirs is not None else list_theme_dirs()
    latest = latest_theme_dir(key, pool)
    recorded = next((d for d in pool if d["dir_name"] == t.get("dir_name")), None)
    stale = latest is not None and latest["dir_name"] != t.get("dir_name")
    if latest is None:
        status = ST_NO_THEME_DIR
    elif latest["reskinned"]:
        status = ST_RECORD_STALE if stale else ST_OK
    elif stale:
        status = ST_OFFICIAL_UPDATE
    else:
        status = ST_OVERWRITTEN
    return {"status": status, "name": name, "label": label, "latest": latest,
            "recorded": recorded, "recorded_stale": stale}


def cmd_status(_args):
    manifests = sorted(SKINS_DIR.glob("*/manifest.json"))
    if not manifests:
        print("当前没有任何已生成的皮肤。")
        return
    theme_dirs = list_theme_dirs()
    bad = 0
    for mf in manifests:
        m = json.loads(mf.read_text(encoding="utf-8"))
        a = assess_skin(m, theme_dirs)
        line = f"· {a['name']}  colors={m['colors'].get('accent')}"
        if a["status"] == ST_UNAPPLIED:
            print(line + "  （未 apply）")
            continue
        latest_name = a["latest"]["dir_name"] if a["latest"] else "(无缓存目录)"
        extra = ""
        if a["recorded_stale"]:
            extra = f"  [记录目录 {m['target'].get('dir_name')} 已过期]"
        if a["status"] == ST_OK:
            print(line + f"  → {latest_name}（{a['label']}）已生效")
        elif a["status"] == ST_RECORD_STALE:
            print(line + f"  → {latest_name}（{a['label']}）已生效{extra}，可用 redo 同步")
        else:
            bad += 1
            print(line + f"  → {latest_name}（{a['label']}）⚠ {HEALTH_DESC[a['status']]}{extra}")
            if a["status"] in FIXABLE:
                print(f"    修复：python scripts/reskin.py redo --name {a['name']}")
            else:
                print("    修复：先在 设置→外观 启用一次该主题（触发缓存下载），再 redo")
    if bad:
        print(f"\n共 {bad} 个皮肤需要修复；可运行 doctor --fix 一键处理。")
    else:
        print("\n全部皮肤状态正常。")


def reskin_target(skin_name, do_backup=True):
    """把皮肤覆盖到其目标主题的**当前最新**目录，并同步 manifest 记录。

    供 redo 与 doctor --fix 共用。返回 {theme_dir, n_files, changed, backup}。
    覆盖前若目标目录是官方原版，默认先整目录备份（保证可回滚）。
    """
    manifest, skin_dir = load_manifest(skin_name)
    t = manifest.get("target")
    if not t:
        die(f"皮肤 {skin_name} 从未 apply 过，请先用 apply。")
    css_path = skin_dir / "skin.css"
    if not css_path.is_file():
        die(f"皮肤 CSS 不存在：{css_path}，请先 make。")
    theme_dir = find_theme_dir(t["resource_key"].replace("theme-", ""))
    if theme_dir is None:
        die(f"主题 {t['resource_key']} 无本地缓存目录。\n"
            "请让用户在 设置→外观 启用一次该主题（触发下载），然后重试。")
    changed = theme_dir.name != t.get("dir_name")
    bak = None
    if is_reskinned(theme_dir):
        print("[reskin] 目标目录已是自定义皮肤，跳过备份。")
    elif do_backup:
        bak = backup_theme(theme_dir)
        manifest.setdefault("backups", []).append(str(bak))
        print(f"[reskin] 已备份官方原样 → {bak}")
    else:
        print("[reskin] 目标目录为官方原版（本次未备份）")
    tgts = overwrite_css(theme_dir, css_path)
    # 关键修复：把记录同步到实际生效目录，避免 status 一直盯着过期目录
    manifest["target"] = {
        "resource_key": t["resource_key"],
        "dir_name": theme_dir.name,
        "official_label": t.get("official_label", ""),
    }
    manifest["applied_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (skin_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"theme_dir": theme_dir, "n_files": len(tgts), "changed": changed, "backup": bak}


def cmd_redo(args):
    manifest, _ = load_manifest(args.name)
    t = manifest.get("target") or {}
    if not t:
        die("该皮肤从未 apply 过，请先用 apply。")
    old_dir = t.get("dir_name", "(未记录)")
    if args.dry_run:
        theme_dir = find_theme_dir(t.get("resource_key", "").replace("theme-", ""))
        print(f"[reskin] DRY-RUN：将把皮肤 {args.name} 覆盖到 "
              f"{theme_dir.name if theme_dir else '(未找到目标目录)'}")
        return
    r = reskin_target(args.name)
    print(f"[reskin] redo 完成：{r['n_files']} 个 skin.css 已重新覆盖到 {r['theme_dir'].name}。")
    if r["changed"]:
        print(f"[reskin] 注意：官方更新了该主题，目标目录已由 {old_dir} 变为 "
              f"{r['theme_dir'].name}，manifest 记录已同步。")
    print("生效方式：设置 → 外观 先选其他主题、再重新选中该主题"
          "（实测无需重启）；若界面未刷新则重启 WorkBuddy。")


def cmd_doctor(args):
    """健康自检：扫描全部皮肤，报告失效原因；--fix 自动修复可修项。

    退出码：0 = 全部正常；1 = 存在异常（含修复后仍有异常）。
    """
    manifests = sorted(SKINS_DIR.glob("*/manifest.json"))
    if not manifests:
        print("当前没有任何已生成的皮肤。")
        sys.exit(0)
    theme_dirs = list_theme_dirs()
    print(f"外观缓存目录：{APPEARANCE_DIR}")
    print(f"缓存主题目录数：{len(theme_dirs)}（客户端上限 8，超出后按 mtime LRU 淘汰）\n")
    problems = []
    for mf in manifests:
        m = json.loads(mf.read_text(encoding="utf-8"))
        a = assess_skin(m, theme_dirs)
        latest = a["latest"]["dir_name"] if a["latest"] else "(无缓存目录)"
        if a["status"] == ST_OK:
            print(f"[ OK ] {a['name']:16s} → {latest}（{a['label']}）")
            continue
        if a["status"] == ST_UNAPPLIED:
            print(f"[ -- ] {a['name']:16s} 尚未 apply")
            continue
        if a["status"] == ST_RECORD_STALE:
            print(f"[ OK ] {a['name']:16s} → {latest}（{a['label']}）皮肤有效；"
                  f"记录目录 {m['target'].get('dir_name')} 已过期")
        else:
            print(f"[FAIL] {a['name']:16s} → {latest}（{a['label']}）")
            print(f"       {HEALTH_DESC[a['status']]}")
            if a["recorded_stale"]:
                print(f"       manifest 记录的目录 {m['target'].get('dir_name')} 已过期")
        problems.append(a)

    fixable = [a for a in problems if a["status"] in FIXABLE]
    unfixable = [a for a in problems if a["status"] not in FIXABLE]
    print()
    if not problems:
        print("结论：全部皮肤状态正常，无需处理。")
        sys.exit(0)
    if unfixable:
        print("以下皮肤需人工介入（先让用户在 设置→外观 启用对应主题，再重跑）：")
        for a in unfixable:
            print(f"  - {a['name']}（{a['label']}）：{HEALTH_DESC[a['status']]}")
    if not args.fix:
        print(f"发现 {len(problems)} 个异常，其中 {len(fixable)} 个可自动修复。")
        print("自动修复：python scripts/reskin.py doctor --fix")
        sys.exit(1)
    if not fixable:
        print("没有可自动修复的项。")
        sys.exit(1)
    print(f"开始自动修复 {len(fixable)} 个皮肤…\n")
    failed = []
    for a in fixable:
        print(f"--- 修复 {a['name']} ---")
        try:
            r = reskin_target(a["name"])
            print(f"[reskin] 完成：{r['n_files']} 个 skin.css → {r['theme_dir'].name}\n")
        except SystemExit:
            failed.append(a["name"])
            print(f"[reskin] 修复失败：{a['name']}\n", file=sys.stderr)
    print("修复完毕。请在 设置→外观 重新选中对应主题以刷新缓存。")
    sys.exit(1 if (failed or unfixable) else 0)


def cmd_rollback(args):
    manifest, skin_dir = load_manifest(args.name)
    t = manifest.get("target")
    if not t:
        die("该皮肤从未 apply 过，无需回滚。")
    baks = manifest.get("backups") or []
    if not baks:
        die("manifest 中没有备份记录（可能 apply 时目标已是自定义皮肤）。")
    bak = Path(baks[-1])
    if not bak.is_dir():
        die(f"备份目录不存在：{bak}")
    theme_dir = find_theme_dir(t["resource_key"].replace("theme-", ""))
    if theme_dir is None:
        die(f"目标主题目录不存在，无法回滚：{t['dir_name']}")
    if args.dry_run:
        print(f"[reskin] DRY-RUN：将用 {bak} 覆盖 {theme_dir}")
        return
    for src in bak.rglob("*"):
        if src.is_file():
            rel = src.relative_to(bak)
            dst = theme_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    print(f"[reskin] 回滚完成：{theme_dir.name} 已恢复官方原样。")
    print("提示：请在 设置→外观 先选其他主题、再重新选中该主题以刷新缓存（通常无需重启）。")


def main():
    ap = argparse.ArgumentParser(prog="reskin.py", description="zys-workbuddy-reskin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("check", help="环境预检：平台/安装目录/版本/外观功能支持（只读）")
    pc.add_argument("--install-dir", default=None,
                    help="WorkBuddy 安装目录（含 WorkBuddy.exe 与 resources/），找不到时手动指定")
    pc.set_defaults(func=cmd_check)

    sub.add_parser("detect", help="探测外观缓存目录与主题（只读）").set_defaults(func=cmd_detect)

    pm = sub.add_parser("make", help="图片 → 生成皮肤 CSS")
    pm.add_argument("--image", required=True)
    pm.add_argument("--name", required=True, help="皮肤名（英文/拼音，将作 CSS id 与目录名）")
    pm.add_argument("--width", type=int, default=2200)
    pm.add_argument("--quality", type=int, default=78)
    pm.add_argument("--accent"), pm.add_argument("--secondary")
    pm.add_argument("--surface"), pm.add_argument("--text")
    pm.add_argument("--brand", default=""), pm.add_argument("--headline", default="")
    pm.set_defaults(func=cmd_make)

    prb = sub.add_parser("rebuild", help="用已有 hero 与配色重建 skin.css（CSS 模板升级后）")
    prb.add_argument("--name", required=True)
    prb.add_argument("--apply", action="store_true", help="重建后立即覆盖到主题目录")
    prb.add_argument("--dry-run", action="store_true")
    prb.set_defaults(func=cmd_rebuild)

    pa = sub.add_parser("apply", help="备份并覆盖目标主题")
    pa.add_argument("--name", required=True)
    pa.add_argument("--theme-key", required=True, help="如 tkbera（不带 theme- 前缀亦可）")
    pa.add_argument("--label", default="", help="主题官方名（用于汇报）")
    pa.add_argument("--dry-run", action="store_true")
    pa.set_defaults(func=cmd_apply)

    sub.add_parser("status", help="查看已安装壳状态（按主题最新目录判定）").set_defaults(func=cmd_status)

    pd = sub.add_parser("doctor", help="健康自检：扫描全部皮肤并报告失效原因（--fix 自动修复）")
    pd.add_argument("--fix", action="store_true",
                    help="自动修复可修项（重新覆盖到最新目录，覆盖前自动备份官方原样）")
    pd.set_defaults(func=cmd_doctor)

    pr = sub.add_parser("redo", help="重新覆盖到该主题当前最新目录（官方更新/LRU 淘汰后）")
    pr.add_argument("--name", required=True)
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=cmd_redo)

    pb = sub.add_parser("rollback", help="恢复官方原样")
    pb.add_argument("--name", required=True)
    pb.add_argument("--dry-run", action="store_true")
    pb.set_defaults(func=cmd_rollback)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
