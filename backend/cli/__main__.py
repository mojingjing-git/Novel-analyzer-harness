"""支持 ``python -m backend.cli`` 执行入口（保留 ``__main__`` 守卫以兼容自动导入冒烟检查）。"""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
