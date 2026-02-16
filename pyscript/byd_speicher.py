import socket
import struct

# ============================================================================
# KONFIGURATION
# ============================================================================
SOLIS_IP = "192.168.178.105"
SOLIS_PORT = 502
UNIT_ID = 1

@time_trigger("period(0, 60s)")
def task_solis_battery():

    # ============================================================================
    # Liest BYD Daten vom Solis WR via Raw-Socket
    # ============================================================================

    # Modbus PDU zusammenbauen:
    # Trans-ID (1), Prot-ID (0), Length (6), Unit (1), Func (4), Start (33133), Count (35)
    pdu = struct.pack('>HHHBBHH', 1, 0, 6, UNIT_ID, 4, 33133, 35)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3.0)

    try:
        s.connect((SOLIS_IP, SOLIS_PORT))
        s.send(pdu)
        response = s.recv(1024)

        # Modbus Response Header ist 9 Bytes lang. Die Daten kommen danach.
        if len(response) >= 9 + (35 * 2):

            # Header abschneiden und die Daten als 16 Unsigned Shorts interpretieren
            # '>35H' bedeutet: Big Endian, 35 mal Unsigned Short (2 Byte pro Register)
            r = struct.unpack('>35H', response[9:9+70])

            # Mapping (Indizes basierend auf r[0] = 33133)
            v_raw               = r[0]   # 33134 | Spannung
            i_raw               = r[1]   # 33135 | Strom
            dir                 = r[2]   # 33136 | Richtung (0=Lad, 1=Entl)
            soc                 = r[6]   # 33140 | SoC
            soh                 = r[7]   # 33141 | SoH
            house               = r[14]  # 33148 | Hausverbrauch
            today_charge_raw    = r[30]  # 33163 | Heutige Ladeleistung
            today_discharge_raw = r[34]  # 33167 | Heutige Entladeleistung

            # U32 Werte bestehen aus zwei Registern (High-Word und Low-Word)
            total_c             = (r[28] << 16) | r[29]  # 33161 + 33162 | Gesamte Ladeleistung
            total_d             = (r[32] << 16) | r[33]  # 33165 + 33166 | Gesamte Entladeleistung

            # Berechnungen
            v_final = round(v_raw * 0.1, 1)               # Einheit 0.1 V
            factor = 1 if dir == 1 else -1                # Richtung des Stromflusses (für HA gedreht)
            i_final = round((i_raw * 0.1) * factor, 2)    # Einheit 0.1 A - gerichtet
            p_batt  = round(v_final * i_final, 0)         # Batterieleistung (mit Richtung)
            today_c = round(today_charge_raw * 0.1, 1)    # Einheit 0.1 kWh
            today_d = round(today_discharge_raw * 0.1, 1) # Einheit 0.1 kWh

            # Plausibilitätsprüfung auf unstimminge Werte)
            is_valid = True
            if not (0 <= soc <= 100) or v_final < 100:
                is_valid = False

            # Sensoren nur an HomeAssistant ausgeben, wenn die Daten valide sind
            if is_valid:
                state.set("sensor.solis_raw_batt_v", value=v_final)
                state.set("sensor.solis_raw_batt_i", value=i_final)
                state.set("sensor.solis_raw_batt_p", value=p_batt)
                state.set("sensor.solis_raw_batt_soc", value=soc)
                state.set("sensor.solis_raw_batt_soh", value=soh)
                state.set("sensor.solis_raw_house_load", value=house)
                state.set("sensor.solis_raw_batt_total_charge", value=total_c)
                state.set("sensor.solis_raw_batt_total_discharge", value=total_d)
                state.set("sensor.solis_raw_batt_today_charge", value=today_c)
                state.set("sensor.solis_raw_batt_today_discharge", value=today_d)
            else:
                log.error("Solis: Datensatz wegen Unplausibilität verworfen.")

        else:
            log.warning(f"Solis Socket: Unerwartete Antwortlänge ({len(response)})")

    except Exception as e:
        log.error(f"Solis Socket Fehler: {e}")

    finally:
        s.close()
