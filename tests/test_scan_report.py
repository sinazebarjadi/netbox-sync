"""Tests for the scan/processing failure reporting and summary output."""
import concurrent.futures
import socket

import pytest
import requests
import requests.exceptions

import netbox_sync.report as report
import netbox_sync.scanner as scanner
import netbox_sync.collectors.hikvision as hikvision
from netbox_sync.collectors.hikvision import probe_hikvision


# ── classification ───────────────────────────────────────────────────────────

def test_classify_timeout():
    assert "timeout" in report.classify_error(TimeoutError("timed out")).lower()


def test_classify_connection_refused():
    msg = "[WinError 10061] No connection could be made because the target machine actively refused it"
    assert "refused" in report.classify_error(ConnectionRefusedError(msg)).lower()


def test_classify_http_401():
    resp = requests.Response()
    resp.status_code = 401
    exc = requests.exceptions.HTTPError(response=resp)
    assert "authentication failure" in report.classify_error(exc)


def test_classify_http_403():
    resp = requests.Response()
    resp.status_code = 403
    exc = requests.exceptions.HTTPError(response=resp)
    assert "HTTP 403" in report.classify_error(exc)


def test_classify_server_error():
    resp = requests.Response()
    resp.status_code = 503
    exc = requests.exceptions.HTTPError(response=resp)
    assert "HTTP 503" in report.classify_error(exc)


# ── probe logging (hikvision port-closed case) ──────────────────────────────

def test_probe_logs_port_closed(monkeypatch):
    monkeypatch.setattr("netbox_sync.report._failures", [])
    monkeypatch.setattr(hikvision, "is_port_open",
                        lambda *a, **k: False)
    assert probe_hikvision("192.0.2.9") is None
    assert report._failures == [
        ("Hikvision NVR", "192.0.2.9", "unreachable", "port 80 closed or timed out")
    ]


def test_probe_logs_auth_failure(monkeypatch):
    monkeypatch.setattr("netbox_sync.report._failures", [])
    monkeypatch.setattr(hikvision, "is_port_open",
                        lambda *a, **k: True)

    resp = requests.Response()
    resp.status_code = 401
    fake_exc = requests.exceptions.HTTPError(response=resp)

    class FakeSession:
        def __init__(self, *a, **k): pass
        def get(self, path): raise fake_exc
        def logout(self): pass

    monkeypatch.setattr(hikvision, "HikvisionSession", FakeSession)
    assert probe_hikvision("192.0.2.10", retries=1) is None
    assert any(f[0] == "Hikvision NVR" and f[1] == "192.0.2.10" and "401" in f[3]
               for f in report._failures)


# ── scanner drain pool exception logging ────────────────────────────────────

def test_drain_pool_records_failed_probe(monkeypatch):
    monkeypatch.setattr("netbox_sync.report._failures", [])
    on_hit = lambda r: None
    on_hit.__family__ = "Server"

    def broken_probe(ip):
        raise socket.timeout("timed out")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        scanner._drain_pool(ex, {ex.submit(broken_probe, "192.0.2.55"): "192.0.2.55"},
                            on_hit)

    assert any("timeout" in f[3] for f in report._failures)
