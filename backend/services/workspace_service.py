"""
工作区管理服务
- 列出 workspace/ 中的小说
- 列出 分析结果/ 中的归档
- 归档小说（移植原版 workspace_manager.py 逻辑）
- 删除归档
- 将小说目录移入系统回收站
"""
import logging
import platform
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from .queue_service import get_service

logger = logging.getLogger(__name__)


def _is_within(child: Path, parent: Path) -> bool:
    """判断 child 解析后是否仍在 parent 目录树内（防路径穿越 ../）"""
    import os
    try:
        common = os.path.commonpath([str(child.resolve()), str(parent.resolve())])
        return common == str(parent.resolve())
    except (ValueError, OSError):
        return False


def _safe_join(parent: Path, name: str) -> Optional[Path]:
    """安全拼接子路径：name 含 ../ 等越界字符时返回 None。resolve 后必须仍在 parent 内。"""
    if not name:
        return None
    try:
        parent_res = parent.resolve()
        target = (parent_res / name).resolve()
        if target == parent_res or not _is_within(target, parent_res):
            return None
        return target
    except (ValueError, OSError):
        return None


def _get_workspace_path() -> Path:
    """获取 workspace 目录"""
    return get_service().workspace_path


def _get_archive_root() -> Path:
    """获取 分析结果/ 归档根目录（在 workspace 内，与 queue_service 归档位置一致）"""
    ws = _get_workspace_path()
    return ws / "分析结果"


def list_workspace_novels() -> List[Dict]:
    """
    扫描 workspace/ 下所有含 blocks/*.txt 的小说目录。
    返回每本：name, blocks_count, result_count, dir_size, dir_mtime
    """
    ws = _get_workspace_path()
    novels = []
    if not ws.exists():
        return novels

    for subdir in sorted(ws.iterdir()):
        if not subdir.is_dir():
            continue
        blocks_dir = subdir / "blocks"
        if not (blocks_dir.exists() and any(blocks_dir.glob("*.txt"))):
            continue

        # blocks 数
        txt_files = sorted(blocks_dir.glob("*.txt"))
        blocks_count = len(txt_files)

        # result 数
        output_dir = subdir / "output"
        result_count = len(list(output_dir.glob("*_result.json"))) if output_dir.exists() else 0

        # 目录大小
        total_bytes = sum(f.stat().st_size for f in subdir.rglob("*") if f.is_file())

        # 最后一次修改时间
        mtime = max((f.stat().st_mtime for f in subdir.rglob("*") if f.is_file()), default=0)

        novels.append({
            "name": subdir.name,
            "blocks_count": blocks_count,
            "result_count": result_count,
            "dir_size": total_bytes,
            "dir_mtime": mtime,
        })

    return novels


def list_archives() -> List[Dict]:
    """扫描 分析结果/ 下的归档，返回列表（最新的在前）"""
    archive_root = _get_archive_root()
    archives = []
    if not archive_root.exists():
        return archives

    for subdir in sorted(archive_root.iterdir(), reverse=True):
        if not subdir.is_dir():
            continue
        files = [f for f in subdir.rglob("*") if f.is_file()]
        total_size = sum(f.stat().st_size for f in files)
        archives.append({
            "name": subdir.name,
            "file_count": len(files),
            "total_size": total_size,
            "dir_mtime": subdir.stat().st_mtime,
        })

    return archives


