"""API 鉴权

桌面端 session.token 鉴权：
- desktop.py 启动时生成 32 字节 token，写到 session.token（0600 权限）
- 同时设置环境变量 NOVEL_ANALYZER_SESSION_TOKEN
- 后端中间件读环境变量，所有受保护端点用 Depends(require_session_token)
- 客户端必须带 X-Session-Token: <token> header
- 开发模式（uvicorn 直接跑，未设环境变量）→ 跳过鉴权
"""
import hmac
import os

from fastapi import Header, HTTPException, status


_ENV_VAR = "NOVEL_ANALYZER_SESSION_TOKEN"


def require_session_token(
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
) -> None:
    """FastAPI Depends：验证 X-Session-Token header。

    行为：
    - 环境变量未设置（开发模式 / 单机测试）→ 放行（不报错）
    - 环境变量已设置 + token 不匹配 → 403
    - 失败模式：使用 hmac.compare_digest 防时序攻击

    注意：桌面端零摩擦依赖前端 client.ts 在所有 fetch
    拦截加 X-Session-Token header。**当前实现仅保护后端，桌面端
    pywebview 调受保护端点会被 403**——这是已知折中，前端改动
    尚未落地。
    """
    expected = os.environ.get(_ENV_VAR)
    if not expected:
        # 开发模式 / 测试环境：未启用 session token
        return
    if not x_session_token or not hmac.compare_digest(x_session_token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session token required. Provide X-Session-Token header.",
        )
