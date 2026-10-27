"""Small native safeguards for a collector running on a user's computer."""
from __future__ import annotations

import ctypes
import os
import shutil
from pathlib import Path


class CollectorBusy(RuntimeError):
    pass


class ProcessLock:
    """An OS lock is released after a crash; a leftover file is not a stale lock."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b" ")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise CollectorBusy("Drugi HRPres postupak već radi. Otvori STANJE ili PAUZIRAJ.") from exc
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


class KeepAwake:
    """Prevent automatic Windows sleep on this thread, without changing the power plan."""

    def __init__(self):
        self.active = False

    def __enter__(self):
        if os.name == "nt":
            self.function = ctypes.windll.kernel32.SetThreadExecutionState
            self.function.argtypes = [ctypes.c_uint]
            self.function.restype = ctypes.c_uint
            self.active = bool(self.function(0x80000001))
        return self

    def __exit__(self, *_):
        if self.active:
            self.function(0x80000000)
            self.active = False


def require_free_space(directory: Path, min_free_gib: float):
    if min_free_gib < 0:
        raise ValueError("Rezerva slobodnog prostora ne smije biti negativna.")
    free = shutil.disk_usage(directory).free
    if free < min_free_gib * 1024**3:
        raise RuntimeError(f"Premalo prostora: {free / 1024**3:.1f} GiB slobodno; "
                           f"rezerva je {min_free_gib:g} GiB. Napredak je sačuvan.")