def detect_book_name(novel_dir: Path) -> str:
    """从小说目录推断书名（移植 workspace_manager.py）

    优先级：目录名（最可靠）> style.md 中的书名正则 > blocks 下非数字 txt（排除 split_report 等报告）。
    旧逻辑只扫 blocks 非数字 txt，而 splitter 每次都会写 blocks/split_report.txt，
    导致归档名恒为「split_report-日期」，去重/覆盖失效。
    """
    # 1) 目录名优先（如 workspace/《书名》/ 或 workspace/书名/）
    dir_name = novel_dir.name.strip()
    if dir_name and dir_name != ".":
        cand = dir_name.replace("_分析报告", "").replace("_分析", "").strip()
        if cand:
            return cand

    # 2) style.md 中的书名
    style_md = novel_dir / "style.md"
    if style_md.exists():
        try:
            text = style_md.read_text(encoding="utf-8")
            m = re.search(r'基于《(.+?)》原文分析', text)
            if m:
                name = m.group(1).strip()
                if name:
                    return name
        except Exception:
            pass

    # 3) blocks 下非数字 txt（排除报告文件）
    blocks = novel_dir / "blocks"
    if blocks.exists():
        for f in blocks.glob("*.txt"):
            if f.name.lower().startswith("split_report"):
                continue
            if not re.match(r'^\d+\.txt$', f.name):
                name = f.stem.replace("_分析报告", "").replace("_分析", "").strip()
                if name:
                    return name

    return novel_dir.name


def find_existing_archive(book_name: str) -> Optional[Path]:
    """查找同名小说的已有归档"""
    archive_root = _get_archive_root()
    if not archive_root.exists():
        return None
    safe_name = re.escape(book_name)
    pattern = re.compile(rf'^{safe_name}(?:-\d{{8}}-\d{{4}})?$')
    for archive in archive_root.iterdir():
        if archive.is_dir() and pattern.match(archive.name):
            return archive
    return None


def _generate_archive_name(book_name: str) -> str:
    """生成带时间戳的归档名"""
    now = datetime.now()
    return f"{book_name}-{now.strftime('%Y%m%d-%H%M')}"


def archive_novel(novel_name: str, mode: str = "rename") -> Dict:
    """
    归档单个小说目录。
    mode: skip(已存在则跳过), overwrite(覆盖), rename(重命名-默认)
    返回 {ok, archive_name?, error?}
    """
    ws = _get_workspace_path()
    novel_dir = _safe_join(ws, novel_name)
    if novel_dir is None:
        return {"ok": False, "error": f"非法小说名（可能越界）: {novel_name}"}

    if not novel_dir.exists():
        return {"ok": False, "error": f"目录不存在: {novel_name}"}

    book_name = detect_book_name(novel_dir)
    existing = find_existing_archive(book_name)

    if existing:
        if mode == "skip":
            return {"ok": False, "error": f"已存在同名归档: {existing.name}", "skipped": True}
        elif mode == "overwrite":
            try:
                shutil.rmtree(existing)
                logger.info("已删除旧归档: %s", existing)
            except Exception as e:
                return {"ok": False, "error": f"删除旧归档失败: {e}"}
        # mode == "rename" → 继续使用新时间戳
    archive_root = _get_archive_root()

    # 第一步：在 workspace 内重命名（加时间戳）
    archive_name = _generate_archive_name(book_name)
    temp_path = novel_dir.parent / archive_name
    try:
        novel_dir.rename(temp_path)
    except OSError as e:
        return {"ok": False, "error": f"重命名失败: {e}"}

    # 第二步：移动到 分析结果/
    archive_dest = archive_root / archive_name
    archive_root.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(temp_path), str(archive_dest))
    except Exception as e:
        # 移动失败，回滚；回滚自身失败必须暴露滞留路径（P2：此前静默吞掉，
        # 目录滞留时间戳临时名，用户按原名重试报“不存在”，看似书丢了）
        roll_err = ""
        try:
            temp_path.rename(novel_dir)
        except Exception as re_err:
            roll_err = f"；回滚亦失败，目录滞留为工作区内「{temp_path.name}」，请手工改名恢复"
            logger.error("归档回滚失败: %s -> %s: %s", temp_path, novel_dir, re_err)
        return {"ok": False, "error": f"移动失败: {e}{roll_err}"}

    logger.info("已归档《%s》 → %s", book_name, archive_dest)
    return {"ok": True, "archive_name": archive_name}


