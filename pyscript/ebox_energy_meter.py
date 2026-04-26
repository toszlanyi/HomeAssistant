"""
eBox Smart – Virtueller Energie-Zaehler (pyscript)
===================================================
Version: 3.2
Ablage:  config/pyscript/ebox_energy_meter.py

Gibt zwei rohe Sensoren aus (Attribute + Helper in ebox_energy_meter.yaml):
  sensor.ebox_energy_cumulative_raw        – kWh kumuliert (monoton, absolut)
  sensor.ebox_charging_state_raw           – Textzustand der State-Machine

Hinweis Session-Fortschritt:
  Intern wird session_progress weiterhin verfolgt und fliesst in den
  kumulierten Sensor ein. Als separater Ausgabe-Sensor ist er nicht
  noetig – dafuer steht sensor.ebox_smart_energy_active_import_register
  der OCPP-Integration zur Verfuegung.

Persistenz-Helper (in ebox_energy_meter.yaml):
  input_number.ebox_cumulative_energy      – kWh kumuliert ueber Neustarts
  input_number.ebox_register_offset        – Offset der aktuellen Session

Absoluter Zaehlerstand (Erststart-Bootstrap):
  Beim allerersten Start (Helper == 0) liest das Script einmalig
  start + max(0, session) als absoluten Basiswert ein.
  Danach faellt der Sensor nie unter diesen Wert – auch nach einem
  Wallbox-Reset nicht. Kein manueller Eingriff noetig.
  Manueller Override: Service pyscript.ebox_meter_set_baseline

Reset-Handling:
  Die eBox setzt bei einem Wallbox-Neustart start=0 / session=0.
  Danach stellt sie aus dem Flash start=<alter Wert> wieder her.
  Das Script erkennt diesen Zustand und schuetzt den kumulierten Wert.
  Sonderfall: Erststart waehrend Reset → Bootstrap wird nach Restore
  automatisch nachgeholt.

Pyscript HACS Kompatibilitaet:
  - service.call() ist in pyscript synchron (kein await).
  - _persist() ist daher ein normales def, wird direkt aufgerufen.
  - await task.sleep() ist nur im async startup-Trigger erlaubt
    und gueltig. In state_trigger-Callbacks kein sleep noetig.
"""

# ── Sensor-Namen ──────────────────────────────────────────────────────────────
SENSOR_START     = "sensor.ebox_smart_energy_meter_start"
SENSOR_SESSION   = "sensor.ebox_smart_energy_session"
SENSOR_CONNECTOR = "sensor.ebox_smart_status_connector"

# ── Schwellwert: session-Sprung ab dem eine neue Ladesession erkannt wird ─────
SESSION_START_THRESHOLD = 5.0   # kWh

# ── Persistenz-Helper ─────────────────────────────────────────────────────────
HELPER_CUMULATIVE = "input_number.ebox_cumulative_energy"
HELPER_REG_OFFSET = "input_number.ebox_register_offset"

# ── Reset-Erkennung: start faellt unter diesen Wert → Wallbox-Reset ──────────
RESET_DETECTION_THRESHOLD = 1.0   # kWh

# ── Verzoegerung beim HA-Start damit alle Entitaeten geladen sind ─────────────
STARTUP_DELAY_S = 3   # Sekunden

# =============================================================================
# INTERNE ZUSTANDSVARIABLEN
# =============================================================================
_state = {
    "charging":             False,   # True waehrend aktiver Ladesession
    "register_offset":      0.0,     # register_calc-Wert zu Beginn der Session
    "cumulative_committed": 0.0,     # kWh aller abgeschlossenen Sessions (absolut)
    "session_progress":     0.0,     # kWh der laufenden Session
    "prev_session":         None,    # letzter bekannter session-Wert
    "prev_start":           999.0,   # letzter bekannter start-Wert
    "wallbox_resetting":    False,   # True waehrend start=0 / Restore-Phase
    "bootstrap_pending":    False,   # True wenn Erststart waehrend Reset
}


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def _get_float(entity_id):
    """Liest einen HA-State sicher als float. Gibt None bei Fehler/NaN zurueck."""
    try:
        val = float(state.get(entity_id))
        return None if val != val else val  # NaN-Check
    except (ValueError, TypeError):
        return None


def _register_calc():
    """start + session = interner Sessionzaehler der eBox (0 bei Ladestart)."""
    s    = _get_float(SENSOR_START)
    sess = _get_float(SENSOR_SESSION)
    if s is None or sess is None:
        return None
    return s + sess


