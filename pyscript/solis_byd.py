import socket
import struct
import time

# ============================================================================
#    KONFIGURATION
# ============================================================================
SOLIS_IP    = "192.168.178.105"
SOLIS_PORT  = 502
UNIT_ID     = 1
QUERY_DELAY = 0.35 # 300ms acc to Modbus Spec

# Exponential Backoff (wie TCP/IP es macht)
BASE_RETRY_DELAY = 1.0      # Startet mit 1 Sek
MAX_RETRY_DELAY = 60.0     # Max 1 Min
BACKOFF_MULTIPLIER = 2.0    # Verdoppelt bei jedem Fehler

# Globale State
_current_retry_delay = BASE_RETRY_DELAY
_consecutive_failures = 0
_last_success_time = 0


# ============================================================================
# REGISTER-BLÖCKE
# ============================================================================
CHUNK_A_START = 33029
CHUNK_A_COUNT = 30

CHUNK_B_START = 33079
CHUNK_B_COUNT = 2

CHUNK_C_START = 33133
CHUNK_C_COUNT = 47


# ============================================================================
# HILFSFUNKTIONEN
# ============================================================================

def build_pdu(trans_id, start, count):
    return struct.pack('>HHHBBHH', trans_id, 0, 6, UNIT_ID, 4, start, count)

def recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Verbindung getrennt")
        buf += chunk
    return buf

def query(sock, trans_id, start, count):
    sock.sendall(build_pdu(trans_id, start, count))
    resp = recv_exact(sock, 9 + count * 2)
    if resp[7] != 4:
        raise ValueError(f"Unerwarteter FC: {resp[7]:#04x}")
    return struct.unpack(f'>{count}H', resp[9:])

def decode_s32(high, low):
    val = (high << 16) | low
    return val if val < 0x80000000 else val - 0x100000000


# ============================================================================
# HAUPT-TASK
# ============================================================================

@time_trigger("period(0, 30s)")
def task_solis_all():

    # === Keine Cloud-Vorhersage. Bei Fehler: Exponential Backoff.===

    global _current_retry_delay, _consecutive_failures, _last_success_time

    # === Check: Sind wir gerade im Backoff? ===
#    if _consecutive_failures > 0:
#        time_since_last_try = time.time() - _last_success_time
#        if time_since_last_try < _current_retry_delay:
#            remaining = int(_current_retry_delay - time_since_last_try)
#            log.debug(f"Backoff aktiv (noch {remaining}s)")
#            return

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)

    try:
        s.connect((SOLIS_IP, SOLIS_PORT))

        # === ABFRAGEN ===
        a = query(s, 1, CHUNK_A_START, CHUNK_A_COUNT)
        task.sleep(QUERY_DELAY)

        b = query(s, 2, CHUNK_B_START, CHUNK_B_COUNT)
        task.sleep(QUERY_DELAY)

        c = query(s, 3, CHUNK_C_START, CHUNK_C_COUNT)

        # === ERFOLG! Reset Backoff ===
        _consecutive_failures = 0
        _current_retry_delay = BASE_RETRY_DELAY
        _last_success_time = time.time()

        # === DATENVERARBEITUNG ===

        # PV Erträge
        state.set("sensor.solis_raw_pv_total_yield", value=(a[0] << 16) | a[1])
        state.set("sensor.solis_raw_pv_month_yield", value=(a[2] << 16) | a[3])
        state.set("sensor.solis_raw_pv_today_yield", value=round(a[6] * 0.1, 1))
        state.set("sensor.solis_raw_pv_year_yield", value=(a[8] << 16) | a[9])

        # Strings
        state.set("sensor.solis_raw_pv_p1", value=round((a[20] * a[21]) * 0.01, 0))
        state.set("sensor.solis_raw_pv_p2", value=round((a[22] * a[23]) * 0.01, 0))
        state.set("sensor.solis_raw_pv_p3", value=round((a[24] * a[25]) * 0.01, 0))
        state.set("sensor.solis_raw_pv_p4", value=round((a[26] * a[27]) * 0.01, 0))

        # PV Leistung
        state.set("sensor.solis_raw_pv_dc_power", value=(a[28] << 16) | a[29])
        state.set("sensor.solis_raw_pv_ac_power", value=decode_s32(b[0], b[1]))

        # Batterie
        v_raw = c[0]
        i_raw = c[1]
        dir_ = c[2]
        soc = c[6]
        soh = c[7]

        v_final = round(v_raw * 0.1, 1)
        factor = 1 if dir_ == 1 else -1
        i_final = round((i_raw * 0.1) * factor, 2)
        p_batt = round(v_final * i_final, 0)
        p_batt_direct = decode_s32(c[16], c[17])

        total_c = (c[28] << 16) | c[29]
        today_c = round(c[30] * 0.1, 1)
        total_d = (c[32] << 16) | c[33]
        today_d = round(c[34] * 0.1, 1)

        if (0 <= soc <= 100) and (v_final >= 100):
            state.set("sensor.solis_raw_batt_v", value=v_final)
            state.set("sensor.solis_raw_batt_i", value=i_final)
            state.set("sensor.solis_raw_batt_p", value=p_batt)
            state.set("sensor.solis_raw_batt_soc", value=soc)
            state.set("sensor.solis_raw_batt_soh", value=soh)
            state.set("sensor.solis_raw_batt_total_charge", value=total_c)
            state.set("sensor.solis_raw_batt_total_discharge", value=total_d)
            state.set("sensor.solis_raw_batt_today_charge", value=today_c)
            state.set("sensor.solis_raw_batt_today_discharge", value=today_d)
            state.set("sensor.solis_raw_batt_p_direct", value=p_batt_direct)

        # Netz & Haus
        state.set("sensor.solis_raw_house_load", value=c[14])
        state.set("sensor.solis_raw_grid_power", value=decode_s32(c[18], c[19]))

        state.set("sensor.solis_raw_grid_import_total", value=(c[36] << 16) | c[37])
        state.set("sensor.solis_raw_grid_import_today", value=round(c[38] * 0.1, 1))
        state.set("sensor.solis_raw_grid_export_total", value=(c[40] << 16) | c[41])
        state.set("sensor.solis_raw_grid_export_today", value=round(c[42] * 0.1, 1))

        state.set("sensor.solis_raw_house_total", value=(c[44] << 16) | c[45])
        state.set("sensor.solis_raw_house_today", value=round(c[46] * 0.1, 1))

        # Status
        state.set("sensor.solis_connection_status",
                 value="online",
                 attributes={
                     'last_success': time.strftime('%H:%M:%S'),
                     'failures': 0,
                     'next_retry': 'now'
                 })

    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        # === FEHLER: Exponential Backoff ===
        _consecutive_failures += 1
        _last_success_time = time.time()

        # Verdopple Wartezeit
        _current_retry_delay = min(
            _current_retry_delay * BACKOFF_MULTIPLIER,
            MAX_RETRY_DELAY
        )

        log.warning(
            f"S2 nicht erreichbar (Fehler #{_consecutive_failures}) - "
            f"Pause {int(_current_retry_delay)}s"
        )

        # Status
        state.set("sensor.solis_connection_status",
                 value="backoff",
                 attributes={
                     'last_error': str(e),
                     'failures': _consecutive_failures,
                     'next_retry': f"in {int(_current_retry_delay)}s"
                 })

    except Exception as e:
        log.error(f"Unerwarteter Fehler: {e}")

    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except:
            pass
        s.close()