def archive_all() -> Dict:
    """归档全部小说（已存在的默认跳过），返回统计"""
    novels = list_workspace_novels()
    archived = []
    skipped = []
    failed = []

    for n in novels:
        result = archive_novel(n["name"], mode="skip")
        if result.get("ok"):
            archived.append(n["name"])
        elif result.get("skipped"):
            skipped.append(n["name"])
        else:
            failed.append({"name": n["name"], "error": result.get("error", "未知错误")})

    return {
        "ok": True,
        "archived": archived,
        "skipped": skipped,
        "failed": failed,
    }


def delete_archive(archive_name: str) -> Dict:
    """删除指定归档"""
    archive_root = _get_archive_root()
    target = _safe_join(archive_root, archive_name)
    if target is None:
        return {"ok": False, "error": f"非法归档名（可能越界）: {archive_name}"}

    if not target.exists():
        return {"ok": False, "error": f"归档不存在: {archive_name}"}
    if not target.is_dir():
        return {"ok": False, "error": f"不是目录: {archive_name}"}

    try:
        shutil.rmtree(target)
        logger.info("已删除归档: %s", archive_name)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"删除失败: {e}"}


def _move_to_trash(path: Path) -> None:
    """将指定路径移入系统回收站（可恢复）。Windows 用 SHFileOperationW；macOS 用 Finder；Linux 回退 gio/trash-put。"""
    target = Path(path).resolve()
    if not target.exists():
        return

    system = platform.system()
    if system == "Windows":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004
        FOF_NOERRORUI = 0x0400
        flags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

        shell32 = ctypes.windll.shell32
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = wintypes.INT

        # pFrom 需要以双空字符结尾
        p_from = str(target) + "\0\0"

        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = p_from
        op.pTo = None
        op.fFlags = flags
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None

        result = shell32.SHFileOperationW(ctypes.byref(op))
        # P2：aborted 也算失败——用户/系统中止时目标仍在原处，若当成功放行，
        # 队列已删而磁盘未删（delete_book 出现幽灵条目）
        if result != 0 or op.fAnyOperationsAborted:
            if op.fAnyOperationsAborted:
                logger.error("SHFileOperationW 被中止（fAnyOperationsAborted=TRUE），目标未移入回收站")
            raise RuntimeError(f"SHFileOperationW 失败，错误码 {result}")
    elif system == "Darwin":
        subprocess.run(
            ["osascript", "-e", f'tell app "Finder" to delete POSIX file "{target}"'],
            check=True,
        )
    else:
        for cmd in (["gio", "trash", str(target)], ["trash-put", str(target)]):
            try:
                subprocess.run(cmd, check=True)
                return
            except Exception:
                continue
        raise RuntimeError("无法调用系统回收站工具（gio trash / trash-put）")


def delete_novel_to_trash(novel_name: str, novel_path: Optional[Path] = None) -> Dict:
    """将小说目录移入系统回收站，并返回操作结果。

    novel_path：显式目录路径。队列项可来自 /api/queue/scan 扫描的任意 base_dir，
    其真实目录不在工作区之内；此时必须按显式路径删除——按名字重新解析回工作区
    会误删工作区同名书，或在无同名时报"目录不存在"（P1 2026-08-24）。
    缺省时保持旧行为：novel_name 解析到工作区内。"""
    ws = _get_workspace_path()
    if novel_path is not None:
        novel_dir = Path(novel_path)
    else:
        resolved = _safe_join(ws, novel_name)
        if resolved is None:
            return {"ok": False, "error": f"非法小说名（可能越界）: {novel_name}"}
        novel_dir = resolved

    if not novel_dir.exists():
        return {"ok": False, "error": f"目录不存在: {novel_dir.name}"}
    if not novel_dir.is_dir():
        return {"ok": False, "error": f"不是目录: {novel_dir.name}"}

    try:
        _move_to_trash(novel_dir)
        logger.info("已将小说目录移入回收站: %s", novel_dir)
        return {"ok": True}
    except Exception as e:
        logger.error("移入回收站失败: %s, %s", novel_dir, e)
        return {"ok": False, "error": f"移入回收站失败: {e}"}
