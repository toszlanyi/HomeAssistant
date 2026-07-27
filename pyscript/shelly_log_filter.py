"""
Unterdrückt bekannte, harmlose Verbindungsfehler EINES bestimmten Shelly-Geräts
(z. B. weil es an einer schaltbaren Steckdose hängt, die regelmäßig stromlos
geschaltet wird), ohne die Fehlerausgabe für andere Shelly-Geräte zu beeinflussen.

Betroffene Logzeilen (Beispiel):
  ERROR (MainThread) [aioshelly.rpc_device.wsrpc] Invalid Message from host 192.168.178.121:80: ...
  ERROR (MainThread) [homeassistant.components.shelly] Error fetching Shelly-mg3-5 data: ...

Läuft dauerhaft (kein Zeitfenster) - filtert ausschließlich Meldungen, die einen
der unten angegebenen Gerätenamen oder IPs enthalten. Alle anderen Shelly-Geräte
bleiben von diesem Filter unberührt.
"""

import logging

# --- Anpassen -----------------------------------------------------------
RULES = [
    {
        "name": "shelly_mg3_5_offline_steckdose",
        "loggers": [
            "homeassistant.components.shelly",
            "aioshelly.rpc_device.wsrpc",
            "aioshelly",
        ],
        # Nachricht muss eines dieser Fragmente enthalten, sonst bleibt sie sichtbar
        "needles": ["Shelly-mg3-5", "192.168.178.121"],
    },
    # Weiteres Gerät ergänzen? Einfach ein zusätzliches Dict in diese Liste:
    # {
    #     "name": "shelly_XYZ",
    #     "loggers": ["homeassistant.components.shelly", "aioshelly.rpc_device.wsrpc", "aioshelly"],
    #     "needles": ["Shelly-XYZ", "192.168.178.XXX"],
    # },
]
# -------------------------------------------------------------------------


@pyscript_compile
def _build_filter_class():
    """Definiert die Filter-Klasse einmalig als kompilierte (nicht-async) native
    Python-Klasse, da logging.Filter.filter() synchron von der Logging-Bibliothek
    aufgerufen wird und keine pyscript-Coroutine akzeptiert. Wird nur einmal beim
    Laden dieses Skripts ausgeführt, nicht bei jedem Trigger."""

    class IgnoreNeedlesFilter(logging.Filter):
        """Unterdrückt Nachrichten mit bestimmten Textfragmenten (oder den
        gesamten Logger, wenn needles leer ist)."""

        def __init__(self, needles):
            super().__init__()
            self.needles = needles

        def filter(self, record):
            if not self.needles:
                return False
            msg = record.getMessage()
            return not any(needle in msg for needle in self.needles)

    return IgnoreNeedlesFilter


# Einmal auf Modulebene erzeugt, damit der Typ bei jedem Reload stabil bleibt
# (sonst würden isinstance()-Prüfungen unten fehlschlagen und Filter sich duplizieren)
_IgnoreNeedlesFilter = _build_filter_class()


@time_trigger("startup")
def install_shelly_log_filters():
    """Registriert alle Filter aus RULES einmalig beim HA-Start bzw. bei jedem
    Pyscript-Reload."""
    applied = []

    for rule in RULES:
        needles = rule["needles"]
        for logger_name in rule["loggers"]:
            target_logger = logging.getLogger(logger_name)
            # Vermeidet doppelte Filter bei mehrfachem Reload
            target_logger.filters = [
                f for f in target_logger.filters
                if not isinstance(f, _IgnoreNeedlesFilter)
            ]
            target_logger.addFilter(_IgnoreNeedlesFilter(needles))
        applied.append(rule["name"])

    log.info(f"shelly_log_filter: aktive Regeln: {applied}")
