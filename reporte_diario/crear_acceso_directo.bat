@echo off
rem Crea el acceso directo "Reporte diario" en el escritorio y el menu Inicio (una sola vez).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" app.pyw --instalar
) else (
  start "" pythonw app.pyw --instalar
)
