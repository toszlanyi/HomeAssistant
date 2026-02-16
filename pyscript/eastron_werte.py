import sys
import struct

MODULE_PATH = "/config/pyscript_modules"
if MODULE_PATH not in sys.path:
    sys.path.append(MODULE_PATH)

import eastron_driver

EASTRON_ID = 0x01

@time_trigger("period(0, 10)")
async def process_eastron_data():
    buffer = await task.executor(eastron_driver.get_raw_data, duration=5.0)
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
                            for target_reg, target_key in [(52, "p_tot"), (72, "e_imp"), (74, "e_exp")]:
                                if reg_start <= target_reg < reg_start + reg_count:
                                    # Jedes Register belegt 2 Bytes, Offset berechnen
                                    byte_offset = (target_reg - reg_start) * 2
                                    if len(payload) >= byte_offset + 4:
                                        val = struct.unpack('>f', payload[byte_offset:byte_offset+4])[0]
                                        stats[target_key].append(val)

                            i = j + 3 + byte_count_expected
                            break
            except: pass
        i += 1

    # Sensoren schreiben (Raw-Output für YAML)
    for key, values in stats.items():
        if values:
            avg_val = sum(values) / len(values)
            state.set(f"sensor.eastron_raw_{key}", value=round(avg_val, 3))
            if key in ["u1", "u2", "u3"]:
                state.set(f"sensor.eastron_raw_{key}_min", value=round(min(values), 3))

    del buffer
