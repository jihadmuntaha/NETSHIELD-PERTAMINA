"""
app/utils/snmp.py
=================
SNMP v2c Deep Metrics Collector — Pertamina NetShield NOC.

Fungsi utama:
    async fetch_snmp_metrics(ip, community, port, timeout) -> dict

Desain (aman untuk event loop FastAPI):
    - Memakai ``pysnmp.hlapi.asyncio`` (pysnmp-lextudio >= 6.0): ``getCmd`` adalah coroutine,
      sehingga tidak ada operasi socket blocking di event loop.
    - SATU PDU GET berisi seluruh OID -> satu round-trip UDP per perangkat. Pada SNMP v2c, OID
      yang tidak didukung agent dikembalikan per-varbind sebagai noSuchObject/noSuchInstance,
      tanpa menggagalkan OID lain.
    - timeout pendek (default 2 detik) + retries=1, dibungkus ``asyncio.wait_for`` sebagai
      batas keras -> worst case ~ timeout * (retries + 1) + 1 detik.
    - SnmpEngine di-cache per event loop (membuat engine baru per request mahal dan
      membocorkan socket UDP).
    - Resolusi hostname dilakukan via ``loop.getaddrinfo`` (non-blocking), karena konstruktor
      ``UdpTransportTarget`` memanggil ``socket.getaddrinfo`` secara sinkron.

OID yang dibaca:
    MIB-II Standard (RFC 1213):
        sysDescr   : 1.3.6.1.2.1.1.1.0
        sysUpTime  : 1.3.6.1.2.1.1.3.0        (TimeTicks, 1/100 detik)

    CISCO-PROCESS-MIB (CPU):
        cpmCPUTotal1minRev : 1.3.6.1.4.1.9.9.109.1.1.1.1.7.1   (utama)
        cpmCPUTotal5minRev : 1.3.6.1.4.1.9.9.109.1.1.1.1.8.1   (fallback)

    CISCO-MEMORY-POOL-MIB (Processor pool):
        ciscoMemoryPoolUsed : 1.3.6.1.4.1.9.9.48.1.1.1.5.1
        ciscoMemoryPoolFree : 1.3.6.1.4.1.9.9.48.1.1.1.6.1
"""

import asyncio
import ipaddress
import logging
import socket
from typing import Any, Dict, List, Optional

logger = logging.getLogger("netshield_snmp")

# ---------------------------------------------------------------------------
# OID Definitions
# ---------------------------------------------------------------------------
OID_SYS_DESCR       = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME      = "1.3.6.1.2.1.1.3.0"
OID_CISCO_CPU_1MIN  = "1.3.6.1.4.1.9.9.109.1.1.1.1.7.1"
OID_CISCO_CPU_5MIN  = "1.3.6.1.4.1.9.9.109.1.1.1.1.8.1"
OID_CISCO_MEM_USED  = "1.3.6.1.4.1.9.9.48.1.1.1.5.1"
OID_CISCO_MEM_FREE  = "1.3.6.1.4.1.9.9.48.1.1.1.6.1"

# OID wajib (MIB-II) dipakai sebagai fallback bila agent menolak seluruh PDU.
_CORE_OIDS: List[str] = [OID_SYS_DESCR, OID_SYS_UPTIME]
_ALL_OIDS: List[str] = _CORE_OIDS + [
    OID_CISCO_CPU_1MIN,
    OID_CISCO_CPU_5MIN,
    OID_CISCO_MEM_USED,
    OID_CISCO_MEM_FREE,
]

DEFAULT_RETRIES = 1
_SYS_DESCR_MAX_LEN = 500

# Cache SnmpEngine per event loop (engine terikat ke loop tempat transport dibuat).
_engine: Any = None
_engine_loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def timeticks_to_duration(ticks: Any) -> str:
    """
    Konversi sysUpTime (TimeTicks, satuan 1/100 detik) ke format durasi hari/jam/menit.

    Contoh: 112233445 -> "12 hari 23 jam 45 menit"
    """
    total_seconds = max(int(ticks), 0) // 100
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days} hari {hours} jam {minutes} menit"


