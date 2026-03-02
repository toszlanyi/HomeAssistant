# ============================================================================
# PHANTOM POWER FILTER
# ============================================================================
# Erkennt 3-Phasen-Phantom-Leistung basierend auf Energiefluss
# Läuft bei jeder Änderung der relevanten Sensoren
# ============================================================================

@state_trigger(
    "sensor.solis_raw_grid_power",
    "sensor.solis_raw_pv_dc_power",
    "sensor.solis_raw_batt_p",
    "sensor.solis_raw_batt_soc",
    "sensor.solis_raw_house_load"
)
def phantom_filter_update():
    """
    Berechnet Plausibilität von Import/Export.
    
    Filtert 3 Arten von Phantom Power:
    1. Symmetrisch (Grid ~0W, DC aktiv)
    2. Import-Phantom (Überschuss vorhanden)
    3. Export-Phantom (keine Quelle)
    """
    
    # ========================================================================
    # SENSOREN LADEN
    # ========================================================================
    
    try:
        pv = float(state.get('sensor.solis_raw_pv_dc_power') or 0)
        batt_p = float(state.get('sensor.solis_raw_batt_p') or 0)
        batt_soc = float(state.get('sensor.solis_raw_batt_soc') or 0)
        grid = float(state.get('sensor.solis_raw_grid_power') or 0)
        house = float(state.get('sensor.solis_raw_house_load') or 0)
    except (ValueError, TypeError):
        log.warning("Phantom Filter: Ungültige Sensorwerte")
        return
    
    # ========================================================================
    # BERECHNUNGEN
    # ========================================================================
    
    # DC-Konvertierung aktiv?
    dc_active = (pv > 50) or (batt_soc > 10 and abs(batt_p) > 100)
    
    # Verfügbare Energie
    verfuegbar = pv + batt_p
    
    # Symmetrisches Phantom
    symmetrisch = dc_active and abs(grid) < 50
    
    # ========================================================================
    # EXPORT PLAUSIBILITÄT
    # ========================================================================
    
    export_plausibel = True
    export_grund = "Plausibel"
    
    if symmetrisch:
        export_plausibel = False
        export_grund = f"Symmetrisch (Grid={round(grid)}W)"
    elif pv < 50 and batt_soc <= 12:
        export_plausibel = False
        export_grund = f"Keine Quelle (PV={round(pv)}W, SoC={round(batt_soc)}%)"
    elif grid < -50:
        export_plausibel = False
        export_grund = f"Grid=Bezug ({round(grid)}W)"
    elif verfuegbar < (house - 200):
        export_plausibel = False
        export_grund = f"Zu wenig ({round(verfuegbar)}W)"
    
    # ========================================================================
    # IMPORT PLAUSIBILITÄT
    # ========================================================================
    
    import_plausibel = True
    import_grund = "Plausibel"
    
    if symmetrisch:
        import_plausibel = False
        import_grund = f"Symmetrisch (Grid={round(grid)}W)"
    elif grid > 100:
        import_plausibel = False
        import_grund = f"Grid=Einspeisung ({round(grid)}W)"
    elif dc_active and verfuegbar > (house + 200) and batt_soc > 10:
        import_plausibel = False
        import_grund = f"Überschuss ({round(verfuegbar)}W)"
    elif not dc_active:
        import_grund = "Kein DC (kein Phantom)"
    elif house > 10500 and batt_p > 9500:
        import_grund = f"Batt-Limit ({round(house)}W)"
    
    # ========================================================================
    # PHANTOM POWER AKTUELL
    # ========================================================================
    
    phantom_w = 0
    
    if symmetrisch:
        phantom_w = abs(grid)
    elif grid < 0 and not import_plausibel:
        phantom_w = abs(grid)
    elif grid > 0 and not export_plausibel:
        phantom_w = grid
    
    # ========================================================================
    # RAW SENSOREN SETZEN (nur Werte, keine Attribute!)
    # ========================================================================
    
    state.set('sensor.phantom_raw_export_plausible', 
              value=1 if export_plausibel else 0)
    
    state.set('sensor.phantom_raw_export_grund', 
              value=export_grund)
    
    state.set('sensor.phantom_raw_import_plausible', 
              value=1 if import_plausibel else 0)
    
    state.set('sensor.phantom_raw_import_grund', 
              value=import_grund)
    
    state.set('sensor.phantom_raw_power_now', 
              value=round(phantom_w, 0))
