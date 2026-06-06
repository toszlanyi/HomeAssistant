import socket
import struct
import time

# ============================================================================
# CONFIGURATION
# ============================================================================
WAVESHARE_IP   = "192.168.178.24"
WAVESHARE_PORT = 502
EASTRON_ID     = 0x01

# Phantom-Schwelle: Phasenleistung muss diesen Wert überschreiten,
# damit das Vorzeichen als "aktiv" gilt (Rauschfilter)
# 0 = kein Filter, empfohlen: 0-10W
PHANTOM_THRESHOLD_W = 1

# Maximale Gesamtabweichung (Summe p1+p2+p3), bei der noch von Phantomstrom
# ausgegangen wird. Überschreitet die Summe diesen Wert, liegt echter
# Netzbezug oder -einspeisung vor.
PHANTOM_MAX_TOTAL_W = 100

# Sniff duration — slightly less than the trigger interval of 20s
SNIFF_DURATION = 15.0


# ============================================================================
# OFFSET-TRACKING für Phantom-Energie (Version 4)
# ============================================================================

_phantom_was_active  = False
_e_imp_phantom_start = 0.0
_e_exp_phantom_start = 0.0
_e_imp_offset        = 0.0
_e_exp_offset        = 0.0
_e_imp_last_good     = 0.0
_e_exp_last_good     = 0.0
_offset_initialized  = False

# Fix 1: Phantom-Erkennung erst aktiv wenn alle drei Phasen mindestens
# einmal einen echten Messwert hatten
_phases_initialized  = False
_p1_seen = False
_p2_seen = False
_p3_seen = False


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def is_phantom(p1, p2, p3):
    """
    Phantom-Erkennung über Phasen-Vorzeichen und Gesamtsumme.
    Gibt True zurück wenn Phantomstrom erkannt wird, sonst False.
    """
    positiv = len([p for p in [p1, p2, p3] if p > PHANTOM_THRESHOLD_W])
    negativ = len([p for p in [p1, p2, p3] if p < -PHANTOM_THRESHOLD_W])

    # Bedingung 1: Gemischte Vorzeichen auf den Phasen
    mixed_signs = (positiv > 0 and negativ > 0)

    if not mixed_signs:
        return False

    # Bedingung 2: Die Summe aller Phasen ist nahe 0
    return abs(p1 + p2 + p3) <= PHANTOM_MAX_TOTAL_W


# ============================================================================
# NETWORK I/O — runs in thread executor to avoid blocking the event loop.
# Passive sniffer: collects all RS485 traffic mirrored by the Waveshare adapter.
# The inner receive loop with short timeouts must run in native Python.
# ============================================================================

@pyscript_executor
def _sniff_raw_data(duration=SNIFF_DURATION):
    """
    Opens a TCP connection to the Waveshare RS485-to-ETH adapter and collects
    all mirrored RS485 bus traffic for the given duration.
    Returns the raw byte buffer, or None on connection failure.
    """
    buffer = b""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)  # Timeout für den Verbindungsaufbau
        s.connect((WAVESHARE_IP, WAVESHARE_PORT))

        start_time = time.time()
        s.settimeout(0.005)  # Kurzer Intervall-Timeout für die Loop

        while (time.time() - start_time) < duration:
            try:
                # Alles empfangen, was der Waveshare auf den Bus spiegelt
                chunk = s.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            except socket.timeout:
                continue

    except Exception:
        return None
    finally:
        if s:
            s.close()

    return buffer


# ============================================================================
# STARTUP — letzte bekannte korrigierte Werte aus HA laden
# ============================================================================

@time_trigger("startup")
def init_on_startup():
    """
    Beim Start letzte bekannte korrigierte Werte aus HA laden.
    Wartet 10s damit HA Zeit hat, alle States zu laden.
    """
    global _e_imp_last_good, _e_exp_last_good, _offset_initialized

    task.sleep(10)

    try:
        imp = state.get('sensor.eastron_raw_e_imp')
        exp = state.get('sensor.eastron_raw_e_exp')

        # Fix 2: offset_initialized bleibt False bis Startup erfolgreich war.
        # Nur wenn echte Werte geladen werden, wird last_good gesetzt und
        # der Offset beim ersten Messwert korrekt berechnet.
        if imp not in (None, 'unknown', 'unavailable'):
            _e_imp_last_good = float(imp)
            log.info(f"Startup: e_imp initialisiert mit {_e_imp_last_good} kWh")
        else:
            log.warning("Startup: sensor.eastron_raw_e_imp nicht verfügbar – starte mit 0")

        if exp not in (None, 'unknown', 'unavailable'):
            _e_exp_last_good = float(exp)
            log.info(f"Startup: e_exp initialisiert mit {_e_exp_last_good} kWh")
        else:
            log.warning("Startup: sensor.eastron_raw_e_exp nicht verfügbar – starte mit 0")

    except Exception as e:
        log.error(f"Startup Init Fehler: {e}")


