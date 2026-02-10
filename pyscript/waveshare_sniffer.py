import socket
import struct

# --- KONFIGURATION ---
UDP_PORT = 14502
GATEWAY_IP = "192.168.178.24"
PUBLISH_INTERVAL = 10

# Vorzeichen-Korrektur:
# 1 = Standard (Bezug positiv),
# -1 = Falls dein Zähler bauartbedingt negativ bei Bezug liefert
SIGN_CORRECTION = -1

buffer = {
    "u": [], "i": [], "p_phases": [], "p_total": [],
    "freq": [], "e_import": [], "e_export": []
}
last_reg = None

if "sock_udp" not in globals():
    sock_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock_udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock_udp.bind(('0.0.0.0', UDP_PORT))
    sock_udp.setblocking(False)

@time_trigger("startup")
def task_sniffer():
    global last_reg
    while True:
        task.sleep(0.02)
        try:
            while True:
                data, addr = sock_udp.recvfrom(1024)
                if addr[0] != GATEWAY_IP: continue
                if len(data) == 8 and data[1] == 0x04:
                    last_reg = (data[2] << 8) + data[3]
                elif len(data) > 5 and data[1] == 0x04:
                    payload = data[3 : 3 + data[2]]
                    vals = [struct.unpack('>f', payload[i:i+4])[0] for i in range(0, len(payload)-3, 4)]
                    if last_reg is not None:
                        if last_reg == 0:    buffer["u"].append(vals[:3])
                        elif last_reg == 6:  buffer["i"].append(vals[:3])
                        elif last_reg == 12: buffer["p_phases"].append(vals[:3])
                        elif last_reg == 52: buffer["p_total"].append(vals[0])
                        elif last_reg == 70: buffer["freq"].append(vals[0])
                        elif last_reg == 72: buffer["e_import"].append(vals[0])
                        elif last_reg == 74: buffer["e_export"].append(vals[0])
                        last_reg = None
        except BlockingIOError: pass

@time_trigger(f"period(0, {PUBLISH_INTERVAL}s)")
def publish_to_ha():
    def set_raw(name, val):
        state.set(f"sensor.eastron_raw_{name}", value=val)

    if buffer["p_total"]:
        set_raw("p_total", round((sum(buffer["p_total"])/len(buffer["p_total"])) * SIGN_CORRECTION, 1))

    if buffer["u"]:
        for i in range(3):
            # Durchschnitt für die normale Anzeige
            avg_u = sum([r[i] for r in buffer["u"]])/len(buffer["u"])
            # Minimum für die U_min Überwachung
            min_u = min([r[i] for r in buffer["u"]])

            set_raw(f"u_l{i+1}", round(avg_u, 1))
            set_raw(f"u_min_l{i+1}", round(min_u, 1))

    if buffer["i"]:
        for i in range(3):
            set_raw(f"i_l{i+1}", round(sum([r[i] for r in buffer["i"]])/len(buffer["i"]), 2))

    if buffer["p_phases"]:
        for i in range(3):
            set_raw(f"p_l{i+1}", round((sum([r[i] for r in buffer["p_phases"]])/len(buffer["p_phases"])) * SIGN_CORRECTION, 1))

    if buffer["freq"]: set_raw("freq", round(buffer["freq"][-1], 2))
    if buffer["e_import"]: set_raw("e_import", round(buffer["e_import"][-1], 2))
    if buffer["e_export"]: set_raw("e_export", round(buffer["e_export"][-1], 2))

    for key in buffer: buffer[key] = []
