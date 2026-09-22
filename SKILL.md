---
name: zys-workbuddy-reskin
display_name: WorkBuddy 换肤
display_name_en: WorkBuddy Reskin
description: WorkBuddy 借壳换肤：把用户提供的图片制作成自包含皮肤 CSS（hero base64 内嵌 + 自动取色），覆盖到 WorkBuddy 官方内置主题的本地缓存（appearance-resources），用户在 设置→外观 选中该主题即通过官方合法路径应用自定义皮肤，且跨重启存活。内置健康自检（doctor 子命令），可检测皮肤因官方更新主题而失效的情况并一键修复。当用户说 「换肤」「换主题」「WorkBuddy 皮肤」「换个背景主题」「reskin」「回滚换肤」「换肤重做」「皮肤失效」「皮肤没了」「换肤失效了」「皮肤变回原版」 或想用自己的图片美化 WorkBuddy 界面时使用。纯 Python 实现，无需 CDP/调试端口。
description_zh: 把任意一张图片变成 WorkBuddy 桌面端的官方主题皮肤，借壳官方内置主题生效，跨重启存活；内置失效自检与一键修复。
description_en: Turn any image into a WorkBuddy desktop theme skin by hijacking a built-in theme's local cache; survives restarts. Includes a health check that detects and repairs skins broken by official theme updates.
category: design
version: 1.2.0
author: 周永三
---

# zys-workbuddy-reskin（WorkBuddy 换肤）

把用户的图片变成 WorkBuddy 的官方主题皮肤。原理是「借壳」：WorkBuddy 内置主题缓存于
`~/.workbuddy/appearance-resources/theme-<key>-<updatedAt>/`，主进程命中本地目录即返回、
**不校验内容与云端 zip 的一致性**。把自包含皮肤 CSS 覆盖进去，用户在官方设置里选中该主题，
皮肤即通过官方合法路径生效；选中后官方会写入 lastApplied.css 缓存 → **冷启动自动挂载 +
reconcile 校验通过 → 跨重启存活**。此路线已经过本机 5.5.4 版本完整验证（2026-09-09），
并于 5.5.6 复验（2026-09-11：预检通过、已换壳主题目录存活）。

## 工具

单文件 CLI：`scripts/reskin.py`（纯 Python 标准库；仅 `make` 需要 Pillow）。

| 子命令 | 作用 |
| --- | --- |
| `check` | 环境预检：平台/安装目录/客户端版本/外观功能支持（只读；exit 0=支持 3=不支持） |
| `detect` | 探测外观缓存目录、列出主题/映射/替换状态（只读，内置预检） |
| `make --image <图> --name <英文名>` | 压缩图片+自动取色+生成 CSS → `skins/<name>/` |
| `rebuild --name <名> [--apply]` | 用已有 hero 与配色重建 skin.css（CSS 模板升级后，无需重新给图） |
| `apply --name <名> --theme-key <key>` | 备份官方目录 → 覆盖其全部 skin.css |
| `status` | 查看已安装壳状态（按该主题**最新目录**判定，非 manifest 记录目录） |
| `doctor [--fix]` | 健康自检：扫描全部皮肤并报告失效原因；`--fix` 一键修复（覆盖前自动备份） |
| `redo --name <名> [--dry-run]` | 重新覆盖到该主题当前最新目录（官方更新/LRU 淘汰后） |
| `rollback --name <名>` | 从备份恢复官方原样 |

运行方式：`python <skill目录>/scripts/reskin.py <子命令>`。Python 优先用 WorkBuddy 自带的
（`~/.workbuddy/binaries/python/versions/*/python.exe`），兜底系统 `python`。

## 首次流程（用户首次触发「换肤」）

