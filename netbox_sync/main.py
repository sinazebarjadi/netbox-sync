"""Single-run entry logic: validate config, guard against overlapping
instances with a lockfile, run one full sync, return a cron-friendly
exit code. Scheduling is external (cron / systemd timer / Task Scheduler)."""
import atexit
import os
import time
import traceback

from netbox_sync.config import _validate_config, log
from netbox_sync.sync import run_sync

LOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "netbox-sync.lock")
LOCK_MAX_AGE_SECONDS = 24 * 3600


def _pid_alive(pid):
    """Best-effort liveness check without external dependencies. PIDs <= 0
    are invalid (on POSIX they would target a process group)."""
    if pid <= 0:
        return False
    try:
        if os.name == "posix":
            os.kill(pid, 0)
            return True
        import ctypes
        SYNCHRONIZE = 0x00100000
        ERROR_ACCESS_DENIED = 5
        WAIT_TIMEOUT = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _lock_is_stale(path):
    """A lock is stale when its recorded PID is no longer alive (a killed
    process never reaches atexit) or when it is older than
    LOCK_MAX_AGE_SECONDS. Unreadable or empty content stays conservative:
    only age can retire it."""
    try:
        age = time.time() - os.path.getmtime(path)
        if age > LOCK_MAX_AGE_SECONDS:
            return True
        with open(path) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    return not _pid_alive(pid)


def _acquire_lock(path=LOCK_FILE):
    """Create the lockfile exclusively; returns True when held. An existing
    lock is replaced when stale (dead owner or older than
    LOCK_MAX_AGE_SECONDS). The lock is released automatically at process
    exit."""
    if os.path.exists(path):
        if _lock_is_stale(path):
            log("WARN", f"Stale lock file (dead owner or >{int(LOCK_MAX_AGE_SECONDS / 3600)}h old) — removing {path}")
            try: os.remove(path)
            except OSError: pass
        else:
            return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    def _release():
        try: os.remove(path)
        except OSError: pass
    atexit.register(_release)
    return True


def main():
    try:
        _validate_config()
    except RuntimeError as exc:
        log("ERROR", str(exc))
        return 1
    if not _acquire_lock():
        log("ERROR", f"Another instance is running (lock: {LOCK_FILE}) — exiting.")
        return 1
    try:
        run_sync()
    except KeyboardInterrupt:
        log("INFO", "Aborted by user.")
        return 130
    except Exception:
        traceback.print_exc()
        return 1
    return 0