def _publish():
    """Schreibt die zwei raw-Sensoren (ohne Attribute – liegen im YAML)."""
    total = _state["cumulative_committed"] + _state["session_progress"]

    state.set(
        "sensor.ebox_energy_cumulative_raw",
        value=round(total, 3),
    )

    sess_val = _get_float(SENSOR_SESSION) or 0.0
    if _state["charging"]:
        status = "Lädt"
    elif _state["wallbox_resetting"]:
        status = "Reset"
    elif sess_val > 0:
        status = "Pausiert"
    else:
        status = "Bereit"

    state.set(
        "sensor.ebox_charging_state_raw",
        value=status,
    )


def _persist():
    """Speichert kumulierten Wert und Register-Offset in die input_number-Helper.
    Hinweis: service.call() ist in pyscript HACS synchron – kein await.
    """
    try:
        service.call(
            "input_number", "set_value",
            entity_id=HELPER_CUMULATIVE,
            value=round(_state["cumulative_committed"], 3),
        )
        service.call(
            "input_number", "set_value",
            entity_id=HELPER_REG_OFFSET,
            value=round(_state["register_offset"], 3),
        )
    except Exception as e:
        log.warning(f"ebox_meter: Persistenz fehlgeschlagen: {e}")


# =============================================================================
# INITIALISIERUNG
# =============================================================================

@time_trigger("startup")
async def ebox_meter_init():
    """
    Liest gespeicherten Stand nach HA-Neustart wieder ein.

    Wartet STARTUP_DELAY_S Sekunden, damit alle Entitaetszustaende
    vollstaendig geladen sind, bevor start und session gelesen werden.

    Erststart-Bootstrap:
      Wenn HELPER_CUMULATIVE == 0 (noch nie gesetzt), wird einmalig
      start + max(0, session) als absoluter Basiswert eingelesen.
      Das stellt sicher, dass der Sensor nie unter den bereits
      bekannten Wallbox-Zaehlerstand faellt – auch nicht nach einem
      Wallbox-Reset.
    """
    await task.sleep(STARTUP_DELAY_S)

    committed = _get_float(HELPER_CUMULATIVE)
    offset    = _get_float(HELPER_REG_OFFSET)
    sess      = _get_float(SENSOR_SESSION)
    start     = _get_float(SENSOR_START)

    _state["prev_session"] = sess
    _state["prev_start"]   = start if start is not None else 999.0

    # ── Persistierten Register-Offset laden ───────────────────────────────────
    if offset is not None:
        _state["register_offset"] = offset

    # ── Erststart vs. normaler Neustart ───────────────────────────────────────
    is_first_start = (committed is None or committed < 0.001)

    if is_first_start:
        if start is not None and start > RESET_DETECTION_THRESHOLD:
            # Normalzustand: absoluten Basiswert aus Wallbox-Registern lesen
            baseline = start + max(0.0, sess or 0.0)
            _state["cumulative_committed"] = baseline
            log.info(
                f"ebox_meter: Erststart-Bootstrap: "
                f"start={start:.3f} + session={max(0.0, sess or 0.0):.3f} "
                f"= {baseline:.3f} kWh"
            )
            _persist()
        else:
            # Erststart waehrend Wallbox-Reset: Bootstrap wird nach Restore nachgeholt
            _state["bootstrap_pending"]    = True
            _state["wallbox_resetting"]    = True
            _state["cumulative_committed"] = 0.0
            log.warning(
                "ebox_meter: Erststart waehrend Wallbox-Reset – "
                "Bootstrap wird nach Restore nachgeholt."
            )
    else:
        # Normaler Neustart: persistierten Wert laden
        _state["cumulative_committed"] = committed
        log.info(f"ebox_meter: Neustart – kumuliert={committed:.3f} kWh")

        # Lief beim HA-Neustart gerade eine Ladesession?
        if sess is not None and sess < -SESSION_START_THRESHOLD:
            _state["charging"] = True
            reg = _register_calc()
            if reg is not None:
                _state["session_progress"] = max(0.0, reg - _state["register_offset"])
            log.info("ebox_meter: Aktive Ladesession erkannt – Fortschritt fortgefuehrt.")
        elif start is not None and start < RESET_DETECTION_THRESHOLD:
            _state["wallbox_resetting"] = True
            log.info("ebox_meter: Wallbox befindet sich im Reset/Restore-Zustand.")

    _publish()


# =============================================================================
# HAUPT-TRIGGER: session-Sensor
# =============================================================================

