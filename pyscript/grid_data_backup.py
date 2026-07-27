"""
Woechentlicher Analyse-Export  ->  /config/pyscript/grid_data_backup.py
Version 1.5.0

Schreibt jeden Montag 05:00 Uhr die komplette Vorwoche (Mo 00:00 bis Mo 00:00
lokal) der Analyse-Entitaeten als CSV auf das NAS.

Dateiname:     JJJJ-KWnn-Exportdatum.csv    z. B. 2026-KW30-2026-07-27.csv
Aufbewahrung:  370 Tage (~52 Dateien)

CSV-SPALTEN
    entity_id, state, last_changed, samples, resid

    samples    Anzahl der Messwerte, aus denen ein 10-Minuten-Wert gebildet
               wurde. Nur an den 10-Minuten-Sensoren gesetzt, sonst leer;
               zeigt an, ob ein Block vollstaendig oder (nach Reload) nur
               teilbesetzt ist.
    resid      Restfehler der Trilateration in Volt, an
               sensor.sternpunktverschiebung gesetzt (vom Rohsensor
               uebernommen, damit es den Recorder-Ausschluss von
               eastron_raw_* ueberlebt). Rauschboden ~0.34 V; Werte ueber
               etwa 0.8 V zeigen an, dass sich die Last waehrend des
               Register-Pollings geaendert hat und der Zyklus fuer die
               Auswertung verworfen werden sollte.

    Hinweis: net_avg/net_avg_n werden bewusst NICHT mehr exportiert. Das
    Phantom-Gleitmittel wird offline aus p1/p2/p3 rekonstruiert; die
    Attribute wurden entfernt, um die Schreiblast auf phantom_active zu
    senken (sonst ein DB-Eintrag je Zyklus statt nur bei 0/1-Wechsel).

    Beide Spalten werden per json_extract() direkt in SQLite aus state_attributes
    geholt - in C statt in Python, und ohne die uebrigen Attribute
    (friendly_name, icon, device_class) mitzuschleppen, die die Datei um
    ~60 MB aufblaehen wuerden.

WARUM PYSCRIPT UND NICHT shell_command
    shell_command wird nur beim Start von HA eingelesen und haette einen
    Neustart erzwungen. Pyscript laedt dieses Modul mit einem blossen
    Reload (Entwicklerwerkzeuge -> Aktionen -> pyscript.reload).

EINRICHTUNG
    1. Diese Datei nach /config/pyscript/ kopieren
    2. pyscript.reload aufrufen   -> fertig, kein Neustart

MANUELLER AUFRUF / TEST
    Entwicklerwerkzeuge -> Aktionen -> pyscript.export_woche
    Optionaler Parameter weeks_back (Standard 1 = letzte Woche).

VORAUSSETZUNGEN
    - pyscript mit allow_all_imports: true (fuer sqlite3/csv/os/re)
    - Recorder auf SQLite (/config/home-assistant_v2.db)

Die Datenbank wird ausschliesslich lesend geoeffnet (mode=ro); der
laufende Recorder wird nicht beeintraechtigt. Alle blockierenden Teile
(NFS-Zugriff, DB-Abfrage, CSV-Schreiben) sind mit @pyscript_executor
dekoriert und laufen dadurch als natives Python im Worker-Thread - die
Event-Loop von Home Assistant wird nicht angehalten.
"""

import csv
import os
import re
import sqlite3
from datetime import datetime, timedelta

DB_PATH = "/config/home-assistant_v2.db"
KEEP_DAYS = 370

# Fester Ablagepfad AUS SICHT DES CORE-CONTAINERS (dort laeuft pyscript).
# Kein Ausweichpfad und keine Schreibprobe: die Exporte sollen immer im
# selben Verzeichnis landen. Ist es nicht erreichbar, schlaegt der Lauf
# fehl und meldet sich - statt die Datei woanders abzulegen.
#
# Derselbe Ordner heisst auf der HAOS-Host-Shell
#   /mnt/data/supervisor/mounts/Medien/HA_Backup/Netz_Messwerte_Backup
# Das ist ein anderer Mount-Namespace und hier nicht verwendbar.
OUT_DIR = "/media/Medien/HA_Backup/Netz_Messwerte_Backup"

