"""Tests for the single-run entry logic (cron mode)."""
import os
import subprocess
import sys
import time

import netbox_sync.main as main_mod


def test_lock_acquire_and_conflict(tmp_path):
    lock = str(tmp_path / "x.lock")
    assert main_mod._acquire_lock(lock) is True
    assert os.path.exists(lock)
    # second attempt while held -> conflict
    assert main_mod._acquire_lock(lock) is False


def test_stale_lock_is_replaced(tmp_path):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write("999999")
    old = time.time() - main_mod.LOCK_MAX_AGE_SECONDS - 10
    os.utime(lock, (old, old))
    assert main_mod._acquire_lock(lock) is True


def test_pid_alive():
    assert main_mod._pid_alive(os.getpid()) is True
    assert main_mod._pid_alive(-5) is False
    assert main_mod._pid_alive(0) is False


def test_fresh_lock_with_dead_pid_is_reclaimed(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write(str(proc.pid))
    assert main_mod._acquire_lock(lock) is True


def test_fresh_lock_with_live_pid_blocks(tmp_path):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    assert main_mod._acquire_lock(lock) is False


def test_fresh_lock_with_garbage_content_blocks(tmp_path):
    lock = str(tmp_path / "x.lock")
    with open(lock, "w") as f:
        f.write("not-a-pid")
    assert main_mod._acquire_lock(lock) is False


def test_main_returns_1_on_config_error(monkeypatch):
    def bad():
        raise RuntimeError("missing vars")
    monkeypatch.setattr(main_mod, "_validate_config", bad)
    assert main_mod.main() == 1


def test_main_returns_1_when_locked(monkeypatch):
    monkeypatch.setattr(main_mod, "_validate_config", lambda: None)
    monkeypatch.setattr(main_mod, "_acquire_lock", lambda: False)
    assert main_mod.main() == 1


def test_main_returns_0_on_success(monkeypatch):
    monkeypatch.setattr(main_mod, "_validate_config", lambda: None)
    monkeypatch.setattr(main_mod, "_acquire_lock", lambda: True)
    called = []
    monkeypatch.setattr(main_mod, "run_sync", lambda: called.append(1))
    assert main_mod.main() == 0
    assert called == [1]


def test_main_returns_1_on_unexpected_error(monkeypatch):
    monkeypatch.setattr(main_mod, "_validate_config", lambda: None)
    monkeypatch.setattr(main_mod, "_acquire_lock", lambda: True)
    def boom():
        raise ValueError("x")
    monkeypatch.setattr(main_mod, "run_sync", boom)
    assert main_mod.main() == 1
