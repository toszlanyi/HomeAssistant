import socket
import struct
import time

# ============================================================================
# CONFIGURATION
# ============================================================================
SOLIS_IP   = "192.168.178.105"
SOLIS_PORT = 502
UNIT_ID    = 1

# ============================================================================
# REGISTER BLOCKS
# ============================================================================
CHUNK_A_START = 33029
CHUNK_A_COUNT = 30

CHUNK_B_START = 33079
CHUNK_B_COUNT = 19   # ends at 33097 (temperatures)

CHUNK_C_START = 33133
CHUNK_C_COUNT = 47

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

# ============================================================================
# MAIN QUERY FUNCTION — pure single shot execution
# ============================================================================

def query_solis():
    """
    Connects to the inverter, reads all 3 register chunks,
    and closes the socket immediately.

    SETTIMEOUT: MIND THE POLLING INTERVAL FROM SOLIS_PYSCRIPT.PY
                BUT ALLOW ENOUGH TIME TO RECOVER FROM CLOUD UPLOAD CLASH
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
        time.sleep(0.100)

        return a, b, c

    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except:
            pass
        s.close()
