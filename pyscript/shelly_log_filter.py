"""
Unterdrückt bekannte, harmlose Log-Meldungen einzelner Geräte/Entitäten, ohne
die Ausgabe für andere Geräte zu beeinflussen. Typische Fälle:
  - ein Shelly an einer schaltbaren Steckdose, das regelmäßig stromlos wird und
    dann Verbindungsfehler wirft
  - der Recorder-Hinweis "total_increasing ... not strictly increasing" durch
    minimale Rückwärts-Sprünge im Energiezähler (Firmware-/Chip-seitig, harmlos;
    HA absorbiert die Dips in der Langzeitstatistik ohnehin)

Der Filter läuft dauerhaft (kein Zeitfenster) und greift ausschließlich bei
Meldungen, die einen der konfigurierten Namen/IPs/Entity-IDs enthalten.

--------------------------------------------------------------------------------
Umgebung / Voraussetzungen (getestet gegen):
  - Home Assistant 2026.7.4
  - Pyscript 2.0.1 (neues Decorator-Subsystem, Standard ab 2.0.0)

WICHTIG - allow_all_imports:
  Dieses Skript nutzt `import logging` und greift auf `logging.root.manager`
  zu. Beides steht NICHT auf Pyscripts Standard-Import-Whitelist. In der
  Pyscript-Konfiguration muss daher gesetzt sein:

      pyscript:
        allow_all_imports: true

  (Bei UI-Konfiguration den Haken "allow_all_imports" aktivieren.)

Funktionsweise-Hinweise (bewusste Design-Entscheidungen):
  - Die Filterklasse wird via @pyscript_compile als NATIVES Python definiert.
    Das ist zwingend: (a) logging.Filter.filter() wird synchron aus der
    Logging-Bibliothek (u. U. aus fremden Threads) aufgerufen und darf keine
    pyscript-Coroutine sein; (b) der pyscript-Interpreter kann selbst keine
    "speziellen Klassenmethoden" (__init__ etc.) definieren - kompiliert schon.
  - install_shelly_log_filters() nutzt **kwargs, weil @service beim Aufruf
    trigger_type="service" als Keyword-Argument übergibt (ebenso setzt der
    time_trigger trigger_type/trigger_time). Ohne **kwargs -> TypeError.
  - Es findet nur In-Memory-Manipulation von Logger-Objekten statt (kein I/O),
    daher kein "Detected blocking call to ... inside the event loop".
--------------------------------------------------------------------------------
"""

import logging

# Stabiler Marker zum Wiedererkennen "unserer" Filter über Reloads und
# Klassen-Neudefinitionen hinweg. MUSS mit dem Klassen-Attribut `managed_tag`
# im kompilierten Klassenkörper unten identisch sein.
_MANAGED_TAG = "shelly_log_filter"

# --- Anpassen -----------------------------------------------------------
RULES = [
    {
        "name": "shelly_mg3_5_offline_steckdose",
        # Prefix-Auflösung (s. _resolve_loggers) deckt Kind-Logger automatisch
        # mit ab. "homeassistant.components.shelly" fängt so auch ein evtl.
        # künftiges "...shelly.coordinator" mit. Der frühere wirkungslose
        # Eintrag "aioshelly" (Elternlogger) entfällt - dessen Filter würde
        # für Records von "aioshelly.rpc_device.wsrpc" nie konsultiert.
        "loggers": [
            "homeassistant.components.shelly",
            "aioshelly.rpc_device.wsrpc",
        ],
        # Nachricht muss eines dieser Fragmente enthalten, sonst bleibt sie
        # sichtbar. Der Gerätename steht in der Coordinator-Meldung, die IP in
        # der wsrpc-Meldung - daher beide.
        "needles": ["Shelly-mg3-5", "192.168.178.121"],
    },
    {
        "name": "ocpp_session_negativ_total_increasing",
        "loggers": ["homeassistant.components.sensor.recorder"],
        "needles": ["sensor.ebox_smart_energy_session"],
    },
    {
        "name": "shelly_terrasse_energie_dips",
        "loggers": ["homeassistant.components.sensor.recorder"],
        "needles": ["sensor.terrasse_energie"],
    },
    # Weiteres Gerät ergänzen? Einfach ein zusätzliches Dict in diese Liste:
    # {
    #     "name": "shelly_XYZ",
    #     "loggers": ["homeassistant.components.shelly", "aioshelly.rpc_device.wsrpc"],
    #     "needles": ["Shelly-XYZ", "192.168.178.XXX"],
    # },
]
# -------------------------------------------------------------------------