1. **环境预检（必须最先做）**：运行 `check`（只读，exit 0=支持 / 3=不支持），按 `verdict`
    分支处理：
    - `supported` → 继续，把预检结论（客户端版本、已缓存主题数）告知用户；
    - `unsupported_platform` → 告知用户：本 skill 仅适用 WorkBuddy **桌面端**（Windows/macOS），
        **小程序端与手机 App 端没有外观主题功能、也无法运行本 skill**；
    - `install_not_found` → 请用户确认已安装桌面版，或用 `--install-dir` 指定 WorkBuddy.exe
        所在目录后重跑；
    - `client_too_old` → 告知：外观（主题皮肤）功能自 **5.5.3** 版本起提供，当前客户端版本
        过低，请先升级 WorkBuddy 桌面端后再来换肤，**流程到此终止**；
    - `supported_not_activated` → 客户端支持但从未用过主题：请用户打开 设置→外观——
        ① 有主题入口 → 随意选中一款主题后重跑 `detect`；② 无外观入口 → 云端灰度未放开或
        账号非个人版（**外观功能仅对个人版开放**，企业版/ultimate/exclusive 不支持）。
        注意：`check` 用「app.asar 特征检测」而非仅版本号判断（版本号可能读不到或被改名），
        两者并用；即使版本够，云端灰度开关（EnableAppearance）与企业版限制仍可能关闭该功能，
        所以 `supported_not_activated` 的引导必须交给用户自查外观入口。
2. **引导与说明**：向用户介绍本 skill 的能力与注意事项（见下），然后运行 `detect`。
3. **选壳（单选）**：向用户提供官方长期主题选项，**单选**：
   「经典QQ、涟漪、有风、同行、伙伴」。
   五项主题与 resourceKey 的映射**已实测建档、全部 confirmed**（`tkboqn`=经典QQ、`tkbdzr`=涟漪、
   `tkbera`=有风、`tkbe90`=同行、`tkbdx8`=伙伴），直接按 `detect` 输出匹配即可，
   **不要再依据子皮肤资源名（qqblue/green/mint/purple/blue-video-fallback）反推主题名**——
   此推断法已于 2026-09-11 经用户实测证伪。
   显示 `unknown`（未建档主题）时，看 `detect` 给出的「本地目录自述名」并向用户确认，
   或让用户在 设置→外观 点选一次目标主题（触发缓存下载、目录 mtime 更新），
   重新 `detect` 后按 mtime 最新者判断。
    **若目标主题目录不存在 → 提示用户先启用一次该主题**（切回浅色主题非必须，但建议统一
    验收起点）。**限时联名主题（张韶涵/Jane金/激战金秋）一律不推荐**，官方可能下架致壳失效。
4. **收图**：请用户提供一张图片，参考建议：**明亮、低饱和的风景或人像图**；横版最佳
    （≥1600px 宽更清晰，过大也没关系会自动压缩）；避免大面积纯色或深色调图片。
    图片复制到 `skins/<英文名>/`（`make` 自动完成，不修改用户源文件）。
5. **制作**：运行 `make`。自动取色基于图片主色（accent 主色、secondary 邻近色、
    surface 提亮至 L≥0.93、text 深色并校验对比度 ≥7:1，任一环节失败回退内置暖金默认值），
    并沿用已定稿的透明度参数（首页透明、会话页 33% 薄纱、菜单栏/标题栏不透 hero、侧栏磨砂）。
    CSS 总体积自动压到 500KB 内（官方上限 512KB）。把生成的四色汇报给用户，允许手动指定
    （`--accent` 等参数）后重跑。
6. **覆盖**：运行 `apply --name <名> --theme-key <key> --label <官方名>`。
    首次覆盖自动整目录备份到 `backups/`。然后输出验收指引（见下）。

### 验收指引（apply / redo 成功后必须完整输出，防用户失去上下文）

```
1. 设置 → 外观：先选其他主题，再重新选中「<所选主题>」
   （该动作即触发客户端刷新主题缓存；2026-09-14 实测无需重启即可生效）
2. 若界面未刷新，再重启 WorkBuddy（普通重启即可，无需任何启动器）
3. 验收：首页与会话页应呈现您提供的图片与配色
```

