# WorkBuddy 5.8.x 皮肤机制（源码级结论）

> 依据：2026-10-17 对 5.8.3 的完整逆向与实机验证（含断网/联网对照实验）。
> 读完本篇可避免重复踩坑。**关键结论：5.8.3 起，当前主题的皮肤包由服务端下发且启动必刷新，本地文件无法阻止。**

---

## 一、四层缓存结构

| 层 | 位置 | 读取方 | 作用 |
|---|---|---|---|
| ① 磁盘缓存 | `~/.workbuddy/appearance-resources/<key>-<updatedAt>/skin*.css` | 渲染层 `runtimeLoad` 的**本地分支** | **非当前主题**走这层；当前主题不走 |
| ② 启动快照 | `localStorage['workbuddy.appearance.lastApplied.css']` | `skin-manager.applyCachedCssSync` | 冷启动**第一帧**注入 |
| ③ zip 派生缓存 | CacheStorage，cache 名 `workbuddy-appearance-zips-v1` | `ResourceLoader.loadZip` | 由 ④ 解包重建，供渲染层取用 |
| ④ HTTP 原始缓存 | `~/.workbuddy/app/session/Cache/Cache_Data/f_*` | Chromium HTTP 缓存 | 原始 zip 响应体；③ 的重建源 |

---

## 二、5.8.3 的加载顺序（`appearance-runtime-*.js`）

```js
function runtimeLoad(e) {
  if (H && e.zipUrl) try {
    const { css } = await H(e);            // ① 本地优先（读磁盘）
    return { resourceKey, updatedAt, css, objectUrls: [] };
  } catch {
    log('[applier] local-load-failed … fallback to zip');
  }
  return M.load(e);                        // ② 回退 zip 路径
}

// M.load → load(e, n, r, i)
if (!i) {                                  // i = forceRefresh
  const t = await zipCache.get(e, n);      // ③ CacheStorage
  if (t) { validateArchive(...); return { fromPersistentCache: true } }
  catch { await zipCache.delete(e, n) }
}
const s = await fetch(zipUrl);             // ④ 联网下载
await zipCache.put(e, n, buffer);          // 先写缓存，再应用
```

### 关键行为差异（本次核心发现）

| 主题类型 | 加载路径 | 结果 |
|---|---|---|
| **内置浅色/深色** | `applyTheme-builtin` → `clearTheme` | 不读任何 CSS，正常显示内置外观 |
| **非当前主题**（点击切换） | `runtimeLoad` → **本地优先** | **读取磁盘自定义 CSS ✅** |
| **当前主题**（启动时） | **包路径**（CacheStorage / fetch） | **使用官方包，磁盘被绕过 ❌** |

**实机证据**：把全部主题目录写入自定义皮肤后，切换任意非当前主题均显示自定义皮肤；
唯独「当前主题」在启动 40 秒后被官方覆盖。

---

## 三、联网 / 断网的行为差异

| 网络状态 | 行为 | 结果 |
|---|---|---|
| **断网** | `fetch` 失败 → 使用已有 `f_*` 缓存 | 可显示缓存中的内容；但主题列表失效 → 回退 `light`（约 60 秒后变空白/浅色），外观设置入口也会消失 |
| **联网** | `fetch` 成功 → **新建** `f_*` 条目（不复用旧的）→ 重建 CacheStorage | 官方包覆盖一切本地修改 |

> 联网时客户端**不会复用**既有的 `f_*` 条目，而是新建一个（实测 `f_000021` 与旧的 `f_000013` 内容相同但文件不同）。

---

## 四、`validateArchive` 的三项硬限制

| 限制 | 阈值 | 说明 |
|---|---|---|
| 条目数 | **500** | 超出抛 `too many entries` |
| 单文件解压大小 | **10 MB** | 超出抛 `archive expanded content is too large` |
| 归档总大小 | **20 MB** | 下载前查 `content-length`，超限直接拒绝 |

另有路径安全检查：不得以 `/` 开头、不得含 `..`。

---

## 五、已穷尽的七条本地路径（全部无效）

| # | 尝试 | 结果 |
|---|---|---|
| 1 | 改磁盘 `skin.v2.css` / 子 `skin.css` | 当前主题启动时不读本地 |
| 2 | 改 CacheStorage zip 内容 | 被启动重建覆盖 |
| 3 | 删除 CacheStorage | 立即重建 |
| 4 | 改 HTTP 缓存 `f_*` | 联网时新建条目，不读旧的 |
| 5 | 原始包 + 派生包**双改** | 联网仍新建下载覆盖 |
| 6 | 写所有主题目录 | 只对「非当前主题」有效 |
| 7 | 断网 | 回退浅色，外观入口消失 |

**结论：纯文件修改无法让「当前主题」在重启后保持自定义皮肤。**

---

## 六、版本演进

| 版本 | 机制要点 | 对换肤的影响 |
|---|---|---|
| 5.5.3 – 5.6.2 | 主进程磁盘优先，`skin.css` 存在即直接用 | 磁盘修改可跨重启存活 |
| **5.8.3**（2026-10-15） | 当前主题改为**包优先 + 启动刷新**；新增 CacheStorage 与 HTTP 两层 | 磁盘仅对非当前主题有效；重启需手动切换 |

---

## 七、当前策略（v1.3.0）

1. **默认（省心）**：写入全部可见主题 → 重启后切任意其他主题即恢复，只需点一下。
2. **可选（全自动）**：以调试端口启动 + 后台守护，检测到覆盖即重新注入（`guard.py`）。

> 判断某主题是否为「当前主题」：看客户端设置里的选中项，通常就是上次使用的那一个。
