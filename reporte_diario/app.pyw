"""
Reporte diario como aplicación de Windows: abre la página en su propia ventana
sin mostrar la consola.

- Doble clic (o el acceso directo del escritorio): si el servidor no está
  corriendo, lo arranca en segundo plano (sin ventana) y abre la página. Si ya
  está corriendo, solo abre la página.
- `pythonw app.pyw --instalar` (o crear_acceso_directo.bat): crea el acceso
  directo «Reporte diario» en el escritorio y en el menú Inicio.

El servidor se cierra con el botón «Cerrar programa» de la página. Su registro
queda en salidas/servidor.log.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG = BASE / "salidas" / "servidor.log"
ICONO = BASE / "web" / "icono.ico"
NOMBRE = "Reporte diario"
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def mensaje(texto: str, error: bool = False) -> None:
    """Ventana de aviso de Windows (con pythonw no hay consola donde escribir)."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, texto, NOMBRE, 0x10 if error else 0x40)
    except Exception:  # noqa: BLE001 — fuera de Windows
        print(texto, file=sys.stderr)


def python_consola() -> Path:
    """python.exe del entorno del proyecto (.venv) o, si no hay, el que abrió este archivo."""
    venv = BASE / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return venv
    actual = Path(sys.executable)
    consola = actual.with_name("python.exe")
    return consola if consola.exists() else actual


def python_ventana() -> Path:
    """pythonw.exe (sin consola) junto al python elegido."""
    p = python_consola()
    w = p.with_name("pythonw.exe")
    return w if w.exists() else p


def puerto() -> int:
    """REPORTE_PUERTO del .env (sin depender de python-dotenv)."""
    try:
        for linea in (BASE / ".env").read_text(encoding="utf-8").splitlines():
            clave, _, valor = linea.partition("=")
            if clave.strip() == "REPORTE_PUERTO":
                return int(valor.split("#")[0].strip().strip('"\''))
    except (OSError, ValueError):
        pass
    return 8765


def servidor_activo(p: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/", timeout=1.5) as r:
            return r.headers.get("Server", "").startswith("ReporteDiario")
    except Exception:  # noqa: BLE001 — conexión rechazada, puerto de otro programa…
        return False


def preparar() -> bool:
    """Primera vez: crea el .env y comprueba las librerías. False = no seguir."""
    if not (BASE / ".env").exists():
        shutil.copy(BASE / ".env.example", BASE / ".env")
        mensaje("Se creó tu archivo .env. Complétalo siguiendo el README (pasos 2 a 7), guárdalo y "
                "vuelve a abrir Reporte diario.")
        subprocess.Popen(["notepad.exe", str(BASE / ".env")])
        return False
    py = str(python_consola())
    prueba = subprocess.run([py, "-c", "import requests, dotenv"], creationflags=SIN_VENTANA,
                            capture_output=True)
    if prueba.returncode != 0:
        instalar = subprocess.run([py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                   "-r", str(BASE / "requirements.txt")],
                                  creationflags=SIN_VENTANA, capture_output=True, text=True)
        if instalar.returncode != 0:
            mensaje("No se pudieron instalar las librerías de Python (revisa tu conexión a internet).\n\n"
                    + (instalar.stderr or instalar.stdout)[-800:], error=True)
            return False
    return True


def arrancar(p: int) -> bool:
    """Arranca servidor.py sin ventana y espera a que responda."""
    LOG.parent.mkdir(exist_ok=True)
    salida = open(LOG, "w", encoding="utf-8")  # el proceso hijo hereda el archivo
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proceso = subprocess.Popen(
        [str(python_consola()), str(BASE / "servidor.py"), "--no-abrir"], cwd=BASE, env=env,
        stdout=salida, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=SIN_VENTANA | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    salida.close()
    for _ in range(60):  # hasta ~15 s
        if servidor_activo(p):
            return True
        if proceso.poll() is not None:
            break
        time.sleep(0.25)
    try:
        detalle = LOG.read_text(encoding="utf-8")[-1200:]
    except OSError:
        detalle = ""
    mensaje(f"No se pudo iniciar el servidor del reporte.\n\n{detalle}\n\nRegistro completo: {LOG}", error=True)
    return False


def navegador_app() -> str | None:
    """Edge o Chrome, para abrir la página en una ventana propia (sin pestañas ni barra)."""
    rutas = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((str(r) for r in rutas if r.exists()), None)


def abrir(p: int) -> None:
    try:
        llave = (BASE / ".llave_local").read_text(encoding="utf-8").strip()
    except OSError:
        llave = ""
    url = f"http://127.0.0.1:{p}/" + (f"?k={llave}" if llave else "")
    exe = navegador_app()
    if exe:
        subprocess.Popen([exe, f"--app={url}", "--window-size=1400,900"])
    else:
        webbrowser.open(url)


def instalar() -> None:
    """Acceso directo en el escritorio y en el menú Inicio (con el icono del programa)."""
    destino, argumentos = str(python_ventana()), f'"{Path(__file__).resolve()}"'
    script = rf"""
$shell = New-Object -ComObject WScript.Shell
foreach ($carpeta in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
  $s = $shell.CreateShortcut((Join-Path $carpeta '{NOMBRE}.lnk'))
  $s.TargetPath = '{destino.replace("'", "''")}'
  $s.Arguments = '{argumentos.replace("'", "''")}'
  $s.WorkingDirectory = '{str(BASE).replace("'", "''")}'
  $s.IconLocation = '{str(ICONO).replace("'", "''")},0'
  $s.Description = 'Reporte diario: daily a Azure DevOps, Drive, Chat y RIPOR'
  $s.Save()
}}
"""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       creationflags=SIN_VENTANA, capture_output=True, text=True)
    if r.returncode != 0:
        mensaje(f"No se pudo crear el acceso directo.\n\n{(r.stderr or r.stdout)[-800:]}", error=True)
    else:
        mensaje(f"Listo: «{NOMBRE}» quedó en el escritorio y en el menú Inicio.\n\n"
                "Para tenerlo en la barra de tareas: ábrelo desde el menú Inicio, clic derecho → "
                "Anclar a la barra de tareas.")


def main() -> int:
    if "--instalar" in sys.argv:
        instalar()
        return 0
    p = puerto()
    if not servidor_activo(p):
        if not preparar() or not arrancar(p):
            return 1
    abrir(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
