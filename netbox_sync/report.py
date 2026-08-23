"""Per-run report of device scan/processing failures.

Probes and collectors record categorized failures here (thread-safe); at the
end of a run, :func:`print_summary` emits a compact, grouped summary so the
operator can immediately see which devices need attention and why.
"""
import socket
import threading
from collections import defaultdict

from netbox_sync.config import log

_lock = threading.Lock()
_failures = []  # list of (family, ip, category, reason)


def classify_error(exc):
    """Map a low-level exception to a short, operator-readable reason."""
    if exc is None:
        return "unknown error"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        if status == 401:
            return "authentication failure (HTTP 401)"
        if status == 403:
            return "forbidden / insufficient permissions (HTTP 403)"
        if status == 408:
            return "HTTP timeout (HTTP 408)"
        if status >= 500:
            return f"device API error (HTTP {status})"
        return f"HTTP {status} from device API"
    mod = type(exc).__module__ or ""
    if isinstance(exc, socket.timeout) or "Timeout" in type(exc).__name__ \
            or "timed out" in str(exc).lower():
        return "timeout"
    if isinstance(exc, (ConnectionRefusedError, socket.error)) \
            and "refused" in str(exc).lower():
        return "connection refused"
    if "requests.exceptions" in mod or mod.startswith("requests"):
        name = type(exc).__name__
        if "SSLError" in name:
            return f"TLS/SSL error: {exc}"
        if "ConnectionError" in name:
            return f"connection error: {exc}"
        return f"{name}: {exc}"
    if isinstance(exc, PermissionError):
        return f"permission denied: {exc}"
    return f"{type(exc).__name__}: {exc}"


def record_probe_failure(family, ip, category, reason):
    """Remember the final probe failure for one device. Intermediate retry
    attempts are intentionally not recorded — only the outcome matters."""
    with _lock:
        _failures[:] = [f for f in _failures
                        if not (f[0] == family and f[1] == ip)]
        _failures.append((family, ip, category, str(reason)))


def record_processing_failure(family, ip, reason):
    """Record a failure that happened after a successful probe (collection,
    NetBox ensure, ...). Keeps any earlier probe failure for context."""
    with _lock:
        if not any(f[0] == family and f[1] == ip and f[2] == "no data"
                   for f in _failures):
            _failures.append((family, ip, "no data", str(reason)))


def clear():
    with _lock:
        _failures.clear()


def _grouped():
    by_key = defaultdict(list)
    with _lock:
        for family, ip, category, reason in _failures:
            by_key[(family, category, reason)].append(ip)
    return by_key


def print_summary(found):
    """End-of-run report: per-family processed counts + grouped failures."""
    log("INFO", "=" * 60)
    log("INFO", "SCAN SUMMARY")
    log("INFO", "=" * 60)
    labels = {
        "servers": "Servers", "storage": "Storage",
        "san_switches": "SAN switches", "cisco_switches": "Cisco switches",
        "fortigates": "FortiGates", "ruckus": "Ruckus",
        "unifi": "UniFi consoles", "hikvision_nvrs": "Hikvision NVRs",
        "dahua_nvrs": "Dahua NVRs", "unv_nvrs": "Uniview NVRs",
    }
    for key, label in labels.items():
        log("INFO", f"  [OK] {label}: {len(found.get(key, []))} processed")
    by_key = _grouped()
    if not by_key:
        log("INFO", "  No scan failures — every probed device was processed.")
        return
    log("WARN", f"  {sum(len(v) for v in by_key.values())} device(s) need attention:")
    for (family, category, reason), ips in sorted(by_key.items()):
        tag = "UNREACHABLE" if category == "unreachable" else "NO DATA"
        ip_list = ", ".join(sorted(ips))
        log("WARN", f"  [{tag}] {family} — {reason} ({len(ips)}): {ip_list}")
