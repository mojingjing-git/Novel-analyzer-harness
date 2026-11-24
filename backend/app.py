"""
FastAPI 应用工厂

开发模式：uvicorn backend.app:app --reload （配合 vite dev 前端）
发布模式：托管 frontend/dist 静态文件，pywebview 指向本服务
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from backend.config.paths import ensure_runtime_dir, get_bundle_dir, get_runtime_dir
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
    ws,
)
from backend.progress_hub import get_hub, install_log_forwarder
from backend.core.llm_failure_logger import redact

logger = logging.getLogger(__name__)

# 运行时数据根（打包 exe 落 EXE 同级；源码态默认落 runtime/）与只读资源根
BUNDLE_DIR = get_bundle_dir()
PROJECT_ROOT = ensure_runtime_dir()
LOG_FILE = PROJECT_ROOT / "analyzer.log"
FRONTEND_DIST = BUNDLE_DIR / "frontend" / "dist"

# 配置日志文件轮转（10MB x 3）
_file_handler = RotatingFileHandler(
    str(LOG_FILE), encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))


class _SensitiveFieldsFilter(logging.Filter):
    """对所有落到 analyzer.log 的 record 做敏感字段二次过滤。

    - 复用 `backend.core.llm_failure_logger.redact`
    - 处理 `record.msg`（f-string 已格式化）和 `record.args`（% 格式化参数）
    - **fail-open**：filter 内部任何异常都放行原文（不丢日志）
    - 永远 return True（不丢 record，只改写 msg/args 内容）
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

from backend.common.dep_health import check_dependency_health as _check_dependency_health  # noqa: E402


def create_app() -> FastAPI:
    # @app.on_event("startup") 已被 FastAPI 弃用（DeprecationWarning），
    # 因此用 lifespan 上下文管理器（行为等价）。
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

        # 启动期自动扫描工作区：
        # 扫描是同步 IO（网络盘多书时可能耗时数秒~数十秒），必须在事件循环之外
        # 执行，否则整个事件循环被冻结（UI 表现为"点一下没反应/进度卡住"）。
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

        # 退出时同步刷盘持久账本（运行期走节流异步写；这里兜底批末数据）
        try:
            from backend.services.usage_ledger import get_usage_ledger
            get_usage_ledger().save_sync()
        except Exception as e:
            logger.warning(f"用量账本退出刷盘失败: {e}")

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
    app.include_router(routes_splitter.router)
    app.include_router(routes_workspace.router)
    app.include_router(routes_prompt.router)
    app.include_router(routes_aggregate.router)
    app.include_router(routes_foreshadow.router)

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
        app.mount("/", SPAStaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
        logger.info("已托管前端静态文件: %s", FRONTEND_DIST)

    return app


def _should_spa_fallback(path: str, method: str) -> bool:
    """判定静态资源 404 时是否应回退到前端 SPA index.html。"""
    if method not in ("GET", "HEAD"):
        return False
    norm = path.replace("\\", "/").lstrip("/")
    if norm.startswith(("api/", "ws/", "assets/")) or norm in ("api", "ws", "assets"):
        return False
    return "." not in Path(norm).name


class SPAStaticFiles(StaticFiles):
    """支持前端 History 模式子路由回退到 index.html 的静态文件服务。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and _should_spa_fallback(path, scope.get("method", "GET")):
                return await super().get_response("index.html", scope)
            raise


app = create_app()
