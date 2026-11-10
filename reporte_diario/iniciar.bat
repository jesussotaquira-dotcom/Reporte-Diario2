@echo off
rem Reporte diario: doble clic para abrir la pagina (Windows).
rem La primera vez prepara el entorno de Python y crea el .env a partir de la plantilla.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo No se encontro Python. Instalalo desde https://www.python.org/downloads/
  echo y, en el instalador, marca "Add python.exe to PATH". Luego vuelve a abrir este archivo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Preparando el entorno de Python ^(solo la primera vez^)...
  python -m venv .venv
  if errorlevel 1 (
    echo No se pudo crear el entorno de Python.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo No se pudieron instalar las librerias ^(revisa tu conexion a internet^).
  pause
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Se creo tu archivo .env. Completalo siguiendo el README ^(pasos 2 a 7^),
  echo guardalo y vuelve a abrir este archivo.
  notepad ".env"
  exit /b 0
)

".venv\Scripts\python.exe" servidor.py
pause
