#!/usr/bin/env bash
# Full reproducible run of the de-identification pipeline.
# Usage: bash run.sh /data/input /data/work /data/out/corpus
set -euo pipefail
IN="${1:-/data/input}"; WORK="${2:-/data/work}"; OUT="${3:-/data/out/corpus}"
PY="${PY:-/opt/venv/bin/python}"
mkdir -p "$WORK"
cd "$(dirname "$0")"
$PY -m deid.harvest  "$IN" "$WORK/harvest.jsonl"
# scans read by the registry (same folder as harvest.jsonl): PDF metadata + text, image EXIF/XMP, SVG metadata
$PY -m deid.pdfscan "$IN" "$WORK/pdf.jsonl"
exiftool -j -r -q -q -ext jpg -ext jpeg -ext png -ext tif -ext tiff -ext gif -ext bmp -ext webp   -Artist -Author -Creator -By-line -Copyright -Rights -OwnerName -XPAuthor -LastModifiedBy -Credit "$IN" > "$WORK/exif.json" || true
$PY -m deid.svgscan "$IN" "$WORK/svgmeta.jsonl"
$PY -m deid.registry "$IN/.get-input/manifest.tsv" "$WORK/harvest.jsonl" "$WORK/registry.json"
$PY -m deid.pipeline prepare "$WORK/registry.json" "$WORK/state.json" "$IN"
$PY -m deid.pipeline run "$WORK/state.json" "$IN" "$OUT" "$WORK/report.jsonl"
$PY -m deid.verify "$WORK/report.jsonl" "$IN" "$OUT"
$PY -m deid.readme "$WORK/report.jsonl" "$IN" "$WORK/README.txt"
