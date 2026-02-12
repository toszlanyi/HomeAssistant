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
    # Liest BYD Daten via Raw-Socket (umgeht Pymodbus-Probleme)
    # ============================================================================

    # Modbus PDU zusammenbauen:
    # Trans-ID (1), Prot-ID (0), Length (6), Unit (1), Func (4), Start (33133), Count (15)
    pdu = struct.pack('>HHHBBHH', 1, 0, 6, UNIT_ID, 4, 33133, 15)

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)

    try:
        s.connect((SOLIS_IP, SOLIS_PORT))
        s.send(pdu)
        response = s.recv(1024)

        # Modbus Response Header ist 9 Bytes lang. Die Daten kommen danach.
        if len(response) >= 9 + (15 * 2):

            # Wir schneiden den Header ab und interpretieren die Daten als 16 Unsigned Shorts
            # '>15H' bedeutet: Big Endian, 15 mal Unsigned Short (2 Byte pro Register)
            r = struct.unpack('>15H', response[9:9+30])

            # Mapping (Indizes basierend auf r[0] = 33133)
            # 0-based 33133 | 1-based 33134
            v_raw = r[0]   # 33134: Spannung
            i_raw = r[1]   # 33135: Strom
            dir   = r[2]   # 33136: Richtung (0=Lad, 1=Entl)
            soc   = r[6]   # 33140: SoC
            soh   = r[7]   # 33141: SoH
            house = r[14]  # 33148: Hausverbrauch

            # Berechnungen
            v_final = round(v_raw * 0.1, 1) # V kommt in Werten von 0,1
            factor = 1 if dir == 1 else -1 # Richtung des Stromflusses (für HA gedreht)
            i_final = round((i_raw * 0.1) * factor, 2) # I kommt in Werten von 0,1 und signed
            p_batt  = round(v_final * i_final, 0) # Batterieleistung (mit Richtung)

            # Plausibilitätsprüfung auf unstimminge Werte)
            is_valid = True

            if not (0 <= soc <= 100):
                log.warning(f"Solis: Unplausibler SOC ignoriert: {soc}%")
                is_valid = False

            if v_final < 40: # Ein BYD-Speicher ist unter 40V technisch "tot"
                log.warning(f"Solis: Unplausible Spannung ignoriert: {v_final}V")
                is_valid = False

            # Sensoren nur an HomeAssistant ausgeben, wenn die Daten valide sind
            if is_valid:
                state.set("sensor.solis_raw_batt_v", value=v_final)
                state.set("sensor.solis_raw_batt_i", value=i_final)
                state.set("sensor.solis_raw_batt_p", value=p_batt)
                state.set("sensor.solis_raw_batt_soc", value=soc)
                state.set("sensor.solis_raw_batt_soh", value=soh)
                state.set("sensor.solis_raw_house_load", value=house)
            else:
                log.error("Solis: Datensatz wegen Unplausibilität verworfen.")

        else:
            log.warning(f"Solis Socket: Unerwartete Antwortlänge ({len(response)})")

    except Exception as e:
        log.error(f"Solis Socket Fehler: {e}")

    finally:
        s.close()
