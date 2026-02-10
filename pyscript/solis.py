import socket
import struct
import time

# --- KONFIGURATION ---
SOLIS_IP = "192.168.178.105"
SOLIS_PORT = 502
POLL_INTERVAL = 60
ID_PREFIX = "solis_raw"

def get_solis_data(ip, port, unit_id, func_code, start_reg, count):
    """Präzise Modbus-TCP Abfrage mit Absicherung"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect((ip, port))
        # PDU zusammenbauen
        pdu = struct.pack('>HHHBBHH', 1, 0, 6, unit_id, func_code, start_reg, count)
        s.send(pdu)
        data = s.recv(1024)
        if len(data) > 9 and data[7] < 0x80:
            return data[9:]
    except Exception as e:
        log.warning(f"Solis Verbindungsfehler bei Reg {start_reg}: {e}")
    finally:
        # Dieser Block sorgt dafür, dass die Verbindung IMMER geschlossen wird
        try:
            s.shutdown(socket.SHUT_RDWR)
            s.close()
        except:
            pass
    return None

@time_trigger(f"period(0, {POLL_INTERVAL}s)")
def task_solis_poll():
    def set_raw(name, val):
        state.set(f"sensor.{ID_PREFIX}_{name}", value=val)

    # BLOCK 1: AC & PV (Start 33071)
    res_ac = get_solis_data(SOLIS_IP, SOLIS_PORT, 1, 4, 33071, 20)
    if res_ac and len(res_ac) >= 24:
        # AC Power: Reg 33079 (Offset 16)
        ac_p = float(struct.unpack('>i', res_ac[16:20])[0])
        set_raw("ac_p_total", ac_p)
        # PV Power: Reg 33081 (Offset 20)
        pv_p_raw = float(struct.unpack('>I', res_ac[20:24])[0])
        #set_raw("pv_power_total", pv_p)
        # Wenn der Wert größer als 15.000 (15kW) ist, ist es ein Modbus-Fehler (Gigawatt-Peak)
        # In diesem Fall setzen wir ihn hart auf 0 (da es meist nachts passiert)
        if pv_p_raw > 15000:
            pv_p = 0.0
        else:
            pv_p = float(pv_p_raw)
        set_raw("pv_power_total", pv_p)

    task.sleep(1.5)

    # BLOCK 2: BATTERIE & SOC (Start 33133)
    # Wir lesen hier 8 Register, das deckt Spannung, Strom UND SOC (33139) ab
    res_batt = get_solis_data(SOLIS_IP, SOLIS_PORT, 1, 4, 33133, 8)
    if res_batt and len(res_batt) >= 14:
        # Spannung (33133) & Strom (33134)
        v = int.from_bytes(res_batt[0:2], 'big') / 10
        i = struct.unpack('>h', res_batt[2:4])[0] / 10
        p = round(v * i, 1)
        set_raw("batt_p", p)

        # NEUER SOC VERSUCH: Register 33139 (Offset 12 Bytes)
        # In vielen Solis-Dokumenten liegt der Hybrid-SOC hier
        soc = int.from_bytes(res_batt[12:14], 'big')
        set_raw("batt_soc", soc)

        if p > 20: status = "Entladen"
        elif p < -20: status = "Laden"
        else: status = "Standby"
        set_raw("batt_status", status)

    task.sleep(1.5)

    # BLOCK 3: ERTRÄGE (Start 33008)
    res_yield = get_solis_data(SOLIS_IP, SOLIS_PORT, 1, 4, 33008, 10)
    if res_yield and len(res_yield) >= 20:
        set_raw("yield_day", int.from_bytes(res_yield[6:8], 'big') / 10)
        set_raw("yield_total", int.from_bytes(res_yield[0:4], 'big'))
