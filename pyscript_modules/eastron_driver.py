# /config/pyscript_modules/eastron_driver.py
import socket
import time

WAVESHARE_IP = "192.168.178.24"
WAVESHARE_PORT = 502

def get_raw_data(duration=9.0):
    """Öffnet die TCP-Leitung und sammelt 9s lang alle eintreffenden Bytes."""
    buffer = b""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5) # Timeout für den Verbindungsaufbau
        s.connect((WAVESHARE_IP, WAVESHARE_PORT))

        start_time = time.time()
        s.settimeout(0.2) # Kurzer Intervall-Timeout für die Loop

        while (time.time() - start_time) < duration:
            try:
                # Wir empfangen alles, was der Waveshare auf den Bus spiegelt
                chunk = s.recv(4096)
                if not chunk: break
                buffer += chunk
            except socket.timeout:
                continue
    except Exception:
        return None
    finally:
        if s: s.close() # Ganz wichtig: Verbindung sofort wieder schließen!
    return buffer
