#!/bin/bash

# --- KONFIGURATION ---
SOLIS_IP="192.168.178.105"
UNIT_ID=1

# Prüfen ob als Root/Sudo gestartet
if [[ $EUID -ne 0 ]]; then
   echo "Bitte starte das Skript mit sudo: sudo ./solis_scanner.sh"
   exit 1
fi

echo "========================================"
echo "   SOLIS MODBUS REGISTER SCANNER"
echo "========================================"
echo "Tippe 'q' bei der Registernummer zum Beenden."

while true; do
    echo "----------------------------------------"
    read -p "Registernummer (z.B. 33035): " REG

    # Beenden-Logik
    if [[ "$REG" == "q" || "$REG" == "exit" ]]; then
        echo "Scanner beendet."
        break
    fi

    read -p "Bit-Modus (1 oder 2 Register): " BITS

    if [ "$BITS" == "2" ]; then
        TYPE="3:int"
    else
        TYPE="3"
    fi

    echo "Abfrage: Register $REG ($BITS Register)..."

    # mbpoll Abfrage
    timeout 5s mbpoll -v -t $TYPE -B -r $REG -a $UNIT_ID -c 1 -1 $SOLIS_IP

    # Sofortiges Killen der Verbindung für den nächsten Loop
    ss -K dport = 502 dst $SOLIS_IP > /dev/null 2>&1

    echo ""
    echo "Verbindung bereinigt. Bereit für nächstes Register."
done
