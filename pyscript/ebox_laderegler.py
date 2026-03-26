import socket
import struct
from datetime import datetime  # Für Zeitstempel

# KONFIGURATION
EBOX_IP    = "192.168.178.99"
EBOX_PORT  = 502
UNIT_ID    = 1

def encode_f32(val):
    b = struct.pack('>f', float(val))
    return struct.unpack('>HH', b)

def build_write_multiple_pdu(trans_id, start, values):
    count = len(values)
    byte_count = count * 2
    length = 7 + byte_count
    fmt = f'>HHHBBHHB{count}H'
    return struct.pack(fmt, trans_id, 0, length, UNIT_ID, 16, start, count, byte_count, *values)

# Regelmäßiger Heartbeat alle 20 Sekunden hält Kontakt zur eBox,
# sodass diese nicht nach 1 Minute in den Fallback Modus fällt
@time_trigger("period(now, 20s)")
def task_ebox_heartbeat():
    # Hole den aktuellen Wert vom Schieberegler
    current_val = state.get("input_number.ebox_ladestrom_vorgabe")
    if current_val is not None:
        compleo_set_current(amps=float(current_val))

@service
def compleo_set_current(amps=6):
    # Setzt den Ladestrom via Modbus TCP auf 0A oder
    # zwischen 6A und 16A
    amps = float(amps)
    if amps < 6: amps = 0
    if amps > 16: amps = 16

    h, l = encode_f32(amps)
    values = [h, l, h, l, h, l]

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect((EBOX_IP, EBOX_PORT))
        pdu = build_write_multiple_pdu(99, 1012, values)
        s.sendall(pdu)
        s.recv(12)

        # Den Status in eine HA-Entität schreiben
        zeitstempel = datetime.now().strftime("%H:%M:%S")
        state.set(
            "sensor.ebox_status_uebertragung",
            value=f"{amps}",
            friendly_name="eBox Letzter Übertragener Stromwert",
            unit_of_measurement="A",
            icon="mdi:check-network",
            last_sync=zeitstempel,
            status="Erfolgreich"
        )

    except Exception as e:
        # Im Fehlerfall den Sensor ebenfalls aktualisieren
        state.set(
            "sensor.ebox_status_uebertragung",
            value="Fehler",
            friendly_name="eBox Letzter Übertragener Stromwert",
            icon="mdi:alert-circle",
            error_message=str(e),
            status="Fehlgeschlagen"
        )
        log.error(f"eBOX Heartbeat Fehler: {e}")
    finally:
        s.close()