def _result(
    status: bool = False,
    sys_descr: Optional[str] = None,
    uptime: Optional[str] = None,
    cpu_usage: Optional[float] = None,
    memory_usage: Optional[float] = None,
    error: str = "",
) -> Dict[str, Any]:
    """Bentuk dictionary hasil yang konsisten untuk seluruh jalur sukses/gagal."""
    return {
        "status": status,
        "sys_descr": sys_descr,
        "uptime": uptime,
        "cpu_usage": cpu_usage,
        "memory_usage": memory_usage,
        "error": error,
    }


def _get_engine():
    """Kembalikan SnmpEngine milik event loop aktif (dibuat ulang bila loop berganti)."""
    global _engine, _engine_loop
    from pysnmp.hlapi.asyncio import SnmpEngine

    loop = asyncio.get_running_loop()
    if _engine is None or _engine_loop is not loop:
        _engine = SnmpEngine()
        _engine_loop = loop
    return _engine


async def _resolve_ipv4(host: str) -> str:
    """Pastikan host berupa IPv4 literal; hostname di-resolve secara non-blocking."""
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_DGRAM)
    if not infos:
        raise OSError(f"Hostname '{host}' tidak dapat di-resolve")
    return infos[0][4][0]


def _is_missing(value: Any) -> bool:
    """True bila varbind kosong / noSuchObject / noSuchInstance / endOfMibView."""
    if value is None:
        return True
    from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

    return isinstance(value, (NoSuchObject, NoSuchInstance, EndOfMibView))


def _to_percent(value: Any) -> Optional[float]:
    """Konversi Gauge32/Integer ke persentase 0-100 (None bila di luar rentang)."""
    try:
        pct = float(int(value))
    except (TypeError, ValueError):
        return None
    return round(pct, 1) if 0.0 <= pct <= 100.0 else None


