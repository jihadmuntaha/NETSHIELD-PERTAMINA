"""
app/services/ping.py
====================
Prometheus Blackbox Exporter-style Asynchronous Socket TCP Probing Helper.

Performs natural network probing via asyncio socket connections without relying on
OS CLI ping execution or regex output parsing.
"""
import asyncio
import time
from typing import Optional, Tuple


async def run_natural_probe(
    ip_address: str,
    default_port: Optional[int] = None,
    timeout: float = 2.0
) -> Tuple[bool, float, str, str]:
    """
    Perform natural TCP socket probing on a host/IP.

    Supports:
      - Pure IP: e.g. "127.0.0.1" or "10.4.12.1"
      - Host with Port: e.g. "127.0.0.1:8000" or "localhost:5000"

    Returns:
        (is_up: bool, latency_ms: float, status_label: str, target_str: str)
        status_label = "CONNECTED" or "TIMEOUT"
        target_str = "127.0.0.1:8000"
    """
    host = ip_address.strip()
    port = default_port

    if ":" in host:
        parts = host.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            host = parts[0]
            port = int(parts[1])

    # Candidate ports for natural IP probing (Web HTTP/HTTPS, custom web apps, SSH)
    candidate_ports = [port] if port is not None else [80, 443, 8000, 5000, 22]

    for p in candidate_ports:
        target_str = f"{host}:{p}"
        start_time = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, p),
                timeout=timeout
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True, max(round(elapsed_ms, 2), 0.1), "CONNECTED", target_str

        except (ConnectionRefusedError, ConnectionResetError):
            # Target IP responded on TCP layer with RST packet -> Host IS ALIVE!
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return True, max(round(elapsed_ms, 2), 0.1), "CONNECTED", target_str

        except (asyncio.TimeoutError, TimeoutError):
            continue

        except OSError:
            continue

    display_target = f"{host}:{candidate_ports[0]}" if candidate_ports else host
    return False, 0.0, "TIMEOUT", display_target


async def run_ping(ip_address: str) -> Tuple[bool, float]:
    """Compatibility wrapper around run_natural_probe."""
    is_up, latency, _, _ = await run_natural_probe(ip_address)
    return is_up, latency
