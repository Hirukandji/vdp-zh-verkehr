"""
Wird alle 15 Minuten von GitHub Actions ausgeführt.
Holt die Aggregationsdaten von VDP-ZH, verknüpft sie über die uID mit den
358 festen Geodaten-Punkten (stations.json) und schreibt das Ergebnis als
latest.json, die das Frontend danach lädt.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

AGGREGATION_URL = "https://vdp.zh.ch/pws/public-service/readOnlineAggregationData"
STATIONS_FILE = "stations.json"   # deine 358 Messstellen, id = MESSST_NR
OUTPUT_FILE = "latest.json"


def extract_numeric_id(uid_like) -> int | None:
    """
    Zieht aus einer uID wie "M01190" (oder einem {id, sub}-Objekt) die reine
    Zahl 1190. Robust gegen unbekanntes Präfix/Padding — wir wissen nicht
    sicher, ob die echte uID "M0" + Zahl ist oder anders aussieht.
    """
    raw = uid_like.get("id") if isinstance(uid_like, dict) else uid_like
    if raw is None:
        return None
    digits = re.sub(r"\D", "", str(raw)).lstrip("0")
    return int(digits) if digits else None


def load_stations() -> dict[int, dict]:
    if not os.path.isfile(STATIONS_FILE):
        sys.exit(
            f"FEHLER: '{STATIONS_FILE}' wurde im Repository nicht gefunden. "
            "Liegt die Datei im Hauptordner (gleiche Ebene wie dieses Skript)?"
        )
    with open(STATIONS_FILE, encoding="utf-8") as f:
        stations = json.load(f)
    return {s["id"]: s for s in stations}


def fetch_aggregation_data() -> list[dict]:
    # Ohne echten User-Agent blocken manche Server/WAFs die Anfrage mit 403,
    # weil Pythons Standard-Header ("Python-urllib/3.x") als Bot erkannt wird.
    request = urllib.request.Request(
        AGGREGATION_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; vdp-zh-sync/1.0)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        sys.exit(
            f"FEHLER: VDP-ZH antwortete mit HTTP {e.code} ({e.reason}).\n"
            f"Antwortausschnitt: {body}"
        )
    except urllib.error.URLError as e:
        sys.exit(f"FEHLER: Verbindung zu VDP-ZH fehlgeschlagen: {e.reason}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        preview = raw[:500].decode("utf-8", errors="replace")
        sys.exit(
            "FEHLER: Antwort von VDP-ZH war kein gültiges JSON (vermutlich eine "
            f"HTML-Fehlerseite). Antwortausschnitt: {preview}"
        )

    # Je nach echter Response-Form anpassen: manche APIs verpacken die
    # Liste in {"items": [...]} statt sie direkt zurückzugeben.
    entries = data if isinstance(data, list) else data.get("items", data.get("data", []))
    if not entries:
        print("WARNUNG: VDP-ZH hat eine leere Liste zurückgegeben — nichts zu verknüpfen.")
    return entries


def sum_flow(entry: dict) -> float | None:
    """
    Ein AggVdp-Eintrag hat pro Fahrzeugklasse eine eigene Statistik
    (AggVdpDetail mit u.a. 'flow'). Wir summieren über alle Klassen zu
    einem Gesamtwert für die Messstelle.
    """
    stats = entry.get("statistics") or entry.get("details") or []
    if not stats:
        return entry.get("flow") or entry.get("count")
    return sum(s.get("flow", s.get("count", 0)) for s in stats)


def join(stations_by_id: dict[int, dict], aggregation_entries: list[dict]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    matched, unmatched = 0, 0
    result = {"fetched_at": now, "stations": {}}

    for entry in aggregation_entries:
        uid = entry.get("uniqueId") or entry.get("uID") or entry.get("id")
        numeric_id = extract_numeric_id(uid)

        if numeric_id is None or numeric_id not in stations_by_id:
            unmatched += 1
            continue

        flow = sum_flow(entry)
        if flow is None:
            continue

        result["stations"][str(numeric_id)] = {
            "current": flow,
            "intBegin": entry.get("intBegin"),
        }
        matched += 1

    print(f"Verknüpft: {matched} Messstellen, nicht zugeordnet: {unmatched}")
    if matched == 0 and aggregation_entries:
        print(
            "WARNUNG: Keine einzige Messstelle konnte zugeordnet werden — "
            "vermutlich stimmt das angenommene uID-Format nicht. "
            f"Beispiel-uID aus der Antwort: {aggregation_entries[0].get('uniqueId') or aggregation_entries[0].get('uID')}"
        )
    return result


def main():
    stations_by_id = load_stations()
    aggregation_entries = fetch_aggregation_data()
    result = join(stations_by_id, aggregation_entries)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"'{OUTPUT_FILE}' geschrieben.")


if __name__ == "__main__":
    main()