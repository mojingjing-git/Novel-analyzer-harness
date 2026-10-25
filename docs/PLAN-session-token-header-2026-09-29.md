---
doc_type: plan
status: done
---

# PR-1 收尾：前端 X-Session-Token 注入（修桌面端 403 折中）

| 字段 | 值 |
|---|---|
| **日期** | 2026-09-29（2026-09-30 按独立复审修订） |
| **目标** | 桌面端 webview 调受保护端点（`GET /api/analysis/logs`）不再 403 |
| **范围** | `desktop.py`（Api 类 +1 方法）、`frontend/src/api/`（token 模块 + request 封装 + 下载方法）、`frontend/src/pages/QueuePage.vue`（下载按钮） |
| **不做** | 不扩大鉴权端点覆盖面（10.2 已定论：本地单用户全量鉴权属过度设计）；不改后端 auth 逻辑 |

---

## 0. 背景与现状

PR-1（2026-09-10，agent.md §10.25）落地 D 方案鉴权：

- `desktop.py:537-541` `main()` 内 `secrets.token_urlsafe(32)` 生成 token → 写 `session.token` + 设 `NOVEL_ANALYZER_SESSION_TOKEN` 环境变量（frozen exe 与源码同一路径；生成失败直接 `sys.exit`，**不存在 exe 首启的空 token 窗口期**——空串分支实际只服务浏览器/dev 场景）
- `backend/api/auth.py:21-44` `require_session_token` Depends 校验 `X-Session-Token` header（`hmac.compare_digest`）；环境变量未设（开发模式）直接放行
- 当前唯一受保护端点：`routes_analysis.py:276` 的 `GET /api/analysis/logs`（全仓 grep 确认仅此一处 `Depends(require_session_token)`）

**已知折中**（auth.py:31-34 docstring 自认）：前端从不发送 `X-Session-Token`
header → 桌面端 webview 调日志下载端点**必 403**。

**复审修正的关键事实（2026-09-30）**：该端点的唯一前端消费点是
`QueuePage.vue:421` 的 `<a href="/api/analysis/logs" target="_blank">下载日志</a>`
——**顶层导航，不是 fetch**，浏览器不会附带任何 JS 注入的 header。
且 `request()`（client.ts:268-305）对"200 + 非 JSON body"显式抛错
（client.ts:299-303 的 P3 契约），而该端点返回 `text/plain` 的 FileResponse
（routes_analysis.py:282-286）——**不能复用 request() 调它**。
所以本 plan 必须同时修两条路：header 注入（fetch 场景）+ 独立下载函数（导航场景）。

## 1. 设计决策

### D1：token 经 pywebview js_api 传递，不走文件 / URL / 端点

| 备选 | 结论 |
|---|---|
| ~~新增 `GET /api/session-token`~~ | 否决：未鉴权端点返回 token = 防御归零 |
| ~~webview 读 `session.token` 文件~~ | 否决：JS 无文件系统 |
| **`Api.get_session_token()` js_api 方法** | ✅ 采用 |

- `desktop.py` Api 类（`desktop.py:330`，`__init__` 仅存 port 无 webview 依赖）
  新增同步方法：返回 `os.environ.get(SESSION_TOKEN_ENV, "")`；未设返回空串不抛错
- **信任域论证（含前提）**：pywebview js_api 暴露给"webview 当前加载的任何页面"；
  本应用 webview 只加载本地 dist（desktop.py:608-618），不渲染远程内容，
  与 `/ws/progress` 的 Origin 白名单同一信任模型。单用户本地应用中
  webview JS 与 token 本就同信任域，不构成提权
- token 生命周期不变：`_on_closed` 退出清理（`desktop.py:603,626,630`）

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

- client.ts 模块加载时调一次 `initSessionToken()`，并挂
  `window.addEventListener('pywebviewready', initSessionToken, { once: true })`
  双保险（与 AppLayout.vue:105-108 既有模式一致；pywebviewready 每次页面
  加载会重发，刷新后自然重取）
- `request()` 附加 header，**合并顺序明确规定**（options 在后展开是现状，
  token 必须在 options.headers 之后合并才不被覆盖）：

```ts
headers: { 'Content-Type': 'application/json', ...options?.headers,
           ...(t ? { 'X-Session-Token': t } : {}) }
```

- **不做 403 自动重试**：受保护端点只有日志下载，触发时机（队列页手动点击）
  远晚于 pywebviewready；自动重试会掩盖配置类问题
- 安全：token 仅存模块内存，不进 localStorage、不打印日志、不进 query；
  403 响应体为静态文案（auth.py:43），无 token 入日志路径

### D3：日志下载改走 fetch + blob（不复用 request()）

- client.ts 新增 `downloadLogs()`：独立 `fetch('/api/analysis/logs', { headers 含 token })`
  → 非 ok 抛错（带 status/detail，与 request() 错误形态一致）→ ok 则
  `res.blob()` → `URL.createObjectURL` + 临时 `<a download="analyzer.log">` 触发保存
- **显式绕开 request() 的 JSON 解析契约**（client.ts:299-303 对 text/plain 必抛错，
  该契约不为单端点破坏）
- `QueuePage.vue:421` 的 `<a href>` 改为 `<button @click="api.downloadLogs()">`：
  保留 `.glass-button` 类与 `v-tooltip`（§9.4 设计系统），tooltip 文案
  "新窗口打开完整运行日志"（QueuePage.vue:275 附近）同步改为"下载完整运行日志"

### D4：开发模式零影响（CORS 修正表述）

- 浏览器 dev（5173）：无 pywebview → token 恒 null → header 永不附加；后端
  env 未设 → 放行。且 dev 的 `/api` 走 vite proxy 同源转发（vite.config.ts
  server.proxy），后端 CORS 本就 `allow_headers=["*"]`（app.py:198-205）
  ——**不需要任何 CORS 改动**