async def _snmp_get(
    ip: str, community: str, port: int, timeout: int, retries: int, oids: List[str]
) -> Dict[str, Any]:
    """
    Satu SNMP v2c GET untuk beberapa OID sekaligus.

    Returns:
        {"error": str, "values": {oid: value}} — value yang tidak tersedia tidak dimasukkan.
    """
    from pysnmp.hlapi.asyncio import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        UdpTransportTarget,
        getCmd,
    )

    error_indication, error_status, error_index, var_binds = await getCmd(
        _get_engine(),
        CommunityData(community, mpModel=1),  # mpModel=1 -> SNMP v2c
        UdpTransportTarget((ip, port), timeout=timeout, retries=retries),
        ContextData(),
        *[ObjectType(ObjectIdentity(oid)) for oid in oids],
        lookupMib=False,  # OID numerik saja: tanpa resolusi MIB (lebih cepat)
    )

    if error_indication:
        return {"error": str(error_indication), "values": {}}
    if error_status:
        return {
            "error": f"{error_status.prettyPrint()} (index {int(error_index or 0)})",
            "values": {},
        }

    # Respons GET selalu mengikuti urutan varbind request -> petakan berdasarkan posisi.
    values: Dict[str, Any] = {}
    for oid, var_bind in zip(oids, var_binds):
        value = var_bind[1]
        if not _is_missing(value):
            values[oid] = value
    return {"error": "", "values": values}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def fetch_snmp_metrics(
    ip: str,
    community: str = "public",
    port: int = 161,
    timeout: int = 2,
) -> Dict[str, Any]:
    """
    SNMP v2c deep inspection terhadap satu perangkat jaringan.

    Returns dict:
        status       (bool)       : True jika agent merespons minimal satu OID MIB-II.
        sys_descr    (str|None)   : Deskripsi perangkat (sysDescr).
        uptime       (str|None)   : Uptime perangkat, mis. '12 hari 23 jam 45 menit'.
        cpu_usage    (float|None) : CPU utilization 0-100 % (Cisco 1-min avg, fallback 5-min).
        memory_usage (float|None) : Memory utilization 0-100 % (Cisco processor pool).
        error        (str)        : Pesan error jika gagal ('' bila sukses).

    Fungsi ini tidak pernah me-raise exception; seluruh kegagalan dikembalikan sebagai
    status=False beserta pesan error.
    """
    host = (ip or "").strip()
    # Toleransi input "ip:port" (port TCP probe) — SNMP selalu memakai parameter `port`.
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    if not host:
        return _result(error="IP address kosong")

    community = (community or "public").strip() or "public"
    try:
        port = int(port or 161)
    except (TypeError, ValueError):
        port = 161
    timeout = max(int(timeout or 2), 1)
    retries = DEFAULT_RETRIES
    hard_limit = timeout * (retries + 1) + 1  # batas keras agar loop polling tidak tertahan

    try:
        import pysnmp.hlapi.asyncio  # noqa: F401  (validasi dependensi lebih awal)
    except ImportError:
        return _result(error="Pustaka pysnmp-lextudio belum terpasang (pip install -r requirements.txt)")

    try:
        target_ip = await asyncio.wait_for(_resolve_ipv4(host), timeout=timeout)

        async def _query() -> Dict[str, Any]:
            res = await _snmp_get(target_ip, community, port, timeout, retries, _ALL_OIDS)
            # Agent lama/non-standar kadang menolak seluruh PDU (errorStatus) karena OID vendor;
            # ulangi sekali hanya dengan OID MIB-II wajib.
            if res["error"] and not res["values"] and "timeout" not in res["error"].lower():
                res = await _snmp_get(target_ip, community, port, timeout, 0, _CORE_OIDS)
            return res

        res = await asyncio.wait_for(_query(), timeout=hard_limit)
        values = res["values"]

        if OID_SYS_DESCR not in values and OID_SYS_UPTIME not in values:
            reason = res["error"] or "agent tidak mengembalikan sysDescr/sysUpTime"
            return _result(error=f"Tidak ada respons SNMP dari {host}:{port} — {reason}")

        # sysDescr — rapikan whitespace/newline (Cisco IOS mengirim multi-baris)
        sys_descr: Optional[str] = None
        if OID_SYS_DESCR in values:
            sys_descr = " ".join(str(values[OID_SYS_DESCR]).split())[:_SYS_DESCR_MAX_LEN] or None

        # sysUpTime -> "X hari Y jam Z menit"
        uptime: Optional[str] = None
        if OID_SYS_UPTIME in values:
            try:
                uptime = timeticks_to_duration(int(values[OID_SYS_UPTIME]))
            except (TypeError, ValueError):
                uptime = str(values[OID_SYS_UPTIME])

        # CPU — Cisco 1-minute average, fallback 5-minute average
        cpu_usage: Optional[float] = None
        for oid in (OID_CISCO_CPU_1MIN, OID_CISCO_CPU_5MIN):
            if oid in values:
                cpu_usage = _to_percent(values[oid])
                if cpu_usage is not None:
                    break

        # Memory — Cisco processor pool: used / (used + free)
        memory_usage: Optional[float] = None
        if OID_CISCO_MEM_USED in values and OID_CISCO_MEM_FREE in values:
            try:
                used = int(values[OID_CISCO_MEM_USED])
                free = int(values[OID_CISCO_MEM_FREE])
                if used + free > 0:
                    memory_usage = round(used * 100.0 / (used + free), 1)
            except (TypeError, ValueError):
                memory_usage = None

        logger.info(
            "[SNMP OK] %s:%s | cpu=%s mem=%s uptime=%s",
            host, port,
            f"{cpu_usage:.1f}%" if cpu_usage is not None else "N/A",
            f"{memory_usage:.1f}%" if memory_usage is not None else "N/A",
            uptime or "N/A",
        )
        return _result(
            status=True,
            sys_descr=sys_descr,
            uptime=uptime,
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
        )

    except asyncio.TimeoutError:
        return _result(error=f"SNMP timeout ke {host}:{port} (> {hard_limit}s)")
    except asyncio.CancelledError:
        raise  # jangan telan pembatalan task (shutdown server)
    except Exception as exc:  # noqa: BLE001 — collector tidak boleh menjatuhkan loop polling
        logger.warning("[SNMP ERROR] %s:%s err=%s", host, port, exc)
        return _result(error=f"{exc.__class__.__name__}: {exc}")
