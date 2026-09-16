#!/usr/bin/env bash
# Conciliación de cobros de Edenia, de punta a punta.
#   1. presencia   (archivos_check.py) -> qué check-outs tienen archivos + estado de pago + total real
#   2. comprobantes(read_comprobantes.py --ai-fallback) -> clasifica y concilia montos
#   3. reporte     (report.py) -> hallazgos priorizados (txt + html + html para artifact)
#   4. base        (store.py) -> guarda la corrida en SQLite (histórico)
#
# Uso: bash conciliar.sh [DIAS] [--no-ai]
#   DIAS  ventana de check-outs a revisar (default 14; usar más para el backfill inicial).
#   --no-ai  no usar OpenAI para imágenes ilegibles.
set -euo pipefail
cd "$(dirname "$0")"
DAYS="${1:-14}"
AI="--ai-fallback"; [ "${2:-}" = "--no-ai" ] && AI=""
mkdir -p out
ARCH="out/archivos_${DAYS}d.csv"

# Relectura fresca de la ventana en cada corrida (los comprobantes se suben tarde);
# el histórico queda en la base SQLite. El checkpoint protege solo dentro de una corrida.
rm -f "$ARCH"

echo "[1/4] presencia + totales, últimos $DAYS días"
python -u archivos_check.py check --days "$DAYS" --concurrency 8 --out "$ARCH"

echo "[2/4] leyendo comprobantes $AI"
python -u read_comprobantes.py --from "$ARCH" $AI

echo "[3/4] reporte de hallazgos"
python -u report.py --archivos "$ARCH" --comprobantes out/comprobantes.csv \
    --out out/hallazgos.txt --html out/hallazgos.html --artifact-html out/hallazgos_artifact.html

echo "[4/4] guardando en SQLite"
python -u store.py --archivos "$ARCH" --comprobantes out/comprobantes.csv --db out/edenia.db --days "$DAYS"
echo "Listo. Reporte: pipeline/out/hallazgos.html  ·  Base: pipeline/out/edenia.db"
