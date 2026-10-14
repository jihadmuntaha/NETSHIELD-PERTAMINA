"""
app/core/timeutil.py
====================
Helper waktu terpusat untuk Pertamina NetShield.

Konvensi database: seluruh kolom DateTime (started_at, acknowledged_at,
resolved_at, dst.) disimpan sebagai *naive datetime* dalam zona WIB (UTC+7),
sama seperti yang ditulis oleh polling engine. Semua kode yang menulis atau
membandingkan timestamp insiden harus memakai helper ini agar selisih waktu
(TTA / TTR / durasi downtime) tidak bergeser akibat campuran UTC dan jam lokal.
"""

from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))


def now_wib() -> datetime:
    """Waktu sekarang di WIB sebagai naive datetime (siap disimpan ke DB)."""
    return datetime.now(WIB).replace(tzinfo=None)