- 不支持"浏览器 dev + 手动设 NOVEL_ANALYZER_SESSION_TOKEN"组合（YAGNI，见 §5）

### D5：范围外路径的鉴权状态（记录，免复审反复追问）

- `/ws/progress`：Origin 白名单鉴权（ws.py:24-46），浏览器 WebSocket API
  本就无法设自定义 header——不需要也不可能在本次加 header
- desktop.py 自身 30s 轮询（urllib）只打未保护端点（`/api/health`、
  `/api/analysis/status`、`/api/summary/status`）——不受影响

## 2. 任务清单

| Phase | # | 文件 | 改动 | 验收 |
|---|---|---|---|---|
| 1 | T1.1 | `desktop.py` | Api 类加 `get_session_token()`（docstring 写明信任域前提 + 空串语义） | env 设/未设两态正确（见 T3.2 测试策略） |
| 1 | T1.2 | `frontend/src/api/sessionToken.ts` | 新建（D2 代码） | vitest：setter/init 双路径 |
| 2 | T2.1 | `frontend/src/api/client.ts` | 模块底部 `initSessionToken()` + pywebviewready 监听；`request()` 按 D2 合并顺序附加 header | 见 T3.1 |
| 2 | T2.2 | `frontend/src/api/client.ts` | 新增 `downloadLogs()`（D3，独立 fetch + blob，不走 request() JSON 契约） | 见 T3.1 |
| 2 | T2.3 | `frontend/src/pages/QueuePage.vue` | `:421` `<a href>` → `<button @click>`；tooltip 文案同步 | 点击触发浏览器保存对话框 |
| 3 | T3.1 | `frontend/src/api/client.test.ts` | 新增：①token set → header 带 X-Session-Token；②null → 不带；③POST + body 时 token 与 Content-Type 共存（合并顺序锁定）；④downloadLogs 200 text → 触发下载且不抛错；⑤403 → 抛错带 status | vitest 绿 |
| 3 | T3.2 | `backend/tests/test_desktop_pythonw.py` | **源码级断言**（沿用该文件既有模式，不 import desktop.py——其顶层副作用含 stdout 重绑/chdir 会污染 pytest 进程）：断言 `def get_session_token` 存在且返回 `os.environ.get(SESSION_TOKEN_ENV, "")` 形态 | pytest 绿 |
| 3 | T3.3 | `frontend/src/api/client.test.ts` | **契约一致性**：断言 sessionToken.ts 调用的 js_api 方法名与 desktop.py 源码中的 `def get_session_token` 同名（名字打错只会静默 null → 403 复现无报错） | vitest 绿 |
| 4 | T4 | `agent.md` | §10.25 补"前端已完成"备注 + §4 前端模块行（sessionToken.ts）+ 测试计数（§9.6 强制） | — |

## 3. 验收标准

1. 桌面端（run_desktop.bat）启动 → **队列页**点"下载日志" → 浏览器保存对话框弹出、文件内容完整（改前 403）
2. 浏览器 dev 模式所有端点行为不变（无 header，后端放行；vite proxy 同源不受 CORS 影响）
3. `python -m pytest` / `npm test` / `npm run build` 全绿（记录执行日实跑数字）
4. `session.token` 生命周期不变（启动生成 / 退出清理）

## 4. 兼容性与回滚

- 后端零改动 → 无兼容性问题；前端 header 附加对"env 未设"的后端是无害冗余
- 回滚：revert 前端三文件 + desktop.py 一个方法即可，与其他改动无耦合

## 5. 不做（显式排除）

- 不扩大 `require_session_token` 到更多端点（10.2 决策维持）
- 不做 403 自动重试 / token 轮换
- 不动 CORS 配置（dev 走 proxy 同源 + 后端已 `allow_headers=["*"]`）
- 不把 token 存 localStorage（刷新后由 initSessionToken 重新获取）
- 不给 WS 加 token（浏览器 WebSocket 无法设 header，已有 Origin 白名单）

## 6. 协调注意（升级为逐文件对账）

另一会话的测试加强已落地（agent.md 10.29，84→225 用例），且工作树中
`client.ts`、`QueuePage.vue`、`client.test.ts` **均已有未提交改动**。
实施前必须逐文件 `git diff` 对账（尤其 QueuePage.vue:421 下载按钮是否仍在原位），
而不是只看 `git status`。

---

## 7. 独立复审记录（2026-09-30）

复审结论：**修订后通过**。2 阻塞 + 5 重要 + 5 建议，阻塞/重要项已全部并入上文：

- 阻塞 1（日志下载是 `<a href>` 导航不走 request()）→ 并入 §0 关键事实 + D3 + T2.2/T2.3
- 阻塞 2（request() 对 200+非 JSON 抛错）→ 并入 D3"显式绕开 JSON 契约" + T3.1④⑤
- 重要 1（import desktop.py 有全局副作用，T3.2 改源码级断言）→ 已改 T3.2
- 重要 2（缺 §9.6 agent.md 步骤）→ 新增 T4
- 重要 3（§9.5"不改 API 调用"例外未声明）→ 特此声明：本 plan 的 header 注入、
  downloadLogs、QueuePage 按钮替换为任务本体要求的功能变更，构成 §9.5 默认规则的显式例外
- 重要 4（header 合并顺序）→ 已写入 D2
- 重要 5（D3 CORS 论据失实）→ 已改为 proxy 同源 + allow_headers=["*"] 的准确表述
- 建议采纳：WS/轮询不需 header 记录为 D5；信任域前提句并入 D1；exe 措辞修正并入 §0；
  契约一致性测试落地为 T3.3；对账升级为逐文件 diff 并入 §6