# Nur exakt dieses Muster wird beim Aufraeumen geloescht. Alle anderen
# Dateien im selben Ordner bleiben unangetastet.
FNAME_RE = re.compile(r"^\d{4}-KW\d{2}-\d{4}-\d{2}-\d{2}\.csv$")
PART_RE = re.compile(r"^\d{4}-KW\d{2}-\d{4}-\d{2}-\d{2}\.csv\.part$")

ENTITIES = [
    # --- Spannungen ---
    "sensor.netzspannung_l1",
    "sensor.netzspannung_l2",
    "sensor.netzspannung_l3",
    "sensor.netzspannung_l1_l2",
    "sensor.netzspannung_l2_l3",
    "sensor.netzspannung_l3_l1",
    # --- Symmetrische Komponenten ---
    "sensor.sternpunktverschiebung",
    "sensor.gegensystem_unsymmetrie",
    # --- Sternpunkt-Richtung: Projektion auf die drei Phasenachsen (5.10.x) ---
    "sensor.sternpunkt_projektion_l1",
    "sensor.sternpunkt_projektion_l2",
    "sensor.sternpunkt_projektion_l3",
    # --- Leistungen ---
    "sensor.netzleistung_l1",
    "sensor.netzleistung_l2",
    "sensor.netzleistung_l3",
    "sensor.netzleistung_gesamt",
    "sensor.blindleistung_l1",
    "sensor.blindleistung_l2",
    "sensor.blindleistung_l3",
    "sensor.scheinleistung_l1",
    "sensor.scheinleistung_l2",
    "sensor.scheinleistung_l3",
    # --- Stroeme, direkt gemessen ---
    "sensor.netzstrom_l1",
    "sensor.netzstrom_l2",
    "sensor.netzstrom_l3",
    # --- Kontext Netz ---
    "sensor.netzfrequenz",
    "sensor.phantom_aktiv",
    # --- Erzeugung und Speicher ---
    "sensor.solis_wr_leistung_ac",
    "sensor.solis_pv_leistung_dc_gesamt",
    "sensor.solis_pv_string_1_leistung",
    "sensor.solis_pv_string_2_leistung",
    "sensor.solis_pv_string_3_leistung",
    "sensor.batterie_leistung",
    "sensor.hausverbrauch",
    # --- Lasten, Phase bekannt ---
    "sensor.shelly_mg3_4_leistung",
    "sensor.shelly_mg3_1_leistung",
    "sensor.shelly_mg3_3_leistung",
    "sensor.verbraucher_cosori_leistung",
    "sensor.shelly_mg3_9_leistung",
    "sensor.wasserbett_leistung",
    # --- Lasten, Phase offen ---
    "sensor.shelly_mg3_2_leistung",
    "sensor.shelly_mg3_10_leistung",
    "sensor.verbraucher_buro_leistung",
    "sensor.multimedia_wz_leistung",
    "sensor.netzwerk_hwr_leistung",
    "sensor.terrasse_leistung",
    # --- Shelly-Spannungen: zweiter Messpunkt, je Phase ein Paar ---
    "sensor.shelly_mg3_3_spannung_2",
    "sensor.shellyplugmg3_90706945519c_spannung",
    "sensor.shelly_mg3_3_spannung",
    "sensor.shelly_mg3_11_spannung",
    "sensor.shelly_mg3_9_spannung",
    "sensor.shelly_mg3_8_spannung",
    "sensor.shelly_mg3_10_spannung",
    # --- Shelly-Stroeme: Kompressor- und Motorlasten, fuer die Pruefung
    #     des Blindleistungskanals; bei rein ohmschen Lasten reicht P/U ---
    "sensor.shellyplugmg3_90706945519c_stromstarke",   # Kuehlschrank  L1
    "sensor.shelly_mg3_10_stromstarke",                # Garage        L3
    "sensor.shelly_mg3_9_stromstarke",                 # Geschirrsp.   L3
    "sensor.shelly_mg3_3_stromstarke_2",               # Waschmaschine L1
    "sensor.shelly_mg3_3_stromstarke",                 # Trockner      L2
    "sensor.shelly_mg3_6_stromstarke",                 # Multimedia
    "sensor.shelly_mg3_11_stromstarke",                # Cosori        L2
    # --- Zyklus-Minima der Spannungen (tragen das samples-Attribut) ---
    "sensor.netzspannung_l1_min",
    "sensor.netzspannung_l2_min",
    "sensor.netzspannung_l3_min",
    # --- 10-Minuten-Werte nach EN 50160 (tragen das samples-Attribut) ---
    "sensor.netzspannung_l1_10min",
    "sensor.netzspannung_l2_10min",
    "sensor.netzspannung_l3_10min",
    "sensor.sternpunktverschiebung_10min",
    "sensor.gegensystem_unsymmetrie_10min",
    # --- Energiezaehler: gefilterter Bezug gegen die ungefilterte
    #     Solis-Referenz. Notwendig, um die Wirkung von
    #     PHANTOM_AVG_WINDOW_S nachtraeglich zu beurteilen. ---
    "sensor.netzbezug_taeglich",
    "sensor.netzeinspeisung_taeglich",
    "sensor.solis_netzbezug_heute",
    # --- Wallbox ---
    "sensor.ebox_smart_current_import",
    "sensor.ebox_smart_voltage",
    "sensor.ebox_smart_power_active_import",
    "sensor.ebox_smart_current_offered",
]


