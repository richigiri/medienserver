#!/bin/sh
# Ollama-Dienst im Hintergrund starten
ollama serve &
pid=$!

# Warten, bis der Dienst bereit ist (maximal 30 Sekunden)
echo "Warte auf Ollama-Dienst..."
for i in $(seq 1 30); do
    if ollama list >/dev/null 2>&1; then
        echo "Ollama ist bereit."
        break
    fi
    sleep 1
done

# Modell erstellen (wenn bereits vorhanden, wird es überschrieben/aktualisiert)
echo "Erstelle Modell translate-gemma-sub-e4b..."
ollama create translate-gemma-sub-e4b -f /ollama-init/Modelfile

# Warten, bis der Dienstprozess endet (Container am Laufen halten)
wait $pid