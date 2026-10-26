"""
app/utils/snmp.py
=================
SNMP v2c Deep Metrics Collector — Pertamina NetShield NOC.

Fungsi utama:
    async fetch_snmp_metrics(ip, community, port, timeout) -> dict

OID yang dibaca:
    MIB-II Standard (RFC 1213):
        sysDescr   : 1.3.6.1.2.1.1.1.0
        sysUpTime  : 1.3.6.1.2.1.1.3.0

    CPU Utilization (multi-vendor cascade):
        Cisco IOS   : 1.3.6.1.4.1.9.9.109.1.1.1.1.7.1
        Cisco IOS-XE: 1.3.6.1.4.1.9.9.109.1.1.1.1.8.1
        Host Resources MIB: 1.3.6.1.2.1.25.3.3.1.2.1

    Memory (Host Resources MIB - hrStorage):
        hrStorageDescr: 1.3.6.1.2.1.25.2.3.1.3
        hrStorageSize : 1.3.6.1.2.1.25.2.3.1.5
        hrStorageUsed : 1.3.6.1.2.1.25.2.3.1.6
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("netshield_snmp")

# OID Definitions
OID_SYS_DESCR           = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME          = "1.3.6.1.2.1.1.3.0"
OID_CISCO_CPU_1MIN      = "1.3.6.1.4.1.9.9.109.1.1.1.1.7.1"
OID_CISCO_CPU_5MIN      = "1.3.6.1.4.1.9.9.109.1.1.1.1.8.1"
OID_HR_PROCESSOR_LOAD   = "1.3.6.1.2.1.25.3.3.1.2.1"
OID_HR_STORAGE_DESCR    = "1.3.6.1.2.1.25.2.3.1.3"
OID_HR_STORAGE_SIZE     = "1.3.6.1.2.1.25.2.3.1.5"
OID_HR_STORAGE_USED     = "1.3.6.1.2.1.25.2.3.1.6"

_HR_STORAGE_MAX_INDEX = 10
_NO_SUCH_STRINGS = (
    "No Such Object currently exists at this OID",
    "No Such Instance currently exists at this OID",
)


def _timeticks_to_uptime(ticks: int) -> str:
    """Konversi TimeTicks (1/100 detik) ke string Xd Xh Xm Xs."""
    total_seconds = ticks // 100
    td = timedelta(seconds=total_seconds)
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"


def _empty_result(error: str = "") -> Dict[str, Any]:
    return {
        "status": False,
        "sys_descr": None,
        "uptime": None,
        "cpu_usage": None,
        "memory_usage": None,
        "error": error,
    }


async def _snmp_get_single(
    ip: str, community: str, port: int, oid: str, timeout: int
) -> Optional[Any]:
    """SNMP GET untuk satu OID. Mengembalikan nilai atau None jika gagal/tidak ada."""
    try:
        from pysnmp.hlapi.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
        )

        error_indication, error_status, _, var_binds = await getCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            UdpTransportTarget((ip, port), timeout=timeout, retries=0),
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )

        if error_indication or error_status:
            return None

        if var_binds:
            _, value = var_binds[0]
            raw = value.prettyPrint()
            if raw in _NO_SUCH_STRINGS:
                return None
            return value

    except Exception as exc:
        logger.debug("[SNMP GET] %s oid=%s err=%s", ip, oid, exc)
    return None


async def _fetch_cpu(ip: str, community: str, port: int, timeout: int) -> Optional[float]:
    """Ambil CPU% dengan cascade: Cisco 1min -> Cisco 5min -> hrProcessorLoad."""
    for oid in (OID_CISCO_CPU_1MIN, OID_CISCO_CPU_5MIN, OID_HR_PROCESSOR_LOAD):
        val = await _snmp_get_single(ip, community, port, oid, timeout)
        if val is not None:
            try:
                pct = float(str(val))
                if 0.0 <= pct <= 100.0:
                    return round(pct, 1)
            except (TypeError, ValueError):
                continue
    return None


async def _fetch_memory(ip: str, community: str, port: int, timeout: int) -> Optional[float]:
    """Ambil Memory% via hrStorage walk. Cari entri RAM/Physical, fallback indeks 1."""
    try:
        from pysnmp.hlapi.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            nextCmd,
        )

        engine = SnmpEngine()
        transport = UdpTransportTarget((ip, port), timeout=timeout, retries=0)

        async def _walk(base_oid: str) -> Dict[str, Any]:
            result: Dict[str, Any] = {}
            async for err_ind, err_st, _, vbs in nextCmd(
                engine,
                CommunityData(community, mpModel=1),
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(base_oid)),
                lexicographicMode=False,
                maxRows=_HR_STORAGE_MAX_INDEX,
            ):
                if err_ind or err_st:
                    break
                for oid_val, val in vbs:
                    idx = str(oid_val).rsplit(".", 1)[-1]
                    result[idx] = val
            return result

        descr_map, size_map, used_map = await asyncio.gather(
            _walk(OID_HR_STORAGE_DESCR),
            _walk(OID_HR_STORAGE_SIZE),
            _walk(OID_HR_STORAGE_USED),
        )

        # Cari indeks RAM
        target_idx: Optional[str] = None
        for idx, descr_val in descr_map.items():
            descr_str = str(descr_val).lower()
            if any(k in descr_str for k in ("ram", "physical", "real")):
                target_idx = idx
                break
        if target_idx is None and descr_map:
            target_idx = list(descr_map.keys())[0]

        if target_idx and target_idx in size_map and target_idx in used_map:
            total = int(str(size_map[target_idx]))
            used  = int(str(used_map[target_idx]))
            if total > 0:
                return round((used / total) * 100, 1)

    except Exception as exc:
        logger.debug("[SNMP MEM] %s err=%s", ip, exc)
    return None


async def fetch_snmp_metrics(
    ip: str,
    community: str = "public",
    port: int = 161,
    timeout: int = 2,
) -> Dict[str, Any]:
    """
    SNMP v2c deep inspection terhadap satu perangkat jaringan.

    Returns dict:
        status       (bool)       : True jika minimal satu OID berhasil dibaca.
        sys_descr    (str|None)   : Deskripsi perangkat (sysDescr).
        uptime       (str|None)   : Uptime perangkat dalam format '3d 2h 15m 4s'.
        cpu_usage    (float|None) : CPU utilization 0-100 %.
        memory_usage (float|None) : Memory utilization 0-100 %.
        error        (str)        : Pesan error jika gagal.
    """
    try:
        # Fase 1: sysDescr + sysUpTime paralel
        sys_descr_val, uptime_val = await asyncio.gather(
            _snmp_get_single(ip, community, port, OID_SYS_DESCR, timeout),
            _snmp_get_single(ip, community, port, OID_SYS_UPTIME, timeout),
        )

        if sys_descr_val is None and uptime_val is None:
            return _empty_result(
                f"Tidak ada respons SNMP dari {ip} (community='{community}', port={port})"
            )

        # Fase 2: CPU + Memory paralel
        cpu_val, mem_val = await asyncio.gather(
            _fetch_cpu(ip, community, port, timeout),
            _fetch_memory(ip, community, port, timeout),
        )

        # Parse uptime
        uptime_str: Optional[str] = None
        if uptime_val is not None:
            try:
                uptime_str = _timeticks_to_uptime(int(str(uptime_val)))
            except (TypeError, ValueError):
                uptime_str = str(uptime_val)

        # Sanitasi sys_descr
        sys_descr_clean: Optional[str] = None
        if sys_descr_val is not None:
            sys_descr_clean = " ".join(str(sys_descr_val).split())[:500]

        logger.info(
            "[SNMP OK] %s | cpu=%.1f%% mem=%.1f%% uptime=%s",
            ip,
            cpu_val if cpu_val is not None else -1,
            mem_val if mem_val is not None else -1,
            uptime_str or "N/A",
        )

        return {
            "status": True,
            "sys_descr": sys_descr_clean,
            "uptime": uptime_str,
            "cpu_usage": cpu_val,
            "memory_usage": mem_val,
            "error": "",
        }

    except Exception as exc:
        logger.warning("[SNMP ERROR] %s err=%s", ip, exc)
        return _empty_result(str(exc))
