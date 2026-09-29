---
doc_type: plan
status: draft
---

# PR-1 收尾：前端 X-Session-Token 注入（修桌面端 403 折中）

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-29 |
| **目标** | 桌面端 webview 调受保护端点（`GET /api/analysis/logs`）不再 403 |
| **范围** | `desktop.py`（Api 类 +1 方法）、`frontend/src/api/`（token 模块 + request 封装） |
| **不做** | 不扩大鉴权端点覆盖面（10.2 已定论：本地单用户全量鉴权属过度设计）；不改后端 auth 逻辑 |

---

## 0. 背景与现状

PR-1（2026-09-10，agent.md §10.25）落地 D 方案鉴权：

- `desktop.py:533-541` 启动时 `secrets.token_urlsafe(32)` 生成 token → 写 `session.token` + 设 `NOVEL_ANALYZER_SESSION_TOKEN` 环境变量
- `backend/api/auth.py:21-44` `require_session_token` Depends 校验 `X-Session-Token` header（`hmac.compare_digest`）；环境变量未设（开发模式）直接放行
- 当前唯一受保护端点：`routes_analysis.py:276` 的 `GET /api/analysis/logs`

**已知折中**（auth.py:31-34 docstring 自认）：前端 `client.ts` 从不发送
`X-Session-Token` header → 桌面端 webview 调日志下载端点**必 403**。
即 PR-1 上线至今，受保护的功能在桌面端实际不可用。本 plan 兑现当初
"前端拦截加 header 作为后续 PR" 的承诺。

## 1. 设计决策

### D1：token 经 pywebview js_api 传递，不走文件 / URL / 端点

| 备选 | 结论 |
|---|---|
| ~~新增 `GET /api/session-token`~~ | 否决：未鉴权端点返回 token = 防御归零 |
| ~~webview 读 `session.token` 文件~~ | 否决：JS 无文件系统 |
| **`Api.get_session_token()` js_api 方法** | ✅ 采用 |

- `desktop.py` Api 类（`desktop.py:330`，现有 pick_files/minimize 等同为同步方法）
  新增同步方法：返回 `os.environ.get(SESSION_TOKEN_ENV, "")`；未设返回空串不抛错（开发模式 / exe 首启窗口期）
- **信任域论证**：pywebview js_api 仅暴露给 webview 内加载的页面（本地 dist，
  与 `/ws/progress` 的 Origin 白名单同一信任模型）；单用户本地应用中
  webview JS 与 token 本就同信任域，不构成提权
- token 仍由 `_on_closed` 退出时清理（`desktop.py:603,626,630`），行为不变

### D2：client.ts 模块级单例 + 双路径就绪时机

新建 `frontend/src/api/sessionToken.ts`（独立小模块，可测）：

```ts
let token: string | null = null
export function getSessionToken(): string | null { return token }
export function __setSessionTokenForTests(t: string | null) { token = t }
export function initSessionToken(): void {
  const wv = (window as any).pywebview?.api
  if (wv?.get_session_token) {
    wv.get_session_token().then((t: unknown) => {
      if (typeof t === 'string' && t) token = t
    }).catch(() => { /* 浏览器 / 早于 ready：保持 null */ })
  }
}
```

- client.ts 模块加载时调一次 `initSessionToken()`；
  `pywebviewready` 晚于模块加载的场景由 desktop.py 侧兜底：js_api 在
  `pywebviewready` 前调用会静默失败（pywebview 行为），因此再挂
  `window.addEventListener('pywebviewready', initSessionToken, { once: true })` 双保险
- `request()`（`client.ts:268-305`，55 个方法唯一出口）附加：

```ts
const t = getSessionToken()
if (t) headers['X-Session-Token'] = t
```

- **不做 403 自动重试**：受保护端点只有日志下载，触发时机（设置页手动点击）
  远晚于 pywebviewready；自动重试会掩盖配置类问题
- 安全：token 仅存模块内存，不进 localStorage、不打印日志、不进 query

### D3：开发模式零影响

- 浏览器 dev（5173）：无 pywebview → token 恒 null → header 永不附加；
  后端 env 未设 → 放行。两条路径天然闭合，**不需要动 CORS**。
- 不支持"浏览器 dev + 手动设 NOVEL_ANALYZER_SESSION_TOKEN"组合
  （自定义 header 跨域需 CORS allow_headers 配合，见 §5 不做）。

## 2. 任务清单

| Phase | # | 文件 | 改动 | 验收 |
|---|---|---|---|---|
| 1 | T1.1 | `desktop.py` | Api 类加 `get_session_token()`（docstring 写明信任域论证 + 返回空串语义） | env 设/未设两态直接实例化调用正确（不依赖窗口） |
| 1 | T1.2 | `frontend/src/api/sessionToken.ts` | 新建（上节代码） | vitest：setter/init 双路径 |
| 2 | T2.1 | `frontend/src/api/client.ts` | 模块底部 `initSessionToken()` + pywebviewready 监听；`request()` 附加 header | 见 T3.1 |
| 3 | T3.1 | `frontend/src/api/client.test.ts` | +2 例：`__setSessionTokenForTests('t')` 后请求带 header；null 时不带 | vitest 绿 |
| 3 | T3.2 | `backend/tests/test_desktop_pythonw.py` | +2 例：Api.get_session_token env set → 返回值；unset → 空串 | pytest 绿 |

## 3. 验收标准

1. 桌面端（run_desktop.bat）启动 → 设置页 → 下载运行日志 → **200**（改前 403）
2. 浏览器 dev 模式所有端点行为不变（无 header，后端放行）
3. `python -m pytest` / `npm test` / `npm run build` 全绿
4. `session.token` 生命周期不变（启动生成 / 退出清理）

## 4. 兼容性与回滚

- 后端零改动 → 无兼容性问题；前端 header 附加对"env 未设"的后端是无害冗余
- 回滚：revert 前端两文件 + desktop.py 一个方法即可，与其他改动无耦合

## 5. 不做（显式排除）

- 不扩大 `require_session_token` 到更多端点（10.2 决策维持）
- 不做 403 自动重试 / token 轮换
- 不动 CORS allow_headers（dev 模式无 token 场景，YAGNI；未来若支持浏览器+env 组合再补）
- 不把 token 存 localStorage（页面刷新后由 initSessionToken 重新获取）

## 6. 协调注意

另一会话正在工作树增加测试深度（1 号提交因此推迟）。本 plan 触碰
`client.test.ts`，实施前先 `git status` 对账，避免同文件并行。
