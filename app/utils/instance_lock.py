"""Prevent running multiple bot instances simultaneously."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InstanceLock:
    """Cross-platform single-instance lock using a lock file."""

    def __init__(self, lock_path: Path) -> None:
        self._path = lock_path
        self._file = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if self._path.exists():
            try:
                old_pid = int(self._path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                old_pid = 0
            if old_pid and _is_process_running(old_pid):
                raise RuntimeError(
                    f"Бот уже запущен (PID {old_pid}). "
                    "Остановите другие процессы `python -m app.main`."
                )
            self._path.unlink(missing_ok=True)

        self._file = open(self._path, "w", encoding="utf-8")

        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(
                    "Бот уже запущен. Остановите другие процессы `python -m app.main`."
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    "Бот уже запущен. Остановите другие процессы `python -m app.main`."
                ) from exc

        self._file.write(str(os.getpid()))
        self._file.flush()

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self._path.unlink(missing_ok=True)