并告知用户：

- 效果不满意可回来微调：换图（重新 make+apply 同一壳）、调色（make 支持 --accent 等）、
    调透明度档位（薄纱 20%~50% 之间报数字即可）
- 本 skill 具备 **回滚（恢复官方原样）/ 重做（主题被淘汰后重新覆盖）/ 换壳（替换其他主题）**
    能力，随时说「回滚换肤」「换肤重做」「换个主题壳」即可
- 主题选择器里的缩略图仍显示官方原图（封面来自云端 CDN），属正常现象，选中后即是新皮肤

## 再次触发的分支路由

**第 0 步（任何再次触发都先做）：健康自检 `doctor`。** 见下节「皮肤失效自检」。

- **换壳**（把另一个内置主题也换成用户皮肤，或对已替换壳整体重来）→ **重走首次流程**
    （第 2 步起）：必须重新列出 5 款主题单选——用户可能希望多个内置主题都换成不同皮肤，
    不能默认沿用上次的壳。`status` 可先展示已替换的壳供参考。
- **换图**（只换某个壳的图片素材）→ `status` 列出已有壳，单壳自动选定、多壳请用户挑，
    然后从第 3 步收图开始（跳过选壳）。
- **回滚** → `rollback --name <名>`（多壳时先让用户挑）。
- **重做 / 皮肤失效**（用户报告「皮肤没了」「变回原版了」）→ 直接跑 `doctor`，按报告处理：
    可修项用 `doctor --fix`（或对单个壳 `redo --name <名>`）；
    若报「该主题已无本地缓存」→ 先请用户在 设置→外观 启用一次该主题，再 `redo`。

## 皮肤失效自检（每次会话开始或用户提到换肤时执行）

`~/.workbuddy/skills/zys-workbuddy-reskin` 里若已存在皮肤（`skins/*/manifest.json`），
**在会话开始、或用户谈及换肤/皮肤异常时，先运行一次**：

```bash
python <skill目录>/scripts/reskin.py doctor
```

- 退出码 `0` = 全部正常；`1` = 存在异常。
- 读到 `[FAIL]` 项即皮肤已失效。**不要等用户自己发现**——主动告知并询问是否修复，
  或直接执行：

```bash
python <skill目录>/scripts/reskin.py doctor --fix
```

`doctor --fix` 会自动把失效皮肤重新覆盖到该主题的**当前最新目录**，覆盖前对官方原版
自动整目录备份（可回滚），随后按验收指引提示用户刷新。

**为什么会失效（必须向用户解释清楚）**：官方更新某主题时会**新建**
`theme-<key>-<updatedAt>` 目录并重新下载，客户端随之改用新目录，旧目录里我们的
自定义 CSS 不再被读取 —— 于是界面回到官方原样。这不是皮肤损坏，`doctor --fix`
即可恢复，无需重新制作。

## 注意事项（务必遵守）

1. **常见失效原因有两种，都靠 `doctor --fix` / `redo` 恢复**：
   ① **官方更新主题 → 换缓存目录**（目录名 `<key>-<updatedAt>` 的 updatedAt 变化）：缓存未命中、
   重新下载官方 zip，我们所在的旧目录被旁路（2026-09-14 实例：客户端启动后批量刷新 4 个主题目录）。
   ② **官方改主题包内的主样式文件名**（2026-09-22 实测：客户端升 **5.6.2** 后主样式由 `skin.css`
   改为 **`skin.v2.css`**，客户端优先加载它；官方 asar 中亦确认认识该文件名）：若只覆盖旧文件名，
   会漏掉真正生效的文件，此时子皮肤文件已替换、主文件仍是官方原版，属于"看起来像部分生效实则失效"。
   工具现以正则 `^skin(\.v\d+)?\.css$` 匹配 `skin.css` / `skin.v2.css` 等多种写法；
   遇到全新格式时先 `detect` 观察目录内真实文件名，必要时扩展该正则（`SKIN_CSS_RE`）。
   **配套纪律**：`is_reskinned()` 判定要求目录内**所有** `skin*.css` 都带标记（`all()`），
   任何"只覆盖了一部分"的情况都必须判为失效，否则会重演 2026-09-22 的静默失效。