@state_trigger(SENSOR_SESSION)
def ebox_session_changed(value=None, old_value=None):
    """
    Kern-Logik der State-Machine. Reagiert auf jeden Wechsel des session-Sensors.

    FALL 1 – session >= 0  →  session < -THRESHOLD : Neuer Ladevorgang gestartet
    FALL 2 – session <  0  →  session > 0          : Ladevorgang beendet (Ground Truth)
    FALL 3 – session = 0                            : Session-Commit (start springt nach)
    FALL 4 – session < 0, kein Vorzeichenwechsel   : Ladefortschritt aktualisieren
    FALL 5 – session = 0  UND  start ~= 0          : Wallbox-Reset erkannt
    """
    try:
        new_sess = float(value)
    except (ValueError, TypeError):
        return

    old_sess = _state["prev_session"]
    if old_value not in (None, "unavailable", "unknown"):
        try:
            old_sess = float(old_value)
        except (ValueError, TypeError):
            pass

    start_val = _get_float(SENSOR_START)
    reg_calc  = _register_calc()

    if start_val is None or reg_calc is None:
        _state["prev_session"] = new_sess
        return

    # ── FALL 5: Wallbox-Reset ─────────────────────────────────────────────────
    if new_sess == 0.0 and start_val < RESET_DETECTION_THRESHOLD:
        if _state["charging"]:
            log.warning(
                f"ebox_meter: Wallbox-Reset waehrend Ladevorgang! "
                f"Sessionfortschritt ({_state['session_progress']:.3f} kWh) verloren. "
                f"Kumuliert ({_state['cumulative_committed']:.3f} kWh) bleibt erhalten."
            )
        else:
            log.info("ebox_meter: Wallbox-Reset erkannt (start~0, session=0).")
        _state["charging"]         = False
        _state["session_progress"] = 0.0
        _state["wallbox_resetting"] = True
        _state["prev_session"]     = new_sess
        _publish()
        return

    # ── FALL 1: Ladevorgang startet ───────────────────────────────────────────
    if (old_sess is None or old_sess >= 0) and new_sess < -SESSION_START_THRESHOLD:
        _state["charging"]         = True
        _state["wallbox_resetting"] = False
        _state["register_offset"]  = reg_calc  # Offset kompensiert Kontinuations-Sessions
        _state["session_progress"] = 0.0
        log.info(
            f"ebox_meter: Ladevorgang GESTARTET | "
            f"start={start_val:.3f} | session={new_sess:.3f} | "
            f"register_offset={reg_calc:.3f}"
        )

    # ── FALL 2: Ladevorgang beendet (positiver session-Wert = Ground Truth) ───
    elif old_sess is not None and old_sess < 0 and new_sess > 0:
        session_energy = new_sess
        _state["charging"]              = False
        _state["cumulative_committed"] += session_energy
        _state["session_progress"]      = 0.0
        log.info(
            f"ebox_meter: Ladevorgang BEENDET | "
            f"Session={session_energy:.3f} kWh | "
            f"Kumuliert={_state['cumulative_committed']:.3f} kWh"
        )
        _persist()

    # ── FALL 3: Session-Commit (session=0, start springt ca. 1 ms spaeter) ───
    elif new_sess == 0.0:
        pass  # Energie wurde in FALL 2 gebucht – hier nichts tun.

    # ── FALL 4: Ladefortschritt ───────────────────────────────────────────────
    elif _state["charging"] and new_sess < 0:
        _state["session_progress"] = max(0.0, reg_calc - _state["register_offset"])

    _state["prev_session"] = new_sess
    _publish()


# =============================================================================
# SEKUNDAER-TRIGGER: start-Sensor
# =============================================================================

