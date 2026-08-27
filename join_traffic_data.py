"""
Wird stündlich von GitHub Actions ausgeführt (Basis-Daten für alle
329 aktiven Messstellen — von ursprünglich 358 aus der Excel-Liste
sind 24 laut echtem Collector-Status DISABLED/IMPORTED und damit
nicht über die Echtzeit-API abfragbar; 5 weitere waren nicht in der
Collector-Liste auffindbar. Diese wurden vorab herausgefiltert).
Echtzeitwerte für eine einzelne, angeklickte Messstelle holt sich
das Frontend separat und nur bei Bedarf direkt von VDP-ZH — dieses
Skript ist dafür nicht zuständig.

Fragt für jede aktive Messstelle einzeln die aktuelle Verkehrsmenge bei
VDP-ZH ab (ein Request pro Messstelle, wie es die API laut Swagger-UI
verlangt: /readOnlineAggregationData/VDP/{uid}?sampleOnly=true) und
schreibt das Ergebnis als latest.json, die das Frontend danach lädt.

Die Requests laufen PARALLEL (Thread-Pool), nicht nacheinander —
sequenziell mit Timeout würde bei ein paar langsamen Antworten leicht
das 10-Minuten-Zeitlimit von GitHub Actions sprengen.
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
STATIONS_FILE = "stations.json"   # deine 329 aktive Messstellen, id = MESSST_NR
OUTPUT_FILE = "latest.json"
REQUEST_TIMEOUT_SECONDS = 8       # eher kurz halten, damit einzelne Hänger nicht das Zeitbudget sprengen
MAX_PARALLEL_REQUESTS = 10        # etwas zurückhaltender als zuvor (20), um den Server nicht zu überlasten
CONNECTION_RETRY_COUNT = 2        # bei reinen Verbindungs-/Timeout-Fehlern (nicht bei HTTP-Fehlercodes) erneut versuchen

HEADERS = {
    # Möglichst wie ein echter Browser-Request wirken, da manche Server/
    # Sicherheitsschichten unbekannte User-Agents oder zu strikte Accept-
    # Header mit 406 Not Acceptable ablehnen.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}


def uid_for(station_id: int) -> str:
    # Bestätigte Regel: uID ist immer "M" + 4-stellige, mit führenden Nullen
    # aufgefüllte Messstellen-Nummer. Z.B. 88 -> M0088, 197 -> M0197,
    # 5091 -> M5091 (schon 4-stellig, keine zusätzliche Null nötig).
    return f"M{station_id:04d}"


def load_stations() -> list[dict]:
    if not os.path.isfile(STATIONS_FILE):
        sys.exit(
            f"FEHLER: '{STATIONS_FILE}' wurde im Repository nicht gefunden. "
            "Liegt die Datei im Hauptordner (gleiche Ebene wie dieses Skript)?"
        )
    with open(STATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def sum_flow(payload) -> float | None:
    """
    Eine Antwort kann ein einzelnes AggVdp-Objekt oder eine Liste davon sein.
    Innerhalb hat jede Fahrzeugklasse ihre eigene Statistik (AggVdpDetail mit
    u.a. 'flow') — wir summieren über alle Klassen zu einem Gesamtwert.
    """
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
    """Gibt (aktueller_wert, fehlermeldung) zurück — genau eines von beidem ist None.
    Bei reinen Verbindungs-/Timeout-Fehlern (nicht bei HTTP-Fehlercodes wie 404,
    die eine echte Antwort des Servers sind) wird kurz erneut versucht — das
    unterscheidet einen kurzen Netzwerk-Ausrutscher von einem echten Ausfall."""
    url = f"{BASE_URL}/VDP/{uid_for(station_id)}?sampleOnly=true"
    request = urllib.request.Request(url, headers=HEADERS)

    last_conn_error = None
    for attempt in range(CONNECTION_RETRY_COUNT):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
            break  # Erfolg -> Retry-Schleife verlassen
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}"  # echte Serverantwort, kein Verbindungsproblem -> kein Retry
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
    """Wrapper fürs Thread-Pool: fängt auch unerwartete Fehler ab, damit ein
    einzelner Ausreisser nicht den ganzen Lauf gefährdet."""
    try:
        flow, error = fetch_station(station_id)
    except Exception as e:
        flow, error = None, f"unerwarteter Fehler ({e})"
    return station_id, flow, error


def main():
    stations = load_stations()
    now = datetime.now(timezone.utc).isoformat()
    result = {"fetched_at": now, "stations": {}}

    ok, failed = 0, 0
    error_samples = []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_REQUESTS) as pool:
        futures = [pool.submit(fetch_station_safe, s["id"]) for s in stations]
        for future in as_completed(futures):
            station_id, flow, error = future.result()

            if error is not None:
                failed += 1
                if len(error_samples) < 5:
                    error_samples.append(f"{uid_for(station_id)}: {error}")
                continue

            result["stations"][str(station_id)] = {
                "current": flow,
                "fetched_at": now,
            }
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

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"'{OUTPUT_FILE}' geschrieben.")


if __name__ == "__main__":
    main()