2. **appearance-resources 上限 8 个目录，LRU 按 mtime 淘汰**：用户下载新官方主题可能挤掉
    已替换的壳。被挤掉后 `doctor` 会报「该主题已无本地缓存」，必须先请用户在 设置→外观
    启用一次该主题（触发重新下载），再 `redo`。
3. **健康判定必须基于「该主题当前最新目录」，不能看 manifest 记录的 dir_name**——记录会随
    官方更新而过期。`status` / `doctor` 已按此实现；自行排查时若只看记录目录，会把失效误判为正常。
4. **备份不能放在 appearance-resources 里面**（可能被目录扫描误读），只放本 skill 的 `backups/`。
5. **只覆盖 skin.css，不动官方 png/mp4 等资源**；CSS 自包含（hero base64 内嵌、零外部引用），
    官方资源成为死重不影响呈现。
6. **不要直写 localStorage**（workbuddy.appearance.*）——冷启动会被云端目录校验重置回 light，
    此路已验证不通，勿再尝试。
7. **版本适用性**：外观（主题皮肤）功能自 **WorkBuddy 5.5.3** 起提供（官方更新日志确认，
    2026-09-08 前后上线），且**仅对个人版账号开放**（客户端代码实测：企业版
    ultimate/exclusive 被排除）；**小程序端与手机 App 端无此功能**。机制细节验证于
    5.5.4 / Windows，5.5.6 复验通过（2026-09-11），**5.6.2 复验并适配**（2026-09-22：
    主题包主样式已改名 `skin.v2.css`，现已纳入覆盖范围）。`check` 子命令以 app.asar 特征检测为主、版本号为辅做预检；
    macOS 路径已适配探测但未实测。**WorkBuddy 大版本升级可能改变主样式文件名、锚点类名或缓存机制**
    ——升版后若皮肤失效，先跑 `detect` 查看目录内真实文件名（见注意事项 1-②）。
8. **备份纪律**：`apply`、`redo` 与 `doctor --fix` 在**目标为官方原版**时都会先整目录备份；
    目标已是自定义皮肤则跳过（避免无意义重复备份）。`rollback` 恢复**最后一个**备份，
    即最新版官方原样，不会出现版本错配。
9. **生效方式**：在 设置→外观 先选其他主题、再重新选中目标主题，即可刷新缓存
    （2026-09-14 实测**无需重启**）；仅在界面未刷新时才需要重启客户端。
10. 对外开源分发时附 README.md 免责声明（见文件）。
11. **客户端大版本升级可能更换「背景承载容器」**（2026-09-22 实测：5.6.2 = v2 机制）：
    5.5.x 用 `#root` 整屏铺图；5.6.2 起改用 `.wb-home-route`（首页新路由）/
    `.main-content--welcome` / `.teams-container` / `.workbuddy-app` / `[data-view-id="main-content"]`，
    独立视图另用 `.claw-workspace__main`（助理）、`.automation-main-page` /
    `.code-buddy-automation` / `.automation-workspace__content`（定时任务）、`.ec-main-content`（专家中心）。
    `CSS_TEMPLATE` 已并列承载上述全部容器（两块：「背景图载体」＋「v2 独立视图页面」）。
    **某页面无图 = 该页主容器不在列表中** → 按「从 app.asar 反查类名 → 补入选择器 → `rebuild --apply`」处理。
    **体积红线**：背景图以 `--zys-skin-hero` 变量只存一份 base64，新增规则块必须用 `var()` 引用；
    若多处内联 base64（约 350KB/份）会突破官方 512KB 上限，导致整个皮肤静默失效。
