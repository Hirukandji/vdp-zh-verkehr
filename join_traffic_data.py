"""
Wird EINMAL PRO TAG von GitHub Actions ausgeführt (nicht mehr stündlich —
das hat den Server mit 329 Anfragen/Stunde überlastet, siehe frühere
Läufe mit Massen-Timeouts). Holt für jede aktive Messstelle einen
aktuellen Wert und hängt ihn an eine selbst geführte Historie in
history.json an (rollierendes Fenster der letzten HISTORY_DAYS Tage).

Damit brauchen wir keine (unbestätigte) Historien-Funktion der VDP-ZH-
API selbst -- wir bauen die Tages-Historie einfach über die Zeit aus
unseren eigenen täglichen Schnappschüssen auf.

Echtzeitwerte für eine einzelne, angeklickte Messstelle holt sich das
Frontend weiterhin separat und nur bei Bedarf direkt von VDP-ZH.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE_URL = "https://vdp.zh.ch/pws/public-service/readOnlineAggregationData"
STATIONS_FILE = "stations.json"   # 329 aktive Messstellen, id = MESSST_NR
HISTORY_FILE = "history.json"     # rollierende Tages-Historie, wird von diesem Skript gepflegt
HISTORY_DAYS = 5                  # wie viele Tage pro Messstelle aufgehoben werden

REQUEST_TIMEOUT_SECONDS = 15
MAX_PARALLEL_REQUESTS = 4         # zurückhaltend -- höhere Werte führten zu Massen-Timeouts
CONNECTION_RETRY_COUNT = 1        # kein Retry -- hat die Erfolgsquote in Tests nicht verbessert

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}


def uid_for(station_id: int) -> str:
    return f"M{station_id:04d}"


def load_stations() -> list[dict]:
    if not os.path.isfile(STATIONS_FILE):
        sys.exit(
            f"FEHLER: '{STATIONS_FILE}' wurde im Repository nicht gefunden. "
            "Liegt die Datei im Hauptordner (gleiche Ebene wie dieses Skript)?"
        )
    with open(STATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_history() -> dict:
    if not os.path.isfile(HISTORY_FILE):
        return {}
    with open(HISTORY_FILE, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            print(f"WARNUNG: '{HISTORY_FILE}' war beschädigt, starte neue Historie.")
            return {}


def sum_flow(payload) -> float | None:
    entries = payload if isinstance(payload, list) else [payload]
    total = 0.0
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        stats = entry.get("statistics") or entry.get("details") or []
        if stats:
            for s in stats:
                total += s.get("flow", s.get("count", 0)) or 0
                found = True
        else:
            val = entry.get("flow", entry.get("count"))
            if val is not None:
                total += val
                found = True
    return total if found else None


def fetch_station(station_id: int) -> tuple[float | None, str | None]:
    url = f"{BASE_URL}/VDP/{uid_for(station_id)}?sampleOnly=true"
    request = urllib.request.Request(url, headers=HEADERS)

    last_conn_error = None
    for attempt in range(CONNECTION_RETRY_COUNT):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_conn_error = e
            if attempt < CONNECTION_RETRY_COUNT - 1:
                time.sleep(0.5)
            continue
    else:
        return None, f"Verbindung fehlgeschlagen nach {CONNECTION_RETRY_COUNT} Versuchen ({last_conn_error})"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Antwort war kein gültiges JSON"

    flow = sum_flow(payload)
    if flow is None:
        return None, "keine verwertbaren Verkehrsdaten in der Antwort"
    return flow, None


def fetch_station_safe(station_id: int) -> tuple[int, float | None, str | None]:
    try:
        flow, error = fetch_station(station_id)
    except Exception as e:
        flow, error = None, f"unerwarteter Fehler ({e})"
    return station_id, flow, error


def append_to_history(history: dict, station_id: str, today: str, value: float) -> None:
    entries = history.setdefault(station_id, [])
    # Falls heute schon ein Eintrag existiert (z.B. Skript zweimal am selben Tag
    # manuell ausgelöst) -> überschreiben statt duplizieren.
    entries[:] = [e for e in entries if e.get("date") != today]
    entries.append({"date": today, "value": value})
    entries.sort(key=lambda e: e["date"])
    del entries[:-HISTORY_DAYS]  # nur die letzten HISTORY_DAYS Einträge behalten


def main():
    stations = load_stations()
    history = load_history()
    today = datetime.now(timezone.utc).date().isoformat()

    ok, failed = 0, 0
    error_samples = []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        futures = []
        for s in stations:
            futures.append(pool.submit(fetch_station_safe, s["id"]))
            time.sleep(0.05)
        for future in as_completed(futures):
            station_id, flow, error = future.result()

            if error is not None:
                failed += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{uid_for(station_id)}: {error}")
                continue

            append_to_history(history, str(station_id), today, flow)
            ok += 1

    print(f"Abgefragt: {ok} erfolgreich, {failed} fehlgeschlagen (von {len(stations)} Messstellen).")
    if error_samples:
        print("Beispiele für Fehler:")
        for line in error_samples:
            print(f"  - {line}")

    if ok == 0:
        sys.exit(
            "FEHLER: Keine einzige Messstelle konnte erfolgreich abgefragt werden. "
            "Läuft die API gerade nicht, oder hat sich das URL-Format nochmal geändert?"
        )

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))
    print(f"'{HISTORY_FILE}' aktualisiert ({today}, {ok} Messstellen).")


if __name__ == "__main__":
    main()
