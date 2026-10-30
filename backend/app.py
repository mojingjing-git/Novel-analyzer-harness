"""
FastAPI 应用工厂

开发模式：uvicorn backend.app:app --reload （配合 vite dev 前端）
发布模式：托管 frontend/dist 静态文件，pywebview 指向本服务
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api import (
    routes_analysis,
    routes_books,
    routes_prompt,
    routes_settings,
    routes_splitter,
    routes_summary,
    routes_viz,
    routes_workspace,
    routes_aggregate,
    routes_foreshadow,
    routes_vnext,
    routes_location_normalization,
    ws,
)
from backend.progress_hub import get_hub, install_log_forwarder
from backend.core.llm_failure_logger import redact

logger = logging.getLogger(__name__)

# 项目根目录（用户数据落 EXE 同级）+ BUNDLE_DIR（只读资源，dist 嵌入位置）
# 设计原则（v0.2.0 exe）：所有用户数据（workspace/ config/ queue_state/ log）
# 都在 EXE 同级目录，不在 %APPDATA% 也不在 _MEIPASS，避免 C 盘残留 + 卸载残留。
if getattr(sys, "frozen", False):
    EXE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", EXE_DIR))
    PROJECT_ROOT = EXE_DIR
    LOG_FILE = PROJECT_ROOT / "analyzer.log"
else:
    # NOVEL_ROOT 允许从“本地副本”启动进程（WebView2 需本地路径），
    # 同时让数据/日志仍落在共享盘真实项目根。
    _novel_root = os.environ.get("NOVEL_ROOT")
    PROJECT_ROOT = Path(_novel_root).resolve() if _novel_root else Path(__file__).resolve().parent.parent
    BUNDLE_DIR = PROJECT_ROOT
    LOG_FILE = PROJECT_ROOT / "analyzer.log"
FRONTEND_DIST = BUNDLE_DIR / "frontend" / "dist"

# 配置日志文件轮转（10MB x 3）
_file_handler = RotatingFileHandler(
    str(LOG_FILE), encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))


class _SensitiveFieldsFilter(logging.Filter):
    """对所有落到 analyzer.log 的 record 做敏感字段二次过滤（PR-1 修复，2026-09-10）。

    - 复用 `backend.core.llm_failure_logger.redact`（与 PR-1 步骤 2 同源）
    - 处理 `record.msg`（f-string 已格式化）和 `record.args`（% 格式化参数）
    - **fail-open**：filter 内部任何异常都放行原文（不丢日志）
    - 永远 return True（不丢 record，只改写 msg/args 内容）

    Refs: _plan_v3.md PR-1 步骤 3
    """
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a
                    for a in record.args
                )
        except Exception:
            # fail-open：filter 失败时放行原文，绝不丢日志
            pass
        return True


_file_handler.addFilter(_SensitiveFieldsFilter())
logging.getLogger().addHandler(_file_handler)


def _check_dependency_health() -> dict:
    """启动时检查关键依赖是否真的能 import

    真实事故复现（analyzer.log:451-457, 2026-08-26 06:46-6:47）：
    json_repair 未安装 8 次, 9 级容错链到策略 7 静默跳过, 解析降级
    但未崩溃。本函数目标: 把这种"未装"显式化, 避免再次发生。

    Returns:
        dict: {
            "status": "ok" | "degraded" | "critical",
            "deps": {label: {"status": "ok"|"missing", "version": str, "error": str}},
            "missing": [str, ...],          # 所有缺失依赖名
            "critical_missing": [str, ...],  # 标记为 critical 的缺失依赖
        }
    """
    # (label, module_name, critical) — critical=True 表示该依赖缺失会显著降级功能
    deps = [
        ("json5", "json5", True),
        ("json_repair", "json_repair", True),
        ("openai", "openai", True),
        ("anthropic", "anthropic", False),  # 可选, 仅使用 Anthropic provider 时需要
        ("openpyxl", "openpyxl", True),
        ("websockets", "websockets", True),
    ]
    health = {"status": "ok", "deps": {}, "missing": [], "critical_missing": []}
    for label, module, critical in deps:
        try:
            m = __import__(module)
            health["deps"][label] = {
                "status": "ok",
                "version": getattr(m, "__version__", "unknown"),
            }
        except ImportError as e:
            health["deps"][label] = {"status": "missing", "error": str(e)}
            health["missing"].append(label)
            if critical:
                health["critical_missing"].append(label)
    if health["critical_missing"]:
        health["status"] = "critical"
    elif health["missing"]:
        health["status"] = "degraded"
    return health


def create_app() -> FastAPI:
    # P2-12：@app.on_event("startup") 已被 FastAPI 弃用（DeprecationWarning），
    # 迁移到 lifespan 上下文管理器（行为等价）。
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        install_log_forwarder()  # 内部使用 asyncio.create_task，需在事件循环中调用

        # 把未检索的 asyncio 任务异常写入日志（默认只打到 stderr，桌面端会丢失）
        try:
            loop = asyncio.get_running_loop()
            _orig_handler = loop.get_exception_handler()

            def _exc_handler(loop, context):
                msg = context.get("message", "Unhandled asyncio exception")
                exc = context.get("exception")
                logger.error(f"[asyncio] {msg}", exc_info=exc)
                if _orig_handler:
                    _orig_handler(loop, context)

            loop.set_exception_handler(_exc_handler)
        except Exception as e:
            logger.debug(f"设置 asyncio 异常处理器失败: {e}")

        # 启动期自动扫描工作区（H1 修复）：
        # 原实现在 AnalysisService.__init__ 里同步扫描，而 __init__ 由首个触发
        # get_service() 的 async 请求在事件循环内调用，导致整个事件循环被同步 IO 冻结
        # （网络盘多书时卡数秒~数十秒，UI 表现为"点一下没反应/进度卡住"）。
        # 此处放到 lifespan 启动阶段、用 asyncio.to_thread 离线程执行，请求到来前队列已就绪，
        # 且扫描期间事件循环不被阻塞。扫描异常在此被吞掉并记录，不影响服务启动。
        try:
            from backend.services.queue_service import get_service
            svc = get_service()
            await asyncio.to_thread(svc._auto_scan_workspace)
            logger.info("启动期工作区扫描完成")
        except Exception as e:
            logger.warning(f"启动期工作区扫描失败（不影响启动）: {e}")

        # 启动期依赖健康检查（缺失时通过 WS 推 ERROR 到 GUI 简化日志 + 落盘 analyzer.log）
        try:
            dep_health = _check_dependency_health()
            if dep_health["status"] != "ok":
                missing_list = dep_health["critical_missing"] or dep_health["missing"]
                msg = (
                    f"⚠️ 关键依赖缺失: {', '.join(missing_list)} "
                    f"(status={dep_health['status']}, 9 级容错链可能降级)"
                )
                hub = get_hub()
                # 走业务事件路径（hub.log 内部 hardcode source="business"）才能在 GUI 简化日志视图显示
                await hub.log(msg, level="error", category="system")
                # 同步落盘 analyzer.log（事后查问题用）
                logger.error(f"依赖健康检查异常: {msg}")
            else:
                logger.info(
                    f"依赖健康检查通过: {len(dep_health['deps'])} 个全部就绪 "
                    f"(json_repair={dep_health['deps'].get('json_repair', {}).get('version', 'unknown')})"
                )
        except Exception as e:
            logger.warning(f"依赖健康检查失败（不影响启动）: {e}")
        yield

    app = FastAPI(title="Novel Analyzer Web", version="0.1.0", lifespan=lifespan)

    # 开发期前端跑在 vite dev server（不同端口），需要 CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 桌面应用缓存策略：前端是本地实时构建产物，必须禁用浏览器缓存。
    # 否则 WebView2 会按 Starlette 默认 Cache-Control: max-age=3600 缓存 index.html，
    # 重建 dist 后用户仍看到旧效果（"改了但没生效"的典型假象）。
    @app.middleware("http")
    async def disable_frontend_cache(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            # Starlette 的 MutableHeaders 没有 .pop() 方法，用 del 守卫式删除
            if "etag" in response.headers:
                del response.headers["etag"]
            if "last-modified" in response.headers:
                del response.headers["last-modified"]
        return response

    # WebSocket 进度通道
    app.include_router(ws.router)

    # REST 路由
    app.include_router(routes_settings.router)
    app.include_router(routes_analysis.router)
    app.include_router(routes_books.router)
    app.include_router(routes_summary.router)
    app.include_router(routes_viz.router)
    app.include_router(routes_location_normalization.router)
    app.include_router(routes_splitter.router)
    app.include_router(routes_workspace.router)
    app.include_router(routes_prompt.router)
    app.include_router(routes_aggregate.router)
    app.include_router(routes_foreshadow.router)
    app.include_router(routes_vnext.router)

    @app.get("/api/health")
    async def health():
        # 合并依赖健康检查：缺关键依赖时把 status 降级为 critical/degraded，
        # 让运维/QA 第一时间发现，而不是只看 analyzer.log 或 GUI 简化日志。
        dep_health = _check_dependency_health()
        return {
            "status": dep_health["status"] if dep_health["status"] != "ok" else "ok",
            "app": "novel-analyzer-web",
            "deps": dep_health["deps"],
            "missing": dep_health["missing"],
            "critical_missing": dep_health["critical_missing"],
        }

    # 发布模式：托管前端构建产物（必须放在最后，避免吞掉 /api 路由）
    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
        logger.info("已托管前端静态文件: %s", FRONTEND_DIST)

    return app


app = create_app()
