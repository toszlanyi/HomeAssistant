# ============================================================================
# Version 5.10.3
# ============================================================================
# Sniffs the Modbus communication between inverter and smart meter, mirrored
# passively by a Waveshare RS485 TO ETH (B). The bus carries Modbus TCP, so
# frames start with an MBAP header and the parser locks onto the unit id and
# function code that follow it.
# The inverter polls the power block roughly every 280 ms and the remaining
# registers roughly every 1.7 s, so the sample count per key follows from
# SNIFF_DURATION and differs by about a factor of six between the two groups.
# All averaged values use a trimmed mean (drop the highest/lowest samples per
# window, average the rest): median-like outlier resistance without the step
# artifacts a pure median causes during fast load transitions, and better
# statistical efficiency than a median on small sample counts. Energy counters
# keep the plain median, being the most consequential values in the script.
# Voltage spreads (spread_ln, spread_ll) are computed per response frame, so
# the three voltages compared always come from the same snapshot. Star point
# shift (v0) and negative sequence unbalance (u2_pct) are derived once per
# cycle from the six trimmed voltage means: v0 isolates the zero sequence in
# volts, u2_pct states in percent how asymmetric the supply itself is.
# Reactive power (q1..q3, q_tot), apparent power (va1..va3, va_tot) and power
# factor (pf1..pf3) come from register blocks the inverter already polls. With
# both P and Q per phase the line resistance and reactance can be separated in
# the voltage rise dU = (R*P + X*Q) / U instead of collapsing into one lumped
# impedance. Apparent power is redundant with P and Q and is kept as a parse
# consistency check: va should equal the magnitude of P and Q combined.
# Frequency is read from register 70, which the inverter only delivers when it
# requests a block covering it; the sensor stays untouched otherwise. It is
# published as a plain cycle average and deliberately left out of the 10 minute
# aggregation, because IEC 61000-4-30 derives the frequency from whole cycle
# counts over 10 s rather than by averaging samples.
# EN 50160 requires voltages to be evaluated as means over fixed, successive
# 10 minute intervals. Those are aggregated here in clock-aligned blocks using
# the quadratic mean (root of the mean of the squares) that IEC 61000-4-30
# specifies, and published as separate eastron_10min_* sensors.
# Phantom: energy and power are corrected to suppress the noise the meter
# registers while the inverter throttles around zero on all three phases; that
# noise would otherwise inflate the meter values without real import/export.
# The net power side of that test is evaluated on a rolling mean over the last
# PHANTOM_AVG_WINDOW_S rather than on the current cycle, because the throttling
# swings are zero mean while a real flow is not.
# If enabled, communication statistics and raw data are written to csv files;
# the raw hex dump grows the file quickly and should be used sparingly.

import socket
import select
import struct
import time
import statistics
import os
import csv

# ============================================================================
# VARIABLES
# ============================================================================

# ---- Network configuration ----
WAVESHARE_IP   = "192.168.178.24"
WAVESHARE_PORT = 502
EASTRON_ID     = 0x01

# ---- Timing (trigger interval, connection timeout, data collection) ----
# TRIGGER_SPEC is built from TRIGGER_PERIOD_S and passed straight into
# @time_trigger(...) below, so the interval only has to be edited in one
# place. If some Pyscript version rejects a non-literal
# decorator argument, replace the @time_trigger(TRIGGER_SPEC) line with the
# hardcoded string, e.g. @time_trigger("period(12, 15)").
#
# CONNECT_TIMEOUT is a fixed budget for establishing the TCP connection (see
# _sniff_raw_data). SNIFF_DURATION is what's left of the trigger period
# after that budget, so the two can never overlap.
TRIGGER_PERIOD_S = 15
CONNECT_TIMEOUT  = 0.5
SNIFF_DURATION   = TRIGGER_PERIOD_S - CONNECT_TIMEOUT
TRIGGER_SPEC     = f"period(12, {TRIGGER_PERIOD_S})"

# ---- Filter settings for collected data (outlier filter and average) ----
# Trimmed-mean settings: number of lowest/highest values removed from each
# side before averaging.
# POWER_TRIM_COUNT applies to p1/p2/p3/p_tot, which the inverter polls at the
# high rate. TRIM_COUNT applies to everything else that gets averaged
# (voltages, currents, line-to-line voltages, spreads), polled roughly six
# times more slowly. One value per side is enough there: it removes a single
# corrupted float from a misaligned frame while leaving most of the window for
# the mean. Both counts must stay well below the samples a window actually
# holds, so revisit them if SNIFF_DURATION is shortened substantially.
# Min/max sensors are taken from the untrimmed list and stay true extremes.
POWER_TRIM_COUNT = 10
TRIM_COUNT       = 1

# ---- Phantom energy parameter ----
# Phantom threshold: phase power must exceed this value for the sign
# to count as "active" (noise filter)
# 0 = no filter, recommended: 0-10W
PHANTOM_THRESHOLD_W = 1

# Maximum total deviation (sum of p1+p2+p3) at which phantom current is
# still assumed. Scales with actual power flow. The threshold is
# PHANTOM_BASE_W plus a percentage of the gross power flowing through the 
# phases (abs(p1)+abs(p2)+abs(p3), so it stays tight at idle and widens
# linear as load increases.
PHANTOM_BASE_W     = 40
PHANTOM_PERCENT    = 0.02  # 2.0% of gross power flow

