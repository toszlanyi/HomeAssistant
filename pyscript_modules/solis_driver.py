import socket
import struct
import time

# ============================================================================
# CONFIGURATION
# ============================================================================
SOLIS_IP = "192.168.178.105"
SOLIS_PORT = 502
UNIT_ID = 1

READ_ENERGY_CHUNK = True

# ============================================================================
# REGISTER BLOCKS
# ============================================================================
CHUNK_A_START = 33029
CHUNK_A_COUNT = 30
CHUNK_B_START = 33079
CHUNK_B_COUNT = 19
CHUNK_C_START = 33133
CHUNK_C_COUNT = 47
CHUNK_D_START = 33590
CHUNK_D_COUNT = 7

# ============================================================================
# HELPER FUNCTIONS
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

def query_optional(sock, trans_id, start, count):
    """Like query(), but tolerant of firmware that does not implement the
    requested registers. Reads the MBAP length field first, then exactly that
    many bytes, so a short Modbus exception frame (illegal data address) does
    not stall recv_exact waiting for a full data block that never arrives.
    Returns None on any exception response or malformed frame instead of
    raising, so the caller can treat "not available" as a normal outcome."""
    sock.sendall(build_pdu(trans_id, start, count))
    header = recv_exact(sock, 6)                  # transID(2) protoID(2) length(2)
    length = struct.unpack('>H', header[4:6])[0]
    rest = recv_exact(sock, length)               # unitID + FC + payload
    fc = rest[1]
    if fc & 0x80:                                 # exception response
        return None
    if fc != 4:
        return None
    byte_count = rest[2]
    if byte_count != count * 2:
        return None
    return struct.unpack(f'>{count}H', rest[3:3 + byte_count])

# ============================================================================
# MAIN QUERY FUNCTION — pure single shot execution
# ============================================================================
def query_solis():
    """
    Connects to the inverter, reads register chunks A/B/C plus the optional
    energy chunk D, and closes the socket immediately.

    SETTIMEOUT: MIND THE POLLING INTERVAL FROM SOLIS_PYSCRIPT.PY
    BUT ALLOW ENOUGH TIME TO RECOVER FROM CLOUD UPLOAD CLASH

    Returns (a, b, c, d). d is None when READ_ENERGY_CHUNK is off or the
    firmware answered the 33590 block with an exception.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(10.0)
    try:
        s.connect((SOLIS_IP, SOLIS_PORT))
        # Query in reverse order with distinct transaction IDs
        c = query(s, 3, CHUNK_C_START, CHUNK_C_COUNT)
        # time.sleep(0.200)
        b = query(s, 2, CHUNK_B_START, CHUNK_B_COUNT)
        # time.sleep(0.100)
        a = query(s, 1, CHUNK_A_START, CHUNK_A_COUNT)

        # Optional energy block. Shorter timeout so a firmware that silently
        # drops the request fails fast instead of stalling the whole cycle,
        # and its own try/except so a miss never takes A/B/C down with it.
        d = None
        if READ_ENERGY_CHUNK:
            try:
                s.settimeout(2.0)
                d = query_optional(s, 4, CHUNK_D_START, CHUNK_D_COUNT)
            except Exception:
                d = None

        time.sleep(0.100)
        return a, b, c, d
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except:
            pass
        s.close()
