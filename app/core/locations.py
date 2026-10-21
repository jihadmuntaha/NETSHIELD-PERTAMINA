"""
app/core/locations.py
=====================
Registry unit operasi Pertamina Patra Niaga Region Jawa Bagian Tengah (JBT) beserta helper
untuk mengelompokkan `Device.location_name` (teks bebas) ke unit operasi kanonik.

Satu unit dapat memiliki beberapa variasi penulisan lokasi di database, misalnya
"IT Semarang" dan "FT Pengapon Semarang" sama-sama termasuk unit IT Semarang / Pengapon.
Lokasi yang tidak dikenali registry tetap ditampilkan sebagai unit tersendiri (ad-hoc),
sehingga perangkat dengan lokasi custom tidak pernah "hilang" dari filter.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

ALL_LOCATIONS = "ALL"

# Status perangkat yang dianggap sehat / peringatan (selain itu dihitung DOWN).
UP_STATUSES = {"UP", "NORMAL"}
WARNING_STATUSES = {"WARNING"}


@dataclass(frozen=True)
class JbtUnit:
    key: str             # nilai query param ?location=..., juga label singkat di tab/kartu
    label: str           # nama lengkap unit operasi
    site_type: str       # REGIONAL_OFFICE / INTEGRATED_TERMINAL / FUEL_TERMINAL / AFT
    city: str
    aliases: Tuple[str, ...] = field(default_factory=tuple)  # kata kunci pencocokan location_name


# Urutan registry = urutan tampil kartu di UI.
JBT_UNITS: Tuple[JbtUnit, ...] = (
    JbtUnit("HQ Semarang", "Regional Office (HQ Semarang)", "REGIONAL_OFFICE", "Semarang",
            ("Regional HQ", "HQ Semarang", "Regional Office", "Kantor Regional")),
    JbtUnit("IT Semarang", "Integrated Terminal Semarang / Pengapon", "INTEGRATED_TERMINAL", "Semarang",
            ("IT Semarang", "Integrated Terminal Semarang", "Pengapon")),
    JbtUnit("IT Cilacap", "Integrated Terminal Cilacap", "INTEGRATED_TERMINAL", "Cilacap",
            ("IT Cilacap", "Integrated Terminal Cilacap")),
    JbtUnit("FT Boyolali", "Fuel Terminal Boyolali", "FUEL_TERMINAL", "Boyolali", ("Boyolali",)),
    JbtUnit("FT Rewulu", "Fuel Terminal Rewulu (Yogyakarta)", "FUEL_TERMINAL", "Yogyakarta", ("Rewulu",)),
    JbtUnit("FT Tegal", "Fuel Terminal Tegal", "FUEL_TERMINAL", "Tegal", ("Tegal",)),
    JbtUnit("FT Maos", "Fuel Terminal Maos", "FUEL_TERMINAL", "Cilacap", ("Maos",)),
    JbtUnit("FT Lomanis", "Fuel Terminal Lomanis", "FUEL_TERMINAL", "Cilacap", ("Lomanis",)),
    JbtUnit("AFT JBT", "Aviation Fuel Terminal (Adi Sumarmo / A. Yani / YIA)", "AFT", "Solo · Semarang · Kulon Progo",
            ("AFT", "Aviation Fuel", "DPPU", "Adi Sumarmo", "Adisumarmo", "Ahmad Yani", "A. Yani", "A.Yani", "YIA")),
)

_UNIT_BY_KEY: Dict[str, JbtUnit] = {u.key.lower(): u for u in JBT_UNITS}

# Regex per unit; batas kata (\b) mencegah salah cocok, mis. "AFT" pada kata lain.
_UNIT_PATTERNS: Tuple[Tuple[JbtUnit, "re.Pattern[str]"], ...] = tuple(
    (u, re.compile(r"\b(?:" + "|".join(re.escape(a) for a in u.aliases) + r")\b", re.IGNORECASE))
    for u in JBT_UNITS
)


def classify_location(location_name: Optional[str]) -> Optional[JbtUnit]:
    """Petakan location_name bebas ke unit registry JBT (None jika tidak dikenali)."""
    text = (location_name or "").strip()
    if not text:
        return None
    exact = _UNIT_BY_KEY.get(text.lower())
    if exact:
        return exact
    for unit, pattern in _UNIT_PATTERNS:
        if pattern.search(text):
            return unit
    return None


def unit_key_for(location_name: Optional[str]) -> str:
    """Key unit untuk sebuah location_name; lokasi tak dikenal memakai namanya sendiri."""
    unit = classify_location(location_name)
    return unit.key if unit else ((location_name or "").strip() or "Tanpa Lokasi")


def is_all(location: Optional[str]) -> bool:
    return not (location or "").strip() or location.strip().upper() == ALL_LOCATIONS


def _empty_bucket(key: str, label: str, site_type: str, city: str, registered: bool) -> Dict:
    return {
        "key": key, "label": label, "site_type": site_type, "city": city, "registered": registered,
        "location_names": [], "total": 0, "up": 0, "warning": 0, "down": 0, "maintenance": 0,
    }


def build_location_summary(rows: Iterable[Tuple[Optional[str], Optional[str], Optional[str], bool, int]]) -> List[Dict]:
    """
    Agregasi ringkasan status per unit operasi.

    rows: iterable (location_name, site_type, status, is_maintenance, count) hasil GROUP BY.
    Hasil: seluruh unit registry (urut registry, termasuk yang belum punya perangkat),
    lalu lokasi ad-hoc yang tidak dikenali registry (urut abjad).
    """
    buckets: Dict[str, Dict] = {
        u.key: _empty_bucket(u.key, u.label, u.site_type, u.city, True) for u in JBT_UNITS
    }
    adhoc_site_votes: Dict[str, Dict[str, int]] = {}

    for location_name, site_type, status, is_maintenance, count in rows:
        unit = classify_location(location_name)
        if unit:
            bucket = buckets[unit.key]
        else:
            key = (location_name or "").strip() or "Tanpa Lokasi"
            bucket = buckets.setdefault(key, _empty_bucket(key, key, site_type or "", "", False))
            votes = adhoc_site_votes.setdefault(key, {})
            votes[site_type or ""] = votes.get(site_type or "", 0) + count

        if location_name and location_name not in bucket["location_names"]:
            bucket["location_names"].append(location_name)

        bucket["total"] += count
        status_up = (status or "").upper()
        if is_maintenance:
            bucket["maintenance"] += count
        elif status_up in UP_STATUSES:
            bucket["up"] += count
        elif status_up in WARNING_STATUSES:
            bucket["warning"] += count
        else:
            bucket["down"] += count

    # Tipe site lokasi ad-hoc = tipe yang paling banyak dipakai perangkatnya.
    for key, votes in adhoc_site_votes.items():
        buckets[key]["site_type"] = max(votes.items(), key=lambda kv: kv[1])[0]

    for bucket in buckets.values():
        total = bucket["total"]
        bucket["health_pct"] = round(bucket["up"] * 100 / total) if total else 0
        bucket["state"] = (
            "empty" if total == 0
            else "down" if bucket["down"] > 0
            else "warning" if bucket["warning"] > 0
            else "maintenance" if bucket["up"] == 0
            else "up"
        )

    registered = [buckets[u.key] for u in JBT_UNITS]
    adhoc = sorted((b for b in buckets.values() if not b["registered"]), key=lambda b: b["label"].lower())
    return registered + adhoc


def resolve_location_filter(location: Optional[str], distinct_names: Iterable[Optional[str]]) -> Optional[Dict]:
    """
    Terjemahkan query param ?location=... menjadi kriteria filter.

    Return None jika ALL/kosong. Selain itu dict:
      - {"key", "label", "names": [...]} -> filter Device.location_name IN names (unit dikenali)
      - {"key", "label", "names": None}  -> fallback ILIKE '%location%' (teks bebas)
    """
    if is_all(location):
        return None
    wanted = location.strip()
    names = [n for n in distinct_names if n]

    unit = classify_location(wanted)
    if unit:
        matched = [n for n in names if unit_key_for(n) == unit.key]
        return {"key": unit.key, "label": unit.label, "names": matched}

    exact = [n for n in names if n.strip().lower() == wanted.lower()]
    if exact:
        return {"key": exact[0].strip(), "label": exact[0].strip(), "names": exact}

    return {"key": wanted, "label": wanted, "names": None}
