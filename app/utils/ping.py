"""
app/utils/ping.py
=================
NOC Diagnostic Toolkit - ICMP Echo Request/Reply engine.

Menjalankan utilitas ``ping`` milik sistem operasi (Windows / Linux / macOS),
lalu mem-parsing output mentahnya untuk mendapatkan rata-rata RTT.

Catatan keamanan:
- Perintah dijalankan sebagai *list argumen* (tanpa ``shell=True``) sehingga
  tidak ada interpretasi shell.
- Target divalidasi ketat (alamat IP atau hostname RFC 1123) sebelum dieksekusi
  untuk mencegah *argument injection* (mis. target berawalan ``-``).

Fungsi di modul ini bersifat **blocking** (subprocess). Dari handler async,
panggil lewat ``run_in_threadpool`` / ``asyncio.to_thread``.
"""
import ipaddress
import os
import platform
import re
import subprocess
from typing import List, Optional, Tuple
from urllib.parse import urlparse

MIN_COUNT = 1
MAX_COUNT = 10
MIN_TIMEOUT_MS = 200
MAX_TIMEOUT_MS = 10_000

_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.?$"
)

# Ringkasan latensi
_WIN_AVG_RE = re.compile(r"(?:Average|Rata-rata|Rata2|Durchschnitt|Moyenne|Media)\s*=\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
_UNIX_AVG_RE = re.compile(r"min/avg/max(?:/(?:mdev|stddev))?\s*=\s*[\d.]+/([\d.]+)/", re.IGNORECASE)
# Fallback: nilai per-reply ("time=12ms", "time<1ms", "waktu=12ms", "time=0.045 ms")
_REPLY_TIME_RE = re.compile(r"(?:time|waktu|zeit|temps|tiempo)\s*([=<])\s*(\d+(?:[.,]\d+)?)\s*ms", re.IGNORECASE)
# Bukti ada Echo Reply sungguhan (Windows bisa exit 0 walau "Destination host unreachable")
_TTL_RE = re.compile(r"\bttl[=\s]\s*\d+", re.IGNORECASE)


class InvalidPingTarget(ValueError):
    """Target ping kosong / tidak valid / berpotensi berbahaya."""


def normalize_ping_target(raw: str) -> str:
    """
    Bersihkan input operator menjadi host yang aman untuk ``ping``.

    Menerima: ``10.4.12.1``, ``10.4.12.1:8000`` (port dibuang), ``[fe80::1]``,
    ``https://pertamina.com/path`` (diambil hostname-nya), ``google.com``.

    Raises:
        InvalidPingTarget: jika target bukan IP/hostname yang valid.
    """
    host = (raw or "").strip()
    if not host:
        raise InvalidPingTarget("Target IP/Host wajib diisi.")
    if len(host) > 255:
        raise InvalidPingTarget("Target IP/Host terlalu panjang.")

    if "://" in host:
        host = urlparse(host).hostname or ""

    host = host.strip().strip("[]")

    # IPv4 / IPv6 murni
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    # host:port (bukan IPv6) -> buang port
    if host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if port.isdigit():
            host = name.strip("[]")
            try:
                return str(ipaddress.ip_address(host))
            except ValueError:
                pass

    if _HOSTNAME_RE.match(host):
        return host.rstrip(".")

    raise InvalidPingTarget(f"'{raw.strip()[:64]}' bukan alamat IP atau hostname yang valid.")


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def build_ping_command(host: str, count: int = 4, timeout_ms: int = 1000) -> List[str]:
    """Susun argumen ``ping`` sesuai OS. ``host`` harus sudah dinormalisasi."""
    system = platform.system()
    ipv6 = ["-6"] if _is_ipv6(host) else []

    if system == "Windows":
        # -n jumlah paket, -w timeout per reply (ms)
        return ["ping", *ipv6, "-n", str(count), "-w", str(timeout_ms), host]
    if system == "Darwin":
        # macOS: -W dalam milidetik
        return ["ping", *ipv6, "-c", str(count), "-W", str(timeout_ms), host]
    # Linux / Unix (iputils, busybox): -W dalam detik (min 1)
    return ["ping", *ipv6, "-c", str(count), "-W", str(max(1, int(timeout_ms / 1000))), host]


def parse_avg_latency(output: str) -> Optional[float]:
    """Ekstrak rata-rata RTT (ms) dari output ping Windows (EN/ID) maupun Linux/macOS."""
    if not output:
        return None

    match = _WIN_AVG_RE.search(output) or _UNIX_AVG_RE.search(output)
    if match:
        return round(float(match.group(1).replace(",", ".")), 3)

    # Fallback: rata-rata nilai per-reply bila baris ringkasan tidak ada
    samples = []
    for op, value in _REPLY_TIME_RE.findall(output):
        v = float(value.replace(",", "."))
        samples.append(v if op == "=" else max(v - 0.5, 0.5))  # "time<1ms" ~ 0.5 ms
    if samples:
        return round(sum(samples) / len(samples), 3)
    return None


def _decode_kwargs() -> dict:
    """Windows `ping` menulis memakai OEM code page (cp437/cp850), bukan ANSI."""
    if platform.system() == "Windows":
        return {"encoding": "oem", "errors": "replace"}
    return {"encoding": "utf-8", "errors": "replace"}


def execute_icmp_ping(host: str, count: int = 4, timeout_ms: int = 1000) -> Tuple[bool, Optional[float], str]:
    """
    Kirim ICMP Echo Request ke ``host`` menggunakan utilitas ping OS.

    Args:
        host: alamat IP / hostname (akan divalidasi & dinormalisasi).
        count: jumlah paket (di-clamp ke 1..10).
        timeout_ms: timeout per reply dalam milidetik (di-clamp ke 200..10000).

    Returns:
        (status_up, avg_latency_ms, raw_terminal_output)
        ``avg_latency_ms`` bernilai ``None`` bila tidak ada reply.

    Raises:
        InvalidPingTarget: bila ``host`` tidak valid.
    """
    target = normalize_ping_target(host)
    count = max(MIN_COUNT, min(int(count), MAX_COUNT))
    timeout_ms = max(MIN_TIMEOUT_MS, min(int(timeout_ms), MAX_TIMEOUT_MS))

    cmd = build_ping_command(target, count, timeout_ms)
    # Batas keras proses: tiap paket ~1 detik interval + timeout, plus jeda resolusi DNS
    hard_timeout = count * (1 + timeout_ms / 1000) + 5

    run_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=hard_timeout, **_decode_kwargs())
    if platform.system() == "Windows":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        run_kwargs["env"] = {**os.environ, "LC_ALL": "C", "LANG": "C"}  # output berbahasa Inggris -> regex stabil

    try:
        proc = subprocess.run(cmd, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        msg = f"{partial.rstrip()}\n\n[NETSHIELD] Proses ping dihentikan: melebihi batas waktu {hard_timeout:.0f} detik (RTO)."
        return False, None, msg.lstrip()
    except FileNotFoundError:
        return False, None, "[NETSHIELD] Utilitas 'ping' tidak ditemukan di server. Pasang paket iputils-ping / inetutils-ping."
    except OSError as exc:
        return False, None, f"[NETSHIELD] Gagal menjalankan ping: {exc}"

    output = (proc.stdout or "").strip("\r\n")
    is_up = proc.returncode == 0 and bool(_TTL_RE.search(output))
    avg = parse_avg_latency(output) if is_up else None
    return is_up, avg, output
