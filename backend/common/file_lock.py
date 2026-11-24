"""跨进程书目文件锁（纯标准库实现，Windows 使用 msvcrt 非阻塞字节锁，POSIX 使用 flock）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class BookLockError(RuntimeError):
    """无法获取跨进程书目文件锁（通常由另一进程占用）。"""


class BookFileLock:
    """针对单本书目目录（``book_dir / ".book.lock"``）的跨进程非阻塞独占锁。

    设计约束：
    - 使用 ``os.O_CREAT | os.O_RDWR`` 打开（绝不截断已有锁文件）；
    - Windows 上在每次锁定与解锁前均显式 ``lseek(fd, 0, SEEK_SET)`` 并仅锁定字节 0，
      使用 ``msvcrt.LK_NBLCK``；
    - 释放时先解锁再关闭描述符，且不删除锁文件以规避双 inode 与共享冲突。
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self._fd: Optional[int] = None
        self._locked: bool = False

    def acquire(self) -> "BookFileLock":
        if self._locked:
            return self
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self.lock_path), flags, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise BookLockError(f"书目正在被其他进程操作，无法获取锁: {self.lock_path}") from exc

        self._fd = fd
        self._locked = True
        return self

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        was_locked = self._locked
        self._locked = False
        try:
            if was_locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "BookFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
