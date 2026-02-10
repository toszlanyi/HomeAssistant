import socket
import struct
import time

# ============================================================================
# KONFIGURATION
# ============================================================================
SOLIS_IP = "192.168.178.105"
SOLIS_PORT = 502
POLL_INTERVAL = 60
SLEEP_INTERVAL = 1.5  # Wartezeit zwischen den Datenblöcken
ID_PREFIX = "solis_raw"

# Modbus Register-Definitionen
REGISTER_AC_PV = 33071       # Start: AC Power & PV Power
REGISTER_BATTERY = 33133     # Start: Battery Voltage, Current, SOC
REGISTER_YIELD = 33008       # Start: Daily & Total Yield

# ============================================================================
# MODBUS-TCP KOMMUNIKATION
# ============================================================================

def get_solis_data(ip, port, unit_id, func_code, start_reg, count):
    """
    Modbus-TCP Holding Registers auslesen.
    
    Args:
        ip (str): IP-Adresse des Solis-Wechselrichters
        port (int): Modbus-Port (Standard: 502)
        unit_id (int): Modbus Unit ID (Standard: 1)
        func_code (int): Modbus Funktionscode (3=Holding Reg, 4=Input Reg)
        start_reg (int): Start-Register
        count (int): Anzahl Register zum Auslesen
    
    Returns:
        bytes: Rohdaten ab Byte 9 (nach Modbus-Header), None bei Fehler
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect((ip, port))
        # Modbus-TCP PDU: Transaction ID, Protocol ID, Length, Unit ID, Func, Start Reg, Count
        pdu = struct.pack('>HHHBBHH', 1, 0, 6, unit_id, func_code, start_reg, count)
        s.send(pdu)
        data = s.recv(1024)
        
        # Validierung: Länge > 9 Bytes und kein Error-Flag (data[7] < 0x80)
        if len(data) > 9 and data[7] < 0x80:
            return data[9:]
    except Exception as e:
        log.warning(f"Solis Verbindungsfehler bei Register {start_reg}: {e}")
    finally:
        # Socket immer sauber schließen
        try:
            s.shutdown(socket.SHUT_RDWR)
            s.close()
        except:
            pass
    
    return None


# ============================================================================
# HAUPTAUFGABE: Periodisches Auslesen der Solis-Daten
# ============================================================================

@time_trigger(f"period(0, {POLL_INTERVAL}s)")
def task_solis_poll():
    """Liest alle 60s Daten aus dem Solis-Wechselrichter und speichert sie als Sensoren."""
    
    def set_raw(name, val):
        """Hilfsfunktion: Sensor-State in Home Assistant setzen."""
        state.set(f"sensor.{ID_PREFIX}_{name}", value=val)
    
    # --- BLOCK 1: AC Power & PV Power ---
    res_ac = get_solis_data(SOLIS_IP, SOLIS_PORT, 1, 4, REGISTER_AC_PV, 20)
    if res_ac and len(res_ac) >= 24:
        # AC Power: Register 33079 = 16 Bytes Offset
        ac_p = float(struct.unpack('>i', res_ac[16:20])[0])
        set_raw("ac_p_total", ac_p)
        
        # PV Power: Register 33081 = 20 Bytes Offset
        # Fehlerbehandlung: Werte > 15kW sind Modbus-Fehler, auf 0 setzen
        pv_p_raw = float(struct.unpack('>I', res_ac[20:24])[0])
        pv_p = 0.0 if pv_p_raw > 15000 else pv_p_raw
        set_raw("pv_power_total", pv_p)
    
    task.sleep(SLEEP_INTERVAL)
    
    # --- BLOCK 2: Battery Voltage, Current & SOC ---
    res_batt = get_solis_data(SOLIS_IP, SOLIS_PORT, 1, 4, REGISTER_BATTERY, 8)
    if res_batt and len(res_batt) >= 14:
        # Spannung (33133): Bytes 0-2, geteilt durch 10
        # Strom (33134): Bytes 2-4, geteilt durch 10
        v = int.from_bytes(res_batt[0:2], 'big') / 10
        i = struct.unpack('>h', res_batt[2:4])[0] / 10
        p = round(v * i, 1)
        set_raw("batt_p", p)
        
        # SOC: Register 33139 = 12 Bytes Offset
        soc = int.from_bytes(res_batt[12:14], 'big')
        set_raw("batt_soc", soc)
        
        # Status aus Leistung ableiten
        if p > 20:
            status = "Entladen"
        elif p < -20:
            status = "Laden"
        else:
            status = "Standby"
        set_raw("batt_status", status)
    
    task.sleep(SLEEP_INTERVAL)
    
    # --- BLOCK 3: Tages- & Gesamtertrag ---
    res_yield = get_solis_data(SOLIS_IP, SOLIS_PORT, 1, 4, REGISTER_YIELD, 10)
    if res_yield and len(res_yield) >= 20:
        # Tagesertrag (33014): Bytes 6-8, geteilt durch 10
        set_raw("yield_day", int.from_bytes(res_yield[6:8], 'big') / 10)
        
        # Gesamtertrag (33008): Bytes 0-4
        set_raw("yield_total", int.from_bytes(res_yield[0:4], 'big'))