def _week_window(now_local, weeks_back):
    """Mo 00:00 bis Mo 00:00 der gewuenschten Woche (lokale Zeit)."""
    monday = now_local - timedelta(days=now_local.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = monday - timedelta(days=7 * (weeks_back - 1))
    start = end - timedelta(days=7)
    return start, end


@pyscript_executor
def _do_export(start_ts, end_ts, fname):
    """Blockierender Teil: DB lesen, CSV schreiben, aufraeumen.

    @pyscript_executor kompiliert diese Funktion als natives Python und
    fuehrt sie beim Aufruf automatisch im Executor-Thread aus. Damit
    gelten hier KEINE pyscript-Einschraenkungen mehr (Comprehensions und
    Generatoren sind erlaubt), aber auch keine pyscript-Objekte:
    state, log und service stehen nicht zur Verfuegung. Das Ergebnis
    wird deshalb als Text zurueckgegeben und vom Aufrufer protokolliert.
    """
    out_path = os.path.join(OUT_DIR, fname)
    tmp_path = out_path + ".part"

    marks = ",".join("?" for _ in ENTITIES)

    # Der Zeitstempel wird von SQLite formatiert (C) statt von Python.
    # strftime('%f') liefert SS.SSS, das Ergebnis ist byteidentisch zur
    # frueheren Python-Variante, kostet aber statt ~2 s nur ~0 s je Mio Zeilen.
    #
    # state_attributes wird per LEFT JOIN angehaengt, damit Zeilen ohne
    # Attribute nicht verlorengehen. json_extract() zieht gezielt die zwei
    # Schluessel heraus, die fuer die Auswertung gebraucht werden.
    query = (
        "SELECT m.entity_id, s.state, "
        "strftime('%Y-%m-%dT%H:%M:%fZ', s.last_updated_ts, 'unixepoch'), "
        "COALESCE(json_extract(sa.shared_attrs, '$.samples'), ''), "
        "COALESCE(json_extract(sa.shared_attrs, '$.resid'), '') "
        "FROM states s "
        "JOIN states_meta m ON s.metadata_id = m.metadata_id "
        "LEFT JOIN state_attributes sa ON s.attributes_id = sa.attributes_id "
        "WHERE m.entity_id IN (" + marks + ") "
        "AND s.last_updated_ts >= ? AND s.last_updated_ts < ? "
        "AND s.state IS NOT NULL "
        "ORDER BY s.last_updated_ts"
    )

    params = list(ENTITIES) + [start_ts, end_ts]

    con = sqlite3.connect("file:" + DB_PATH + "?mode=ro", uri=True, timeout=60)
    rows = 0
    seen = {}
    try:
        cur = con.cursor()
        cur.execute(query, params)
        fh = open(tmp_path, "w", newline="")
        try:
            writer = csv.writer(fh)
            writer.writerow(["entity_id", "state", "last_changed",
                             "samples", "resid"])
            while True:
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                # Die Zeilen sind bereits fertig formatiert und werden ohne
                # Python-Schleife durchgereicht; nur die Statistik braucht
                # noch einen Durchlauf ueber die Entitaetsnamen.
                writer.writerows(batch)
                rows += len(batch)
                for rec in batch:
                    seen[rec[0]] = 1
        finally:
            fh.close()
    finally:
        con.close()

    os.replace(tmp_path, out_path)
    size_mb = os.path.getsize(out_path) / 1e6

    # Aufraeumen: ausschliesslich eigene Exportdateien. Zusaetzlich werden
    # .part-Reste entfernt, die entstehen, wenn die NFS-Verbindung mitten im
    # Schreiben abbricht - os.replace() kommt dann nicht mehr zum Zug.
    removed = 0
    cutoff = datetime.now().timestamp() - KEEP_DAYS * 86400
    cutoff_part = datetime.now().timestamp() - 86400
    for name in os.listdir(OUT_DIR):
        is_export = FNAME_RE.match(name) is not None
        is_part = PART_RE.match(name) is not None
        if not is_export and not is_part:
            continue
        path = os.path.join(OUT_DIR, name)
        try:
            age = os.path.getmtime(path)
            # Exporte nach KEEP_DAYS, .part-Reste schon nach einem Tag.
            limit = cutoff if is_export else (cutoff_part)
            if age < limit:
                os.remove(path)
                removed += 1
        except OSError:
            pass

    missing = len(ENTITIES) - len(seen)
    text = "%s: %d Zeilen, %.1f MB, %d/%d Entitaeten" % (
        fname, rows, size_mb, len(seen), len(ENTITIES)
    )
    if missing:
        text = text + " (%d ohne Daten)" % missing
    if removed:
        text = text + ", %d alte Datei(en) geloescht" % removed
    return text


@time_trigger("cron(0 5 * * mon)")
@service
def export_woche(weeks_back=1):
    """Analyse-Daten einer Kalenderwoche als CSV auf das NAS schreiben.

    weeks_back: 1 = letzte abgeschlossene Woche (Standard), 2 = die davor.
    """
    weeks_back = int(weeks_back)
    if weeks_back < 1:
        weeks_back = 1

    now_local = datetime.now()
    start, end = _week_window(now_local, weeks_back)
    cal = start.isocalendar()
    fname = "%04d-KW%02d-%s.csv" % (
        int(cal[0]), int(cal[1]), now_local.strftime("%Y-%m-%d")
    )

    log.info(
        "export_woche: %s bis %s -> %s/%s"
        % (start.strftime("%d.%m. %H:%M"), end.strftime("%d.%m. %H:%M"),
           OUT_DIR, fname)
    )

    try:
        # @pyscript_executor -> laeuft automatisch im Worker-Thread,
        # kein task.executor noetig.
        result = _do_export(start.timestamp(), end.timestamp(), fname)
    except Exception as err:
        msg = "Export fehlgeschlagen: %s" % err
        log.error("export_woche: " + msg)
        service.call(
            "persistent_notification", "create",
            title="Analyse-Export fehlgeschlagen",
            message=msg + "\n\nZielverzeichnis: " + OUT_DIR
                        + "\nDie Daten bleiben bis zum Recorder-Purge "
                          "verfuegbar. Erneuter Versuch ueber die Aktion "
                          "pyscript.export_woche.",
        )
        return

    log.info("export_woche: " + result)
    state.set(
        "sensor.analyse_export_letzter_lauf",
        now_local.strftime("%Y-%m-%d %H:%M"),
        new_attributes={
            "ergebnis": result,
            "pfad": OUT_DIR,
            "datei": fname,
            "friendly_name": "Analyse-Export letzter Lauf",
            "icon": "mdi:database-export",
        },
    )