@pyscript_compile
def _build_filter_class():
    """Definiert die Filter-Klasse einmalig als kompilierte (nicht-async)
    native Python-Klasse. Wird nur beim Laden/Reload dieses Skripts ausgeführt,
    nicht bei jedem Trigger."""

    class IgnoreNeedlesFilter(logging.Filter):
        """Unterdrückt Nachrichten mit bestimmten Textfragmenten (oder den
        gesamten Logger, wenn needles leer ist)."""

        # Als Literal im kompilierten Klassenkörper eingebacken - muss mit
        # _MANAGED_TAG oben übereinstimmen. Trägt jede Instanz, damit die Dedup
        # sie unabhängig von der Klassen-Identität (Reload!) wiederfindet.
        managed_tag = "shelly_log_filter"

        def __init__(self, needles, rule_name=""):
            super().__init__()
            self.needles = needles
            self.rule_name = rule_name

        def filter(self, record):
            if not self.needles:
                return False
            try:
                msg = record.getMessage()
            except Exception:
                # Formatierungsfehler o. Ä.: im Zweifel NICHT unterdrücken,
                # damit nie versehentlich eine echte Meldung verschluckt wird
                # (fail-open statt fail-silent).
                return True
            return not any(needle in msg for needle in self.needles)

    return IgnoreNeedlesFilter


# Einmal auf Modulebene erzeugt. Bei jedem Reload entsteht zwar ein neuer
# Klassentyp - deshalb erkennt _is_managed() unsere Filter über den Marker
# (managed_tag) UND den Klassennamen, nicht über isinstance().
_IgnoreNeedlesFilter = _build_filter_class()


def _resolve_loggers(names, expand_prefix):
    """Liefert konkrete Logger-Objekte zu den Namen. Bei expand_prefix=True
    werden zusätzlich alle bereits existierenden Kind-Logger (name + '.')
    einbezogen. Das fängt spätere Umbenennungen wie '...shelly' ->
    '...shelly.coordinator' ab, ohne den Filter-Code anzufassen.

    Hinweis: Es werden nur zum Install-Zeitpunkt existierende Kind-Logger
    erfasst. Der Hauptname selbst wird über getLogger() bei Bedarf angelegt,
    greift also auch, wenn die Integration erst danach lädt."""
    resolved = {}
    existing = list(logging.root.manager.loggerDict.keys())
    for name in names:
        if not name:
            continue
        resolved[name] = logging.getLogger(name)
        if expand_prefix:
            prefix = name + "."
            for key in existing:
                if key.startswith(prefix):
                    resolved[key] = logging.getLogger(key)
    return list(resolved.values())


def _is_managed(f):
    """Erkennt "unsere" Filter - über den Marker UND den Klassennamen, damit
    auch Instanzen aus einer früheren (Reload-)Klassendefinition sicher
    entfernt werden und sich nichts dupliziert."""
    return getattr(f, "managed_tag", None) == _MANAGED_TAG \
        or type(f).__name__ == "IgnoreNeedlesFilter"


@service
@time_trigger("startup")
def install_shelly_log_filters(**kwargs):
    """Registriert alle Filter aus RULES - idempotent und dedup-fest über
    Reloads hinweg. Läuft automatisch beim HA-Start/Reload und ist zusätzlich
    als Service `pyscript.install_shelly_log_filters` manuell aufrufbar
    (praktisch zum Testen nach Änderungen an RULES).

    **kwargs fängt die von @service (trigger_type="service") bzw. vom
    time_trigger übergebenen Keyword-Argumente ab."""
    # Phase 0: Ziellogger je Regel bestimmen. Leere needles (= ganzer Logger)
    # werden NICHT per Prefix aufgeweitet, sonst brächte man versehentlich
    # einen kompletten Teilbaum zum Schweigen.
    plan = []
    touched = {}
    for rule in RULES:
        needles = rule["needles"]
        loggers = _resolve_loggers(rule["loggers"], expand_prefix=bool(needles))
        for lg in loggers:
            touched[id(lg)] = lg
        plan.append((rule["name"], needles, loggers))

    # Phase 1: alle bisherigen eigenen Filter von den berührten Loggern in
    # EINEM Durchgang entfernen (wichtig, wenn ein Logger von mehreren Regeln
    # bespielt wird, z. B. der recorder-Logger mit ebox UND Terrasse).
    for lg in touched.values():
        lg.filters = [f for f in lg.filters if not _is_managed(f)]

    # Phase 2: frische Filter setzen - ein Objekt je Regel, an alle Ziellogger.
    applied = []
    for name, needles, loggers in plan:
        filt = _IgnoreNeedlesFilter(needles, name)
        for lg in loggers:
            lg.addFilter(filt)
        applied.append(f"{name} ({len(loggers)} Logger)")

    log.info(f"shelly_log_filter: {len(applied)} Regel(n) aktiv: {applied}")