# ============================================================================
# MAIN TASK
# ============================================================================

@time_trigger("period(10, 15)")
def process_eastron_data():
    """
    Triggered every 15 seconds. Sniffs RS485 bus traffic between the Solis
    inverter and the Eastron smart meter, parses Modbus RTU request/response
    frame pairs, applies phantom power detection and offset tracking, and
    writes corrected values to Home Assistant sensor states.
    """
    global _phantom_was_active
    global _e_imp_phantom_start, _e_exp_phantom_start
    global _e_imp_offset, _e_exp_offset
    global _e_imp_last_good, _e_exp_last_good
    global _offset_initialized
    global _phases_initialized, _p1_seen, _p2_seen, _p3_seen

    buffer = _sniff_raw_data()

    if not buffer or len(buffer) < 8:
        return

    stats = {
        "u1": [], "u2": [], "u3": [],
        "i1": [], "i2": [], "i3": [],
        "p1": [], "p2": [], "p3": [],
        "p_tot": [], "e_imp": [], "e_exp": []
    }

    i = 0
    while i < len(buffer) - 12:
        if buffer[i] == EASTRON_ID and buffer[i+1] == 0x04:
            try:
                reg_start = struct.unpack('>H', buffer[i+2:i+4])[0]
                reg_count = struct.unpack('>H', buffer[i+4:i+6])[0]
                byte_count_expected = reg_count * 2

                for j in range(i + 6, i + 15):
                    if j + 3 + byte_count_expected <= len(buffer):
                        if buffer[j] == EASTRON_ID and buffer[j+1] == 0x04 and buffer[j+2] == byte_count_expected:
                            payload = buffer[j+3 : j+3+byte_count_expected]

                            # --- PHASEN-LOGIK ---
                            if reg_start == 0 and byte_count_expected == 12:
                                v = struct.unpack('>fff', payload)
                                stats["u1"].append(v[0]); stats["u2"].append(v[1]); stats["u3"].append(v[2])
                            elif reg_start == 6 and byte_count_expected == 12:
                                v = struct.unpack('>fff', payload)
                                stats["i1"].append(v[0]); stats["i2"].append(v[1]); stats["i3"].append(v[2])
                            elif reg_start == 12 and byte_count_expected == 12:
                                v = struct.unpack('>fff', payload)
                                stats["p1"].append(v[0]); stats["p2"].append(v[1]); stats["p3"].append(v[2])
                            elif reg_start == 0 and byte_count_expected >= 36:
                                v = struct.unpack('>fffffffff', payload[0:36])
                                stats["u1"].append(v[0]); stats["u2"].append(v[1]); stats["u3"].append(v[2])
                                stats["i1"].append(v[3]); stats["i2"].append(v[4]); stats["i3"].append(v[5])
                                stats["p1"].append(v[6]); stats["p2"].append(v[7]); stats["p3"].append(v[8])

                            # --- FLEXIBLE LOGIK FÜR ENERGIE & P-TOTAL ---
                            # Prüft, ob die Ziel-Register im angefragten Bereich liegen
                            for target_reg, target_key in [(52, "p_tot"), (72, "e_exp"), (74, "e_imp")]:
                                if reg_start <= target_reg < reg_start + reg_count:
                                    # Jedes Register belegt 2 Bytes, Offset berechnen
                                    byte_offset = (target_reg - reg_start) * 2
                                    if len(payload) >= byte_offset + 4:
                                        val = struct.unpack('>f', payload[byte_offset:byte_offset+4])[0]
                                        stats[target_key].append(val)

                            i = j + 3 + byte_count_expected
                            break
            except Exception:
                # Fängt Fehler beim Entpacken (struct.error) ab, ohne das Skript abstürzen zu lassen
                pass
        i += 1

    # ========================================================================
    # PHANTOM-ERKENNUNG auf Basis der gemittelten Phasenleistungen
    # ========================================================================

    avg_p1 = sum(stats["p1"]) / len(stats["p1"]) if stats["p1"] else None
    avg_p2 = sum(stats["p2"]) / len(stats["p2"]) if stats["p2"] else None
    avg_p3 = sum(stats["p3"]) / len(stats["p3"]) if stats["p3"] else None

    # Fix 1: Phasen-Initialisierung tracken — erst wenn alle drei Phasen
    # mindestens einen echten Messwert hatten, ist die Erkennung valide
    if avg_p1 is not None:
        _p1_seen = True
    if avg_p2 is not None:
        _p2_seen = True
    if avg_p3 is not None:
        _p3_seen = True

    if not _phases_initialized:
        if _p1_seen and _p2_seen and _p3_seen:
            _phases_initialized = True
            log.info("Eastron: Alle Phasen initialisiert, Phantom-Erkennung aktiv")
        else:
            log.debug("Eastron: Warte auf Initialisierung aller Phasen")

    # Fallback auf 0.0 nur für state.set — nicht für Phantom-Erkennung
    p1 = avg_p1 if avg_p1 is not None else 0.0
    p2 = avg_p2 if avg_p2 is not None else 0.0
    p3 = avg_p3 if avg_p3 is not None else 0.0

    phantom = _phases_initialized and is_phantom(p1, p2, p3)

    state.set("sensor.eastron_raw_phantom_active",
              value=1 if phantom else 0,
              attributes={
                  'p1': round(p1, 1),
                  'p2': round(p2, 1),
                  'p3': round(p3, 1),
                  'p_tot_calc': round(p1 + p2 + p3, 1),
                  'phases_ready': _phases_initialized,
              })

    # ========================================================================
    # OFFSET-TRACKING: Phantom-Energie dauerhaft herausrechnen
    # ========================================================================

    e_imp_raw = sum(stats["e_imp"]) / len(stats["e_imp"]) if stats["e_imp"] else None
    e_exp_raw = sum(stats["e_exp"]) / len(stats["e_exp"]) if stats["e_exp"] else None

    if e_imp_raw is not None and e_exp_raw is not None:

        # Fix 2: Offset erst initialisieren wenn Startup-Werte geladen wurden.
        # _e_imp_last_good und _e_exp_last_good sind nach init_on_startup
        # entweder die letzten bekannten HA-Werte oder 0.0 (kein HA-Wert).
        # In beiden Fällen ist der Offset nach dem ersten Messwert korrekt.
        if not _offset_initialized:
            _e_imp_offset = e_imp_raw - _e_imp_last_good
            _e_exp_offset = e_exp_raw - _e_exp_last_good
            _offset_initialized = True
            log.info(
                f"Offset initialisiert: imp={_e_imp_offset:.2f} kWh, "
                f"exp={_e_exp_offset:.2f} kWh"
            )

        # Phantom startet
        if phantom and not _phantom_was_active:
            _e_imp_phantom_start = e_imp_raw
            _e_exp_phantom_start = e_exp_raw
            log.debug(
                f"Phantom START – e_imp={e_imp_raw:.2f}, e_exp={e_exp_raw:.2f} "
                f"(p1={round(p1)}W, p2={round(p2)}W, p3={round(p3)}W)"
            )

        # Phantom endet
        if not phantom and _phantom_was_active:
            delta_imp = e_imp_raw - _e_imp_phantom_start
            delta_exp = e_exp_raw - _e_exp_phantom_start
            _e_imp_offset += delta_imp
            _e_exp_offset += delta_exp
            log.debug(
                f"Phantom ENDE – Delta e_imp={delta_imp:.3f} kWh, "
                f"e_exp={delta_exp:.3f} kWh → "
                f"Offset gesamt: imp={_e_imp_offset:.3f}, exp={_e_exp_offset:.3f}"
            )

        _phantom_was_active = phantom

        if phantom:
            state.set("sensor.eastron_raw_e_imp", value=_e_imp_last_good)
            state.set("sensor.eastron_raw_e_exp", value=_e_exp_last_good)
        else:
            _e_imp_last_good = round(e_imp_raw - _e_imp_offset, 2)
            _e_exp_last_good = round(e_exp_raw - _e_exp_offset, 2)
            state.set("sensor.eastron_raw_e_imp", value=_e_imp_last_good)
            state.set("sensor.eastron_raw_e_exp", value=_e_exp_last_good)

    # ========================================================================
    # ALLE ANDEREN SENSOREN SCHREIBEN
    # ========================================================================

    for key, values in stats.items():
        if not values:
            continue

        if key in ("e_imp", "e_exp"):
            continue

        avg_val = sum(values) / len(values)

        if phantom and key == "p_tot":
            state.set("sensor.eastron_raw_p_tot", value=0.0)
            continue

        state.set(f"sensor.eastron_raw_{key}", value=round(avg_val, 2))

        if key in ["u1", "u2", "u3"]:
            state.set(f"sensor.eastron_raw_{key}_min", value=round(min(values), 2))

    del buffer
