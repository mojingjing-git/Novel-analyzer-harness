# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Novel Analyzer v0.2.0 single-file Windows exe.

Build:
    pyinstaller --noconfirm desktop.spec

Output:
    dist/novel-analyzer.exe  (~65-80 MB)

策略（v0.2.0）:
- --onefile 单文件输出
- 不用 UPX（杀软误报 +30%）
- excludes 大量未用 stdlib/三方库
- hiddenimports 覆盖 pywebview 6.x / pythonnet 3.x / uvicorn 隐式依赖
- datas: frontend/dist（嵌入） + config.example.json + _welcome_workspace.md

设计原则（用户偏好，2026-09-12）:
- 所有用户数据（workspace/ config/ log/ queue_state）落 EXE 同级
- 不写 %APPDATA% 不写 _MEIPASS（避免 C 盘残留 + 卸载残留）
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# 嵌入资源
datas = [
    ('frontend/dist', 'frontend/dist'),
    ('backend/data/models_dev.json', 'backend/data'),
    ('config.example.json', '.'),
    ('_welcome_workspace.md', '.'),
]

# 隐式 import（PyInstaller 静态分析抓不到 + pywebview 6.x / pythonnet 3.x 需要）
hiddenimports = [
    # pywebview 6.x 子模块
    'webview.platforms.edgechromium',
    'webview.platforms.winforms',
    'webview.lib.runtimes',
    # pythonnet 3.x（pywebview 依赖）
    'clr_loader',
    'pythonnet',
    # uvicorn 隐式子模块
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    # websockets 隐式
    'websockets.asyncio',
    'websockets.legacy',
    # fastapi 隐式
    'fastapi.routing',
    'fastapi.middleware',
    'fastapi.middleware.cors',
]

# 自动收集 backend 全部业务子模块（排除 tests）
# v0.2.0 关键修复：PyInstaller 静态分析抓不到 uvicorn.run("backend.app:app") 的字符串 import
# 用 collect_submodules 自动发现整个 backend 包的所有子模块
_backend_submods = [m for m in collect_submodules('backend') if '.tests' not in m]
hiddenimports += _backend_submods
print(f"[spec] auto-collected backend submodules: {len(_backend_submods)}")

# 排除未用的 stdlib/三方库（减体积）
excludes = [
    # 图形/科学栈（项目不用）
    'tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy', 'PIL',
    'Pillow', 'Image', 'cv2', 'skimage',
    # IDE / 工具（项目不用）
    'IPython', 'jupyter', 'notebook',
    'black', 'flake8', 'mypy', 'pylint', 'autopep8', 'yapf',
    'pytest', 'pytest_asyncio',  # 运行时不需要（测试场景用 .venv）
    # 其他未用
    'cryptography.hazmat.bindings._rust',  # 加密库可选优化（破坏性，需测）
    'sphinx', 'docutils', 'pydoc',
    'setuptools', 'pkg_resources',  # PyInstaller 自带
    'Cython', 'pypy',
]

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='novel-analyzer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                                # 不用 UPX（杀软 +30% 误报）
    upx_exclude=[],
    runtime_tmpdir=None,                      # 默认 %TEMP%/_MEIxxxxx
    console=False,                            # 双击无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,                         # 当前 Python 架构
    codesign_identity=None,                   # 无证书
    entitlements_file=None,
    icon=None,                                # 默认 Python 图标（v0.2.0 不做图标）
)
