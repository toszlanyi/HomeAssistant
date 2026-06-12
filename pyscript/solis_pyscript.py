import sys
import time

# ============================================================================
# Reloads changes in solis_driver.py when pyscript integration is reloaded.
# ============================================================================

MODULE_PATH = "/config/pyscript_modules"
if MODULE_PATH not in sys.path:
    sys.path.append(MODULE_PATH)

import importlib
import solis_driver
importlib.reload(solis_driver)

# ============================================================================
# STATE
# ============================================================================

_is_running = False   # guard against overlapping cycles


# ============================================================================
# DECODE HELPERS
# u16/u32 = unsigned, s16/s32 = signed
# ============================================================================

def decode_u16(val):
    return val

def decode_s16(val):
    return val if val < 0x8000 else val - 0x10000

def decode_u32(high, low):
    return (high << 16) | low

def decode_s32(high, low):
    val = (high << 16) | low
    return val if val < 0x80000000 else val - 0x100000000


# ============================================================================
# MAIN TASK
# ============================================================================

@time_trigger("period(0, 15)")
def task_solis_all():
    """
    MIND THE CONNECTION TIMEOUT IN SOLIS_DRIVER.PY WHEN CHANGING POLL INTERVAL

    Fires on the fixed grid :00/:15/:30/:45 and queries the inverter once.
    If the S2-WL-ST is busy (e.g. during cloud upload) the query times out,
    that single data point is skipped, and the next grid tick tries again.
    """
    global _is_running

    if _is_running:
        log.warning(f"S2: Solis polling noch aktiv")
        return

    _is_running = True

    try:
        a, b, c = task.executor(solis_driver.query_solis)
        log.debug(f"S2: Datensatz erhalten")

        # --- Temperatures ---
        state.set("sensor.solis_raw_wr_temperature",   value=round(decode_s16(b[14]) * 0.1, 1))

        # --- PV yield ---
        state.set("sensor.solis_raw_pv_total_yield",  value=decode_u32(a[0], a[1]))
        state.set("sensor.solis_raw_pv_month_yield",  value=decode_u32(a[2], a[3]))
        state.set("sensor.solis_raw_pv_today_yield",  value=round(decode_u16(a[6]) * 0.1, 1))
        state.set("sensor.solis_raw_pv_year_yield",   value=decode_u32(a[8], a[9]))

        # --- PV strings (voltage * current = power in W) ---
        state.set("sensor.solis_raw_pv_p1", value=round(decode_u16(a[20]) * decode_u16(a[21]) * 0.01, 0))
        state.set("sensor.solis_raw_pv_p2", value=round(decode_u16(a[22]) * decode_u16(a[23]) * 0.01, 0))
        state.set("sensor.solis_raw_pv_p3", value=round(decode_u16(a[24]) * decode_u16(a[25]) * 0.01, 0))
        state.set("sensor.solis_raw_pv_p4", value=round(decode_u16(a[26]) * decode_u16(a[27]) * 0.01, 0))

        # --- PV power ---
        state.set("sensor.solis_raw_pv_dc_power", value=round(decode_u32(a[28], a[29]), 1))
        state.set("sensor.solis_raw_pv_ac_power", value=round(decode_s32(b[0], b[1]), 1))

        # --- Battery ---
        # v_final and i_final are kept (used twice: own sensor + p_batt).
        # i_final carries the charge/discharge sign.
        v_final = round(decode_u16(c[0]) * 0.1, 1)
        i_final = round(decode_u16(c[1]) * 0.1 * (1 if decode_u16(c[2]) == 1 else -1), 2)

        state.set("sensor.solis_raw_batt_v",               value=v_final)
        state.set("sensor.solis_raw_batt_i",               value=i_final)
        state.set("sensor.solis_raw_batt_p",               value=round(v_final * i_final, 0))
        state.set("sensor.solis_raw_batt_soc",             value=decode_u16(c[6]))
        state.set("sensor.solis_raw_batt_soh",             value=decode_u16(c[7]))
        state.set("sensor.solis_raw_batt_total_charge",    value=decode_u32(c[28], c[29]))
        state.set("sensor.solis_raw_batt_total_discharge", value=decode_u32(c[32], c[33]))
        state.set("sensor.solis_raw_batt_today_charge",    value=round(decode_u16(c[30]) * 0.1, 1))
        state.set("sensor.solis_raw_batt_today_discharge", value=round(decode_u16(c[34]) * 0.1, 1))

        # --- Grid & house load ---
        state.set("sensor.solis_raw_house_load",  value=decode_u16(c[14]))
        state.set("sensor.solis_raw_grid_power",  value=decode_s32(c[18], c[19]))

        state.set("sensor.solis_raw_grid_import_total", value=decode_u32(c[36], c[37]))
        state.set("sensor.solis_raw_grid_import_today", value=round(decode_u16(c[38]) * 0.1, 1))
        state.set("sensor.solis_raw_grid_export_total", value=decode_u32(c[40], c[41]))
        state.set("sensor.solis_raw_grid_export_today", value=round(decode_u16(c[42]) * 0.1, 1))

        state.set("sensor.solis_raw_house_total", value=decode_u32(c[44], c[45]))
        state.set("sensor.solis_raw_house_today", value=round(decode_u16(c[46]) * 0.1, 1))

        # --- Connection status ---
        state.set("sensor.solis_connection_status",
                  value="online",
                  attributes={'last_success': time.strftime('%H:%M:%S')})

    except (ConnectionRefusedError, ConnectionError, ValueError, TimeoutError, OSError) as e:
        # S2 busy or unreachable — skip this data point, next grid tick retries
        log.warning(f"S2: Datenpunkt entfallen ({e})")

    except Exception as e:
        log.error(f"S2: Unerwarteter Fehler: {e}")

    finally:
        _is_running = False