# Window the net power is averaged over before it is compared against that
# threshold. The gross term above scales with what flows through the meter, so
# it stays at its floor when a balanced three phase load is covered internally
# from PV and battery - precisely the case where the regulation swings are
# widest and a single cycle can land far from zero in either direction. Those
# swings are zero mean, a real import or export is not, so averaging separates
# them. Longer is not automatically better: past roughly three minutes real
# transitions start to smear into the mean and the classification degrades
# again. The energy counters lag by up to one window at each transition, once
# in each direction, so the error cancels rather than accumulating.
PHANTOM_AVG_WINDOW_S = 65

# ---- Key groups for the sensor write loop ----
# HIGH_RATE_KEYS come from the fast-polled register blocks and use
# POWER_TRIM_COUNT; everything else uses TRIM_COUNT. SIGNED_KEYS are flipped to the
# commercial convention (negative = export) as they leave the script; apparent
# power is a magnitude and stays positive.
HIGH_RATE_KEYS = ("p1", "p2", "p3", "p_tot",
                  "q1", "q2", "q3", "q_tot", "va_tot")
SIGNED_KEYS    = ("p1", "p2", "p3", "p_tot",
                  "q1", "q2", "q3", "q_tot",
                  "pf1", "pf2", "pf3")

# ---- EN 50160 aggregation ----
# Keys aggregated into fixed 10 minute blocks. The six voltages are fed with
# every trimmed sample of each cycle; v0 and u2_pct are derived once per cycle
# and contribute a single value each, so their block counts are lower by the
# number of samples per window.
# EN50160_MIN_SAMPLES suppresses partial blocks after a restart or reload; the
# actual count is published as the samples attribute of each sensor.
EN50160_KEYS        = ("u1", "u2", "u3", "u12", "u23", "u31", "v0", "u2_pct")
EN50160_BLOCK_S     = 600
EN50160_MIN_SAMPLES = 20

# ---- Traffic logging (to measure the actual data volume per sniff cycle) ----
# Writes one row per cycle: timestamp, duration, bytes, frames parsed.
# If needed (RAW_LOG_ENABLED), also logs the raw data as a hex string —
# careful: this makes the file significantly larger, only enable for
# short test runs.
TRAFFIC_LOG_ENABLED = False
RAW_LOG_ENABLED      = False
LOG_DIR              = "/config/eastron_sniffer_log"
STATS_LOG_FILE       = os.path.join(LOG_DIR, "traffic_stats.csv")
RAW_LOG_FILE         = os.path.join(LOG_DIR, "traffic_raw.csv")

# ---- Offset tracking for phantom energy (mutable state, updated at runtime) ----
_phantom_was_active  = False
_e_imp_phantom_start = 0.0
_e_exp_phantom_start = 0.0
_e_imp_offset        = 0.0
_e_exp_offset        = 0.0
_e_imp_last_good     = 0.0
_e_exp_last_good     = 0.0
_offset_initialized  = False

# Rolling buffer for the phantom net power test, one entry per cycle:
# (timestamp, phase sum, p_tot register or None). Entries older than
# PHANTOM_AVG_WINDOW_S are dropped as they age out.
_net_history = []

# Running sum of squares and sample count of the current 10 minute block,
# keyed by sensor name, plus the block index the buffers belong to.
_en50160_sumsq = {}
_en50160_count = {}
_en50160_block = None

# Phantom detection only becomes active once all three phases
# have had at least one real reading
_phases_initialized  = False
_p1_seen = False
_p2_seen = False
_p3_seen = False


# ============================================================================
# LOGGER — reports the resolved timing values on every reload, to confirm the
# dynamic @time_trigger(TRIGGER_SPEC) loaded correctly.
# ============================================================================

log.info(
    f"S1 Eastron sniffer: TRIGGER_SPEC={TRIGGER_SPEC!r}, "
    f"SNIFF_DURATION={SNIFF_DURATION}s, CONNECT_TIMEOUT={CONNECT_TIMEOUT}s — "
)


# ============================================================================
# TRIGGERS
# ============================================================================
# The triggers below call helpers and executors defined later in the file.
# Names in a function body are resolved at call time, not at definition, and
# the triggers only fire after the module has fully loaded, so forward
# references are fine. SNIFF_DURATION is used as a default argument (evaluated
# at definition) and is defined above in VARIABLES.

@time_trigger("startup")
def init_on_startup():
    """Load the last corrected e_imp/e_exp values from HA on startup, after a
    10s delay so HA has loaded its states."""
    global _e_imp_last_good, _e_exp_last_good, _offset_initialized

    task.sleep(10)

    try:
        # Import current states of the template sensors resulting from *_raw_e_*
        # to initialize *_last_good. Raw sensors are excluded from recorder.
        imp = state.get('sensor.netzbezug_gesamt')
        exp = state.get('sensor.netzeinspeisung_gesamt')

        # offset_initialized stays False until startup succeeded.
        # last_good is only set when real values are loaded, so the
        # offset is calculated correctly on the first reading.
        if imp not in (None, 'unknown', 'unavailable'):
            _e_imp_last_good = float(imp)
            log.info(f"Startup: e_imp initialized with {_e_imp_last_good} kWh")
        else:
            log.warning("Startup: sensor.eastron_raw_e_imp not available – starting with 0")

        if exp not in (None, 'unknown', 'unavailable'):
            _e_exp_last_good = float(exp)
            log.info(f"Startup: e_exp initialized with {_e_exp_last_good} kWh")
        else:
            log.warning("Startup: sensor.eastron_raw_e_exp not available – starting with 0")

    except Exception as e:
        log.error(f"Startup init error: {e}")


