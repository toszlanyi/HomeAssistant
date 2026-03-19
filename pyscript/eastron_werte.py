import sys
import struct

MODULE_PATH = "/config/pyscript_modules"
if MODULE_PATH not in sys.path:
    sys.path.append(MODULE_PATH)

import eastron_driver

EASTRON_ID = 0x01

# Phantom-Schwelle: Phasenleistung muss diesen Wert überschreiten,
# damit das Vorzeichen als "aktiv" gilt (Rauschfilter)
# 0 = kein Filter, empfohlen: 0-10W
PHANTOM_THRESHOLD_W = 1

# ============================================================================
# OFFSET-TRACKING für Phantom-Energie (Version 3)
# ============================================================================
# Strategie:
#   - Während Phantom: letzten guten kWh-Wert einfrieren
#   - Beim Phantom-Ende: aufgelaufenes Delta zum Offset addieren
#   - Außerhalb Phantom: korrigierten Wert (raw - offset) schreiben
#
# Lebensdauer: RAM only → bei HA-Neustart einmaliger Sprung (bekannter Edge Case)
# ============================================================================

_phantom_was_active  = False
_e_imp_phantom_start = 0.0
_e_exp_phantom_start = 0.0
_e_imp_offset        = 0.0
_e_exp_offset        = 0.0
_e_imp_last_good     = 0.0
_e_exp_last_good     = 0.0


def is_phantom(p1, p2, p3):
    """
    Phantom-Erkennung über Phasen-Vorzeichen.

    Der Wechselrichter (Solis S6, Unbalanced Output=OFF) konvertiert DC
    symmetrisch auf alle 3 Phasen und balanciert um den Nullpunkt.
    Das führt zu gleichzeitiger Einspeisung und Bezug auf verschiedenen Phasen.
    → Wenn Vorzeichen gemischt (mind. 1 positiv UND mind. 1 negativ): Phantom.

    Vorzeichenkonvention Eastron: + = Bezug, - = Einspeisung
    """
    t = PHANTOM_THRESHOLD_W
    positiv = len([p for p in [p1, p2, p3] if p >  t])
    negativ = len([p for p in [p1, p2, p3] if p < -t])
    return positiv > 0 and negativ > 0


@time_trigger("period(0, 20)")
async def process_eastron_data():

    global _phantom_was_active
    global _e_imp_phantom_start, _e_exp_phantom_start
    global _e_imp_offset, _e_exp_offset
    global _e_imp_last_good, _e_exp_last_good

    buffer = await task.executor(eastron_driver.get_raw_data, duration=19.0)
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
                            for target_reg, target_key in [(52, "p_tot"), (72, "e_exp"), (74, "e_imp")]:
                                if reg_start <= target_reg < reg_start + reg_count:
                                    byte_offset = (target_reg - reg_start) * 2
                                    if len(payload) >= byte_offset + 4:
                                        val = struct.unpack('>f', payload[byte_offset:byte_offset+4])[0]
                                        stats[target_key].append(val)

                            i = j + 3 + byte_count_expected
                            break
            except: pass
        i += 1

    # ========================================================================
    # PHANTOM-ERKENNUNG auf Basis der gemittelten Phasenleistungen
    # ========================================================================

    avg_p1 = sum(stats["p1"]) / len(stats["p1"]) if stats["p1"] else 0.0
    avg_p2 = sum(stats["p2"]) / len(stats["p2"]) if stats["p2"] else 0.0
    avg_p3 = sum(stats["p3"]) / len(stats["p3"]) if stats["p3"] else 0.0

    phantom = is_phantom(avg_p1, avg_p2, avg_p3)

    state.set("sensor.eastron_raw_phantom_active",
              value=1 if phantom else 0,
              attributes={
                  'p1': round(avg_p1, 1),
                  'p2': round(avg_p2, 1),
                  'p3': round(avg_p3, 1)
              })

    # ========================================================================
    # OFFSET-TRACKING: Phantom-Energie dauerhaft herausrechnen
    #
    # Während Phantom:  letzten guten Wert einfrieren
    # Beim Phantom-Ende: aufgelaufenes Delta zum Offset addieren
    # Außerhalb Phantom: korrigierten Wert schreiben und als letzten guten merken
    # ========================================================================

    e_imp_raw = sum(stats["e_imp"]) / len(stats["e_imp"]) if stats["e_imp"] else None
    e_exp_raw = sum(stats["e_exp"]) / len(stats["e_exp"]) if stats["e_exp"] else None

    if e_imp_raw is not None and e_exp_raw is not None:

        # Phantom startet → Eastron-Stand zu Beginn merken
        if phantom and not _phantom_was_active:
            _e_imp_phantom_start = e_imp_raw
            _e_exp_phantom_start = e_exp_raw
            log.debug(
                f"Phantom START – e_imp={e_imp_raw:.2f}, e_exp={e_exp_raw:.2f} "
                f"(p1={round(avg_p1)}W, p2={round(avg_p2)}W, p3={round(avg_p3)}W)"
            )

        # Phantom endet → Delta während Phantom-Phase zum Offset addieren
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
            # Einfrieren: letzten guten Wert wiederholen
            state.set("sensor.eastron_raw_e_imp", value=_e_imp_last_good)
            state.set("sensor.eastron_raw_e_exp", value=_e_exp_last_good)
        else:
            # Korrigierten Wert schreiben und als letzten guten merken
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

        # e_imp/e_exp bereits oben behandelt
        if key in ("e_imp", "e_exp"):
            continue

        avg_val = sum(values) / len(values)

        # p_tot bei Phantom auf 0 setzen (Momentanwert, nicht einfrieren)
        if phantom and key == "p_tot":
            state.set("sensor.eastron_raw_p_tot", value=0.0)
            continue

        state.set(f"sensor.eastron_raw_{key}", value=round(avg_val, 2))

        if key in ["u1", "u2", "u3"]:
            state.set(f"sensor.eastron_raw_{key}_min", value=round(min(values), 2))

    del buffer

