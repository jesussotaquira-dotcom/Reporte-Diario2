#!/bin/bash
# Reporte diario: doble clic para abrir la página (Mac).
# La primera vez prepara el entorno de Python y crea el .env a partir de la plantilla.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "No se encontró Python 3. Instálalo desde https://www.python.org/downloads/ (o con: brew install python)"
  echo "y vuelve a abrir este archivo."
  read -r -p "Pulsa Enter para cerrar..."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Preparando el entorno de Python (solo la primera vez)..."
  python3 -m venv .venv || { echo "No se pudo crear el entorno de Python."; read -r -p "Enter para cerrar..."; exit 1; }
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt \
  || { echo "No se pudieron instalar las librerías (revisa tu conexión a internet)."; read -r -p "Enter para cerrar..."; exit 1; }

if [ ! -f ".env" ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Se creó tu archivo .env. Complétalo siguiendo el README (pasos 2 a 7), guárdalo y vuelve a abrir este archivo."
  open -e .env
  exit 0
fi

.venv/bin/python servidor.py
