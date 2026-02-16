import socket
import struct

# ============================================================================
# KONFIGURATION
# ============================================================================
SOLIS_IP = "192.168.178.105"
SOLIS_PORT = 502
UNIT_ID = 1

# Register-Bereiche (0-based)
START_TABLE_4 = 33029  # Start PV Erträge
COUNT_TABLE_4 = 52     # Bis 33080 (AC Leistung)

START_TABLE_3 = 33151  # Start Netzleistung
COUNT_TABLE_3 = 29     # Bis 33179 (Hausverbrauch Heute)

def decode_s32(high, low):
    """Konvertiert zwei 16-Bit Register in eine S32 (vorzeichenbehaftet)"""
    val = (high << 16) | low
    return val if val < 0x80000000 else val - 0x100000000

@time_trigger("period(0, 30s)")
def task_solis_pv():
    # -------------------------------------------------------------------------
    # 1. PV ERTRÄGE, STRINGS & LEISTUNG
    # -------------------------------------------------------------------------
    pdu4 = struct.pack('>HHHBBHH', 1, 0, 6, UNIT_ID, 4, START_TABLE_4, COUNT_TABLE_4)
    data4 = call_solis_modbus(pdu4, COUNT_TABLE_4)

    if data4 and len(data4) >= 52:
        # PV Erträge (kWh)
        state.set("sensor.solis_raw_pv_total_yield", value=(data4[0] << 16) | data4[1]) # 33029
        state.set("sensor.solis_raw_pv_month_yield", value=(data4[2] << 16) | data4[3]) # 33031
        state.set("sensor.solis_raw_pv_year_yield", value=(data4[8] << 16) | data4[9])  # 33037
        state.set("sensor.solis_raw_pv_today_yield", value=round(data4[6] * 0.1, 1))    # 33035

        # PV Strings (Watt) - Berechnung: (Volt * 0.1) * (Ampere * 0.1)
        state.set("sensor.solis_raw_pv_p1", value=round((data4[20] * data4[21]) * 0.01, 0)) # 33049/50
        state.set("sensor.solis_raw_pv_p2", value=round((data4[22] * data4[23]) * 0.01, 0)) # 33051/52
        state.set("sensor.solis_raw_pv_p3", value=round((data4[24] * data4[25]) * 0.01, 0)) # 33053/54
        state.set("sensor.solis_raw_pv_p4", value=round((data4[26] * data4[27]) * 0.01, 0)) # 33055/56

        # Inverter Leistungen (Watt)
        state.set("sensor.solis_raw_pv_dc_power", value=(data4[28] << 16) | data4[29])    # 33057
        state.set("sensor.solis_raw_pv_ac_power", value=decode_s32(data4[50], data4[51])) # 33079

    # -------------------------------------------------------------------------
    # 2. NETZ & HAUS-ERTRÄGE
    # -------------------------------------------------------------------------
    pdu3 = struct.pack('>HHHBBHH', 1, 0, 6, UNIT_ID, 4, START_TABLE_3, COUNT_TABLE_3)
    data3 = call_solis_modbus(pdu3, COUNT_TABLE_3)

    if data3 and len(data3) >= 29:
        # Netzleistung aktuell (Watt)
        state.set("sensor.solis_raw_grid_power", value=decode_s32(data3[0], data3[1])) # 33151

        # Netz Zähler (kWh)
        state.set("sensor.solis_raw_grid_import_total", value=(data3[18] << 16) | data3[19]) # 33169
        state.set("sensor.solis_raw_grid_import_today", value=round(data3[20] * 0.1, 1))      # 33171
        state.set("sensor.solis_raw_grid_export_total", value=(data3[22] << 16) | data3[23]) # 33173
        state.set("sensor.solis_raw_grid_export_today", value=round(data3[24] * 0.1, 1))      # 33175

        # Hausverbrauch Zähler (kWh)
        state.set("sensor.solis_raw_house_total", value=(data3[26] << 16) | data3[27])       # 33177
        state.set("sensor.solis_raw_house_today", value=round(data3[28] * 0.1, 1))           # 33179

def call_solis_modbus(pdu, count):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4.0)
    try:
        s.connect((SOLIS_IP, SOLIS_PORT))
        s.send(pdu)
        resp = s.recv(1024)
        s.close()
        if len(resp) == 9 + (count * 2):
            return struct.unpack(f'>{count}H', resp[9:])
    except:
        if s: s.close()
    return None