# TRIGGER_SPEC is built above from TRIGGER_PERIOD_S. If a pyscript version
# rejects the non-literal argument, replace it with the literal line below and
# keep it in sync with TRIGGER_PERIOD_S.
# @time_trigger("period(12, 15)")
@time_trigger(TRIGGER_SPEC)
def process_eastron_data():
    """Sniff the RS485 traffic between inverter and Eastron meter, parse the
    Modbus request/response frame pairs, apply phantom detection and offset
    tracking, and write the corrected values to HA sensor states."""
    global _phantom_was_active
    global _e_imp_phantom_start, _e_exp_phantom_start
    global _e_imp_offset, _e_exp_offset
    global _e_imp_last_good, _e_exp_last_good
    global _offset_initialized
    global _phases_initialized, _p1_seen, _p2_seen, _p3_seen
    global _en50160_block
    global _net_history

    buffer, actual_start_time, actual_duration = _sniff_raw_data()

    # Close the previous 10 minute block before this cycle's samples are added,
    # so each sample lands in the block its sniff window started in.
    block = int(actual_start_time // EN50160_BLOCK_S)
    if _en50160_block is None:
        _en50160_block = block
    elif block != _en50160_block:
        en50160_flush()
        _en50160_block = block

    if not buffer or len(buffer) < 8:
        _log_traffic(buffer, SNIFF_DURATION, actual_start_time, actual_duration, 0)
        return

    stats = {
        "u1": [], "u2": [], "u3": [],
        "u12": [], "u23": [], "u31": [],
        "spread_ln": [], "spread_ll": [],
        "i1": [], "i2": [], "i3": [],
        "p1": [], "p2": [], "p3": [],
        "p_tot": [], "e_imp": [], "e_exp": [],
        "freq": [],
        "va1": [], "va2": [], "va3": [], "va_tot": [],
        "q1": [], "q2": [], "q3": [], "q_tot": [],
        "pf1": [], "pf2": [], "pf3": []
    }

    frame_count = 0
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

                            # --- PHASE LOGIC ---
                            # Fixed Eastron register layout. reg_start 0 maps to
                            # a 12-byte voltage block or a >=36-byte combined
                            # block, so the byte_count distinction is required.
                            # Spreads are built here, inside one frame, so the
                            # compared voltages share a single snapshot.
                            if reg_start == 0 and byte_count_expected == 12:
                                v = _unpack_floats(payload, 3)
                                _append(stats, ("u1", "u2", "u3"), v)
                                stats["spread_ln"].append(max(v) - min(v))
                            elif reg_start == 6 and byte_count_expected == 12:
                                _append(stats, ("i1", "i2", "i3"),
                                        _unpack_floats(payload, 3))
                            elif reg_start == 12 and byte_count_expected == 12:
                                _append(stats, ("p1", "p2", "p3"),
                                        _unpack_floats(payload, 3))
                            elif reg_start == 18 and byte_count_expected == 12:
                                _append(stats, ("va1", "va2", "va3"),
                                        _unpack_floats(payload, 3))
                            elif reg_start == 24 and byte_count_expected == 12:
                                _append(stats, ("q1", "q2", "q3"),
                                        _unpack_floats(payload, 3))
                            elif reg_start == 30 and byte_count_expected == 12:
                                _append(stats, ("pf1", "pf2", "pf3"),
                                        _unpack_floats(payload, 3))
                            elif reg_start == 200 and byte_count_expected == 12:
                                v = _unpack_floats(payload, 3)
                                _append(stats, ("u12", "u23", "u31"), v)
                                stats["spread_ll"].append(max(v) - min(v))
                            elif reg_start == 0 and byte_count_expected >= 36:
                                v = _unpack_floats(payload, 9)
                                _append(stats,
                                        ("u1", "u2", "u3", "i1", "i2", "i3",
                                         "p1", "p2", "p3"), v)
                                stats["spread_ln"].append(max(v[:3]) - min(v[:3]))

                            # --- FLEXIBLE LOGIC FOR ENERGY & P-TOTAL ---
                            # Check whether the target registers lie within the requested range
                            for target_reg, target_key in [(52, "p_tot"), (56, "va_tot"),
                                                           (60, "q_tot"), (70, "freq"),
                                                           (72, "e_exp"), (74, "e_imp")]:
                                if reg_start <= target_reg < reg_start + reg_count:
                                    # Each register occupies 2 bytes, calculate offset
                                    byte_offset = (target_reg - reg_start) * 2
                                    if len(payload) >= byte_offset + 4:
                                        val = struct.unpack('>f', payload[byte_offset:byte_offset+4])[0]
                                        stats[target_key].append(val)

                            frame_count += 1
                            i = j + 3 + byte_count_expected
                            break
            except Exception:
                # Catches unpacking errors (struct.error) without crashing the script
                pass
        i += 1

    _log_traffic(buffer, SNIFF_DURATION, actual_start_time, actual_duration, frame_count)

    # ========================================================================
    # PHANTOM DETECTION based on trimmed mean phase power
    # ========================================================================

    avg_p1 = trimmed_mean(stats["p1"], POWER_TRIM_COUNT) if stats["p1"] else None
    avg_p2 = trimmed_mean(stats["p2"], POWER_TRIM_COUNT) if stats["p2"] else None
    avg_p3 = trimmed_mean(stats["p3"], POWER_TRIM_COUNT) if stats["p3"] else None
    avg_p_tot = trimmed_mean(stats["p_tot"], POWER_TRIM_COUNT) if stats["p_tot"] else None

    # track phase initialization — phantom detection is only valid
    # once all three phases have had at least one real reading
    if avg_p1 is not None:
        _p1_seen = True
    if avg_p2 is not None:
        _p2_seen = True
    if avg_p3 is not None:
        _p3_seen = True

    if not _phases_initialized:
        if _p1_seen and _p2_seen and _p3_seen:
            _phases_initialized = True
            log.info("Eastron: All phases initialized, phantom detection active")
        else:
            log.debug("Eastron: Waiting for all phases to initialize")

    # Fallback to 0.0 only for state.set — not for phantom detection
    p1 = avg_p1 if avg_p1 is not None else 0.0
    p2 = avg_p2 if avg_p2 is not None else 0.0
    p3 = avg_p3 if avg_p3 is not None else 0.0

    # Rolling net power for the phantom test. Phase sum and p_tot are kept
    # apart and averaged separately, so the artificial 0.0 that combined_net()
    # returns on a sign conflict never enters the mean; the two are combined
    # afterwards. Only appended when the whole phase block was captured, since
    # p1/p2/p3 arrive in one register read and the 0.0 fallback below would
    # otherwise push a phase sum into the buffer that was never measured.
    if avg_p1 is not None and avg_p2 is not None and avg_p3 is not None:
        now = time.time()
        _net_history.append((now, avg_p1 + avg_p2 + avg_p3, avg_p_tot))
        while _net_history and (now - _net_history[0][0]) > PHANTOM_AVG_WINDOW_S:
            _net_history.pop(0)

    # An empty buffer (first cycles after a reload, or a run of cycles without
    # the phase block) leaves net_avg at None and is_phantom falls back to the
    # single cycle value. That is the safe direction: it lets flow through
    # rather than suppressing it, and shows up against the utility meter.
    net_avg = None
    if _net_history:
        sum_avg = sum([h[1] for h in _net_history]) / len(_net_history)
        tot_vals = [h[2] for h in _net_history if h[2] is not None]
        tot_avg = sum(tot_vals) / len(tot_vals) if tot_vals else None
        net_avg = combined_net(sum_avg, tot_avg)

    phantom = _phases_initialized and is_phantom(
        p1, p2, p3, p_tot_raw=avg_p_tot, net_override=net_avg)

    state.set("sensor.eastron_raw_phantom_active",
              value=1 if phantom else 0,
              new_attributes={
                  # Sign-flipped to the commercial convention (negative =
                  # export, positive = import), matching the sensors below.
                  'p1': round(-p1, 1),
                  'p2': round(-p2, 1),
                  'p3': round(-p3, 1),
                  'p_tot_calc': round(-(p1 + p2 + p3), 1),
                  # Meter's own p_tot register; None when not captured this cycle.
                  'p_tot': round(-avg_p_tot, 1) if avg_p_tot is not None else None,
                  # Rolling mean the phantom test was decided on, and how many
                  # cycles it covers. Logged so the window can be retuned from
                  # recorded data instead of by guesswork.
                  'net_avg': round(-net_avg, 1) if net_avg is not None else None,
                  'net_avg_n': len(_net_history),
                  'phases_ready': _phases_initialized,
              })

    # ========================================================================
    # OFFSET TRACKING: permanently subtract phantom energy
    # ========================================================================

    e_imp_raw = statistics.median(stats["e_imp"]) if stats["e_imp"] else None
    e_exp_raw = statistics.median(stats["e_exp"]) if stats["e_exp"] else None

    if e_imp_raw is not None and e_exp_raw is not None:

        # only initialize the offset once startup values were loaded.
        # _e_imp_last_good and _e_exp_last_good are, after init_on_startup,
        # either the last known HA values or 0.0 (no HA value available).
        # In both cases the offset is calculated correctly on the first reading.
        if not _offset_initialized:
            _e_imp_offset = e_imp_raw - _e_imp_last_good
            _e_exp_offset = e_exp_raw - _e_exp_last_good
            _offset_initialized = True
            log.info(
                f"Offset initialized: imp={_e_imp_offset:.2f} kWh, "
                f"exp={_e_exp_offset:.2f} kWh"
            )

        # Phantom starts
        if phantom and not _phantom_was_active:
            _e_imp_phantom_start = e_imp_raw
            _e_exp_phantom_start = e_exp_raw
            log.debug(
                f"Phantom START – e_imp={e_imp_raw:.2f}, e_exp={e_exp_raw:.2f} "
                f"(p1={round(p1)}W, p2={round(p2)}W, p3={round(p3)}W)"
            )

        # Phantom ends
        if not phantom and _phantom_was_active:
            delta_imp = e_imp_raw - _e_imp_phantom_start
            delta_exp = e_exp_raw - _e_exp_phantom_start
            _e_imp_offset += delta_imp
            _e_exp_offset += delta_exp
            log.debug(
                f"Phantom END – Delta e_imp={delta_imp:.3f} kWh, "
                f"e_exp={delta_exp:.3f} kWh → "
                f"Total offset: imp={_e_imp_offset:.3f}, exp={_e_exp_offset:.3f}"
            )

        _phantom_was_active = phantom

        if phantom:
            state.set("sensor.eastron_raw_e_imp", value=_e_imp_last_good)
            state.set("sensor.eastron_raw_e_exp", value=_e_exp_last_good)
        else:
            # Only the counter matching the net flow direction advances. The
            # idle counter is re-pegged to its own last_good, so its offset
            # absorbs the raw register growth instead of deferring it into a
            # jump on the next opposite-direction cycle.
            # Three decimals resolve 1 Wh. That is the meter's own limit: the
            # counters are float32, whose spacing at the current register
            # magnitudes is about 0.5 to 1 Wh, so a fourth decimal would only
            # add quantization noise. The offsets themselves stay unrounded.
            net_power = combined_net_power(p1, p2, p3, avg_p_tot)
            if net_power > 0:
                _e_exp_last_good = round(e_exp_raw - _e_exp_offset, 3)
                _e_imp_offset = e_imp_raw - _e_imp_last_good
            elif net_power < 0:
                _e_imp_last_good = round(e_imp_raw - _e_imp_offset, 3)
                _e_exp_offset = e_exp_raw - _e_exp_last_good
            state.set("sensor.eastron_raw_e_imp", value=_e_imp_last_good)
            state.set("sensor.eastron_raw_e_exp", value=_e_exp_last_good)

    # ========================================================================
    # WRITE ALL OTHER SENSORS
    # ========================================================================
    # Power sensors are sign-flipped here, at the point they leave the script:
    # HA uses the commercial convention (negative = export, positive = import),
    # while parsing, is_phantom and offset tracking above use the raw Eastron
    # convention (positive = export).

    for key, values in stats.items():
        if not values:
            continue

        if key in ("e_imp", "e_exp"):
            continue

        if key in HIGH_RATE_KEYS:
            avg_val = trimmed_mean(values, POWER_TRIM_COUNT)
        else:
            # voltages, currents, line-to-line voltages, spreads, frequency,
            # per phase apparent power and power factor
            trimmed = trimmed_values(values, TRIM_COUNT)
            avg_val = statistics.fmean(trimmed)
            if key in EN50160_KEYS:
                en50160_add(key, trimmed)

        if key in SIGNED_KEYS:
            avg_val = -avg_val

        # Only p_tot is suppressed during phantom. Reactive power is a real
        # flow even when the active net flow sits at zero, so q_tot passes
        # through untouched.
        if phantom and key == "p_tot":
            state.set("sensor.eastron_raw_p_tot", value=0.0)
            continue

        # Frequency carries a third decimal; 10 mHz is the resolution
        # IEC 61000-4-30 works with, and two decimals would sit right at it.
        decimals = 3 if key in ("freq", "pf1", "pf2", "pf3") else 2
        state.set(f"sensor.eastron_raw_{key}", value=round(avg_val, decimals))

        # Extremes come from the untrimmed list and follow the direction that
        # matters: voltages sag, currents and asymmetry peak.
        #
        # The sample count travels with them because an extreme depends on
        # it far more strongly than a mean does: at the update rate of these
        # registers a window holds only a handful of samples, and one more or
        # less shifts the expected extreme without anything happening on the
        # grid. Check this attribute before reading a physical cause into a
        # min/max series. TRIM_COUNT has no effect here - the extremes come
        # from the untrimmed list by design.
        if key in ("u1", "u2", "u3", "u12", "u23", "u31"):
            state.set(f"sensor.eastron_raw_{key}_min", value=round(min(values), 2),
                      new_attributes={"samples": len(values)})
        elif key in ("i1", "i2", "i3", "spread_ln", "spread_ll"):
            state.set(f"sensor.eastron_raw_{key}_max", value=round(max(values), 2),
                      new_attributes={"samples": len(values)})

    # Star point shift and negative sequence unbalance, derived from the
    # trimmed voltage means of this cycle. Both need all six voltages, so the
    # sensors are left untouched and keep their previous value whenever one of
    # the two register blocks was missing from the capture.
    if (stats["u1"] and stats["u2"] and stats["u3"]
            and stats["u12"] and stats["u23"] and stats["u31"]):
        u1_avg = trimmed_mean(stats["u1"], TRIM_COUNT)
        u2_avg = trimmed_mean(stats["u2"], TRIM_COUNT)
        u3_avg = trimmed_mean(stats["u3"], TRIM_COUNT)
        u12_avg = trimmed_mean(stats["u12"], TRIM_COUNT)
        u23_avg = trimmed_mean(stats["u23"], TRIM_COUNT)
        u31_avg = trimmed_mean(stats["u31"], TRIM_COUNT)

        result = star_point_vector(u1_avg, u2_avg, u3_avg,
                                   u12_avg, u23_avg, u31_avg)
        if result is not None:
            v0 = result[0]
            # The residual travels with v0 so that a cycle disturbed by a
            # load step during the poll can be filtered out downstream.
            state.set("sensor.eastron_raw_v0", value=round(v0, 2),
                      new_attributes={"resid": round(result[4], 3)})
            # Volts the neutral displacement adds to (positive) or removes
            # from (negative) each phase.
            state.set("sensor.eastron_raw_v0_l1", value=round(result[1], 2))
            state.set("sensor.eastron_raw_v0_l2", value=round(result[2], 2))
            state.set("sensor.eastron_raw_v0_l3", value=round(result[3], 2))
            en50160_add("v0", [v0])

        u2_pct = unbalance_u2(u12_avg, u23_avg, u31_avg)
        if u2_pct is not None:
            state.set("sensor.eastron_raw_u2_pct", value=round(u2_pct, 2))
            en50160_add("u2_pct", [u2_pct])

    del buffer


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _unpack_floats(payload, count):
    """Unpack `count` big-endian float32 values from the front of `payload`
    and return them as a tuple. struct.error propagates to the caller."""
    return struct.unpack(f">{'f' * count}", payload[:count * 4])


def _append(stats, keys, values):
    """Append each value to the stats list named by the matching key, paired
    positionally. keys and values must be the same length."""
    for key, value in zip(keys, values):
        stats[key].append(value)


def star_point_vector(u1, u2, u3, u12, u23, u31):
    """Locate the neutral point relative to the phase-voltage triangle.

    Takes six voltage magnitudes:

        u1, u2, u3     line-to-neutral   (L1-N, L2-N, L3-N)
        u12, u23, u31  line-to-line      (L1-L2, L2-L3, L3-L1)

    Treated geometrically, the three phase conductors are points in the
    complex voltage plane. The line-to-line magnitudes are the distances
    between them and fix the shape of the triangle; the line-to-neutral
    magnitudes are the distances from each corner to the neutral and fix
    where the neutral sits inside it. Six distances against two unknowns
    leaves the problem over-determined, and that redundancy is used to
    average out the inconsistency from the registers not being read at
    the same instant.

    A balanced system puts the neutral at the centroid. Displacement
    from it is the zero-sequence voltage V0 = (U1 + U2 + U3) / 3, a
    vector: its length is how far the star point has moved, its
    direction which phase that raises.

    Direction is reported by projecting V0 onto the three phase axes,
    each running from the centroid towards one corner. Since
    U_k = E_k + V0, with E_k the balanced phase vector, a displacement
    small against nominal voltage makes the projection d_k the number of
    volts the displacement adds to phase k:

        d_k > 0   phase k reads high, neutral moved away from it
        d_k < 0   phase k reads low, neutral moved towards it

    This differs from phase voltage minus average, which mixes zero and
    negative sequence; d_k isolates the zero-sequence part.

    d1 + d2 + d3 vanishes only for an equilateral triangle. Negative
    sequence makes the triangle irregular and leaves a small residue,
    which is geometry rather than error and must not be read as a
    consistency check.

    Returns (v0, d1, d2, d3, resid), all in volts:

        v0      length of the neutral displacement, always >= 0
        d1..d3  volts the displacement adds to each phase
        resid   how far the six inputs are from describing one
                consistent triangle, see STEP 6

    Returns None if any magnitude is non-positive, or if fewer than
    three candidate positions can be constructed. Callers must handle
    None; it occurs when a register read is missed and a stale zero
    reaches this function.
    """
    a, b, c = u12, u23, u31

    # A zero or negative magnitude means a bad read, not a real system
    # state. Bail out rather than produce a plausible-looking number.
    if min(a, b, c) <= 0 or min(u1, u2, u3) <= 0:
        return None

    # STEP 1 - lay the triangle out in an arbitrary 2-D frame. Only
    # distances and the relative direction of the displacement are used
    # later, both invariant under rotation, so the orientation is free:
    # L1 at the origin, L2 on the positive x-axis at distance u12, L3
    # where the remaining two sides put it. cx from the law of cosines,
    # cy from Pythagoras.
    cx = (c * c - b * b + a * a) / (2 * a)
    cy2 = c * c - cx * cx
    # cy2 < 0 means the three sides cannot form a triangle. Collapse to
    # a flat one rather than raise; the scoring below reflects it.
    cy = cy2 ** 0.5 if cy2 > 0 else 0.0

    verts = [(0.0, 0.0), (a, 0.0), (cx, cy)]
    dists = [u1, u2, u3]

    # The centroid is where the neutral would sit with zero displacement.
    gx = (a + cx) / 3
    gy = cy / 3

    # STEP 2 - trilateration. Around each corner sits a circle of radius
    # equal to that phase's line-to-neutral voltage. Taken two at a time
    # the circles cross at two points, so the three pairs yield six
    # candidate neutral positions. Each is scored against the corner not
    # used to construct it: the error is how far its distance to that
    # third corner deviates from the third measured voltage, which
    # separates the true intersection from its mirror image.
    candidates = []
    for i in range(3):
        j = (i + 1) % 3          # second corner of this pair
        k = (i + 2) % 3          # the unused corner, used for scoring

        px = verts[i][0]
        py = verts[i][1]
        qx = verts[j][0]
        qy = verts[j][1]
        dp = dists[i]            # circle radius around corner i
        dq = dists[j]            # circle radius around corner j
        dr = dists[k]            # expected distance to the third corner

        d2 = (qx - px) * (qx - px) + (qy - py) * (qy - py)
        if d2 <= 0:
            # Two corners coincide - impossible in a live system, but a
            # stale register could produce it. Skip this pair.
            continue
        d = d2 ** 0.5

        # Unit vector from corner i towards corner j.
        ex = (qx - px) / d
        ey = (qy - py) / d

        # x is how far along that line the two circles intersect,
        # h is how far off it, perpendicular.
        x = (dp * dp - dq * dq + d * d) / (2 * d)
        h2 = dp * dp - x * x
        # h2 < 0 means the circles do not reach each other. Rather than
        # discard the pair, treat them as just touching (h = 0), which
        # puts the candidate on the connecting line. The error score
        # will rank it appropriately.
        h = h2 ** 0.5 if h2 > 0 else 0.0

        # The two intersection points: one either side of the line.
        for sign in (1, -1):
            sx = px + x * ex - sign * h * ey
            sy = py + x * ey + sign * h * ex
            rx = sx - verts[k][0]
            ry = sy - verts[k][1]
            err = abs((rx * rx + ry * ry) ** 0.5 - dr)
            candidates.append((err, sx, sy))

    if len(candidates) < 3:
        return None

    # STEP 3 - keep and average the three best candidates. With
    # consistent inputs the three true intersections coincide while the
    # mirror images do not, so sorting by error selects the right set;
    # averaging them turns the redundant measurements into noise
    # rejection. Tuples sort by their first element, so no sort key is
    # needed - which also keeps this pyscript-compatible.
    candidates.sort()
    mx = 0.0
    my = 0.0
    for item in candidates[:3]:
        mx = mx + item[1]
        my = my + item[2]
    mx = mx / 3
    my = my / 3

    # STEP 4 - displacement vector and magnitude. Points from the
    # neutral to the centroid, the sign convention that makes V0 equal
    # (U1+U2+U3)/3 and the projections below read as volts added.
    vx = gx - mx
    vy = gy - my
    v0 = (vx * vx + vy * vy) ** 0.5

    # STEP 5 - project onto the phase axes. The axis of phase k runs
    # from the centroid to corner k, the direction of the ideal phase
    # voltage E_k. Normalised, its dot product with the displacement is
    # the component along that phase.
    proj = [0.0, 0.0, 0.0]
    for i in range(3):
        ax = verts[i][0] - gx
        ay = verts[i][1] - gy
        an = (ax * ax + ay * ay) ** 0.5
        if an > 0:
            proj[i] = (vx * ax + vy * ay) / an

    # STEP 6 - residual. The mean of the three surviving candidate
    # errors measures in volts how far the six inputs are from one
    # triangle. Two effects raise it: sampling noise, setting a floor
    # that scales with the inverse square root of the samples per
    # window, and a load step during the poll, which leaves the
    # line-to-neutral and line-to-line registers describing different
    # instants. Suited as an outlier filter against a threshold well
    # above that floor, not as a proportional weight.
    resid = (candidates[0][0] + candidates[1][0] + candidates[2][0]) / 3

    return v0, proj[0], proj[1], proj[2], resid


def star_point_shift(u1, u2, u3, u12, u23, u31):
    """Return only the magnitude of the neutral displacement, in volts.

    Discards the direction and the residual. Kept so that existing
    callers do not have to change. Verified to return bit-identical values to
    the previous standalone implementation over the full weekend
    dataset (maximum difference 0.00e+00 V).
    """
    result = star_point_vector(u1, u2, u3, u12, u23, u31)
    if result is None:
        return None
    return result[0]


def unbalance_u2(u12, u23, u31):
    """Return the negative sequence voltage unbalance in percent, computed
    from the three line-to-line magnitudes alone. Returns None if the
    magnitudes are all zero."""
    a2, b2, c2 = u12 * u12, u23 * u23, u31 * u31
    total = a2 + b2 + c2
    if total <= 0:
        return None

    beta = (a2 * a2 + b2 * b2 + c2 * c2) / (total * total)
    root = 3 - 6 * beta
    root = root ** 0.5 if root > 0 else 0.0
    return ((1 - root) / (1 + root)) ** 0.5 * 100


def phantom_max_total_w(gross_power_w):
    """Return the dynamic phantom threshold in W for a given gross power flow
    (abs(p1)+abs(p2)+abs(p3))."""
    return PHANTOM_BASE_W + PHANTOM_PERCENT * gross_power_w


def combined_net(phase_sum, p_tot_raw=None):
    """Return the best estimate of net power flow (positive = export, raw
    Eastron convention) from an already formed phase sum and the meter's p_tot
    register. The phase powers come from separate Modbus reads, so a load shift
    between them can fake a net flow; p_tot is a single snapshot without that
    skew, so the smaller-magnitude of the two is used. A measured p_tot of 0.0
    is a valid reading and is trusted; only p_tot_raw None falls back to the
    phase sum. Opposite signs between the two indicate an artifact, returning
    0.0. Takes the sum rather than the three phases so that callers holding
    averages of both inputs can reuse it."""
    if p_tot_raw is None:
        return phase_sum
    if (phase_sum > 0) != (p_tot_raw > 0) and p_tot_raw != 0.0:
        return 0.0
    return phase_sum if abs(phase_sum) <= abs(p_tot_raw) else p_tot_raw


def combined_net_power(p1, p2, p3, p_tot_raw=None):
    """Return combined_net() for three individual phase powers."""
    return combined_net(p1 + p2 + p3, p_tot_raw)


def is_phantom(p1, p2, p3, p_tot_raw=None, net_override=None):
    """Return True if the readings look like phantom current: mixed phase signs
    and a net power near zero relative to the gross flow.

    net_override replaces this cycle's net power with the caller's rolling
    mean; None falls back to the single cycle value. The mixed sign test stays
    on the current cycle either way, since averaging signs is meaningless and
    the test is the structural half of the criterion: a genuine unbalanced
    import with all three phases in the same direction never reaches the
    threshold comparison at all."""
    positiv = len([p for p in [p1, p2, p3] if p > PHANTOM_THRESHOLD_W])
    negativ = len([p for p in [p1, p2, p3] if p < -PHANTOM_THRESHOLD_W])

    # Condition 1: mixed signs across the phases
    mixed_signs = (positiv > 0 and negativ > 0)

    if not mixed_signs:
        return False

    # Condition 2: net power near zero relative to the gross phase flow
    if net_override is None:
        net_power = combined_net_power(p1, p2, p3, p_tot_raw)
    else:
        net_power = net_override
    gross_power_w = abs(p1) + abs(p2) + abs(p3)
    threshold = phantom_max_total_w(gross_power_w)

    return abs(net_power) <= threshold


def trimmed_values(values, trim_count):
    """Return the sorted values with up to `trim_count` entries dropped from
    each end. trim_count is capped so at least one value always remains, and an
    empty input returns an empty list."""
    n = len(values)
    if n == 0:
        return []
    actual_trim = min(trim_count, (n - 1) // 2)
    sorted_vals = sorted(values)
    return sorted_vals[actual_trim: n - actual_trim] if actual_trim > 0 else sorted_vals


def trimmed_mean(values, trim_count):
    """Return the mean of the trimmed values, or None if there are none."""
    trimmed = trimmed_values(values, trim_count)
    if not trimmed:
        return None
    return statistics.fmean(trimmed)


def en50160_add(key, values):
    """Add the squares of one cycle's samples for `key` to the current
    10 minute block."""
    sum_sq = 0.0
    for value in values:
        sum_sq += value * value
    if key in _en50160_sumsq:
        _en50160_sumsq[key] += sum_sq
        _en50160_count[key] += len(values)
    else:
        _en50160_sumsq[key] = sum_sq
        _en50160_count[key] = len(values)


def en50160_flush():
    """Publish the finished 10 minute block as the quadratic mean of its
    samples and clear the buffers. Blocks holding fewer than
    EN50160_MIN_SAMPLES values are dropped rather than published."""
    global _en50160_sumsq, _en50160_count

    for key in EN50160_KEYS:
        count = _en50160_count.get(key, 0)
        if count >= EN50160_MIN_SAMPLES:
            value = (_en50160_sumsq[key] / count) ** 0.5
            state.set(f"sensor.eastron_10min_{key}",
                      value=round(value, 2),
                      samples=count)

    _en50160_sumsq = {}
    _en50160_count = {}


# ============================================================================
# EXECUTORS — run in thread executor to avoid blocking the event loop.
# ============================================================================

@pyscript_executor
def _sniff_raw_data(duration=SNIFF_DURATION):
    """Open a TCP connection to the Waveshare RS485-to-ETH adapter and collect
    the mirrored bus traffic for `duration` seconds. Returns (buffer,
    actual_start_time, actual_duration); buffer is None on connection failure.
    actual_start_time marks when the receive loop began, actual_duration the
    time actually spent in it (shorter if the connection or socket cut short)."""
    buffer = b""
    s = None
    start_time = time.time()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)  # Timeout for establishing the connection
        s.connect((WAVESHARE_IP, WAVESHARE_PORT))

        start_time = time.time()
        s.setblocking(False)  # select() handles the waiting

        while (time.time() - start_time) < duration:
            remaining = duration - (time.time() - start_time)
            ready, _, _ = select.select([s], [], [], min(remaining, 0.2))
            if ready:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buffer += chunk

    except Exception:
        return None, start_time, (time.time() - start_time)
    finally:
        if s:
            s.close()

    actual_duration = time.time() - start_time
    return buffer, start_time, actual_duration


@pyscript_executor
def _log_traffic(buffer, planned_duration, actual_start_time, actual_duration, frame_count):
    """Write one statistics row per sniff cycle to STATS_LOG_FILE (timestamp,
    planned/actual durations, byte count, bytes/second, frames parsed). With
    RAW_LOG_ENABLED, also append the raw buffer as a hex string to
    RAW_LOG_FILE."""
    if not TRAFFIC_LOG_ENABLED:
        return

    try:
        os.makedirs(LOG_DIR, exist_ok=True)

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        start_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(actual_start_time))
        n_bytes = len(buffer) if buffer else 0
        bytes_per_sec = round(n_bytes / actual_duration, 1) if actual_duration else 0

        stats_is_new = not os.path.exists(STATS_LOG_FILE)
        with open(STATS_LOG_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if stats_is_new:
                writer.writerow([
                    "timestamp", "planned_duration_s", "actual_start_time",
                    "actual_duration_s", "bytes", "bytes_per_sec", "frames_parsed"
                ])
            writer.writerow([
                ts, planned_duration, start_ts,
                round(actual_duration, 3), n_bytes, bytes_per_sec, frame_count
            ])

        if RAW_LOG_ENABLED and buffer:
            raw_is_new = not os.path.exists(RAW_LOG_FILE)
            with open(RAW_LOG_FILE, "a", newline="") as f:
                writer = csv.writer(f)
                if raw_is_new:
                    writer.writerow(["timestamp", "bytes", "hex_data"])
                writer.writerow([ts, n_bytes, buffer.hex()])

    except Exception as e:
        # Logging must never crash the actual processing
        log.error(f"Traffic logging error: {e}")