@state_trigger(SENSOR_START)
def ebox_start_changed(value=None, old_value=None):
    """
    Ueberwacht den start-Sensor fuer zwei Sonderfaelle:

    Fall A – start faellt auf ~0  : Wallbox-Reset (auch wenn session noch >0 ist)
    Fall B – start steigt von ~0  : Wallbox hat Flash-Wert wiederhergestellt
                                    → ggf. Erststart-Bootstrap nachholen
    """
    try:
        new_start = float(value)
        old_start = float(old_value) if old_value not in (None, "unavailable") else 0.0
        delta     = new_start - old_start
    except (ValueError, TypeError):
        return

    # ── Fall A: start bricht ein → Reset-Modus aktivieren ────────────────────
    if new_start < RESET_DETECTION_THRESHOLD and old_start > 50.0:
        if _state["charging"]:
            log.warning(
                f"ebox_meter: start-Einbruch waehrend Ladevorgang: "
                f"{old_start:.3f} → {new_start:.3f} – Reset-Modus aktiv."
            )
        _state["charging"]         = False
        _state["session_progress"] = 0.0
        _state["wallbox_resetting"] = True
        _state["prev_start"]       = new_start
        _publish()
        return

    # ── Fall B: Wallbox meldet Flash-Wert zurueck ─────────────────────────────
    if old_start < RESET_DETECTION_THRESHOLD and new_start > 50.0:
        _state["wallbox_resetting"] = False

        if _state["bootstrap_pending"]:
            # Erststart-Bootstrap jetzt nachholen
            sess_now = _get_float(SENSOR_SESSION) or 0.0
            baseline = new_start + max(0.0, sess_now)
            _state["cumulative_committed"] = baseline
            _state["bootstrap_pending"]    = False
            _persist()
            log.info(
                f"ebox_meter: Bootstrap nach Restore: "
                f"start={new_start:.3f} + session={max(0.0, sess_now):.3f} "
                f"= {baseline:.3f} kWh"
            )
        else:
            log.info(
                f"ebox_meter: Wallbox-Restore abgeschlossen: "
                f"start={new_start:.3f} kWh – warte auf Ladestart."
            )
        _state["prev_start"] = new_start
        _publish()
        return

    # ── Normaler Commit-Sprung → nur loggen ──────────────────────────────────
    if abs(delta) > 0.01:
        log.info(
            f"ebox_meter: START committed "
            f"{old_start:.3f} → {new_start:.3f} kWh (Delta={delta:.3f})"
        )
    _state["prev_start"] = new_start


# =============================================================================
# SERVICES
# =============================================================================

@service
def ebox_meter_reset_session():
    """
    Setzt nur den Session-Fortschritt zurueck (z.B. nach Sensor-Fehler).
    Der kumulierte Wert bleibt unveraendert.

    Aufruf: service: pyscript.ebox_meter_reset_session
    """
    _state["charging"]         = False
    _state["session_progress"] = 0.0
    _state["register_offset"]  = 0.0
    _state["wallbox_resetting"] = False
    _publish()
    log.warning("ebox_meter: Session manuell zurueckgesetzt.")


@service
def ebox_meter_set_baseline(kwh=None):
    """
    Setzt den absoluten Basiswert manuell.
    Nuetzlich wenn das Script nach vielen Ladezyklen erstmals installiert
    wird und der automatische Bootstrap nicht den gewuenschten Startwert hat.

    Aufruf:  service: pyscript.ebox_meter_set_baseline
             data:
               kwh: 1234.567

    ACHTUNG: Ueberschreibt den gespeicherten kumulierten Wert!
    """
    if kwh is None:
        log.error("ebox_meter: set_baseline benoetigt Parameter 'kwh'.")
        return
    try:
        new_base = float(kwh)
    except (ValueError, TypeError):
        log.error(f"ebox_meter: set_baseline – ungueltiger Wert: {kwh}")
        return

    old = _state["cumulative_committed"]
    _state["cumulative_committed"] = new_base
    _state["bootstrap_pending"]    = False
    _persist()
    _publish()
    log.warning(
        f"ebox_meter: Basiswert manuell gesetzt: "
        f"{old:.3f} → {new_base:.3f} kWh"
    )


@service
def ebox_meter_force_resync():
    """
    Erzwingt Neu-Synchronisation aus aktuellen Sensor-Werten.
    Nuetzlich nach HA-Neustart, wenn eine Ladesession gerade aktiv war.

    Aufruf: service: pyscript.ebox_meter_force_resync
    """
    sess  = _get_float(SENSOR_SESSION)
    start = _get_float(SENSOR_START)
    if sess is None or start is None:
        log.warning("ebox_meter: Resync fehlgeschlagen – Sensoren nicht verfuegbar.")
        return

    reg = _register_calc()

    if start < RESET_DETECTION_THRESHOLD:
        _state["charging"]         = False
        _state["session_progress"] = 0.0
        _state["wallbox_resetting"] = True
    elif sess < -SESSION_START_THRESHOLD:
        offset = _get_float(HELPER_REG_OFFSET) or (reg or 0.0)
        _state["charging"]         = True
        _state["wallbox_resetting"] = False
        _state["register_offset"]  = offset
        _state["session_progress"] = max(0.0, (reg or 0.0) - offset)
    elif sess > 0:
        _state["charging"]         = False
        _state["wallbox_resetting"] = False
    else:
        _state["charging"]         = False
        _state["wallbox_resetting"] = False

    _state["prev_session"] = sess
    _state["prev_start"]   = start
    _publish()
    log.info(
        f"ebox_meter: Resync abgeschlossen | "
        f"session={sess:.3f} | charging={_state['charging']} | "
        f"resetting={_state['wallbox_resetting']}"
    )
