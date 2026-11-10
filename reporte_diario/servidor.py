"""
servidor.py — Interfaz web local para armar y subir el reporte diario sin editar JSON.

    python servidor.py              # abre http://127.0.0.1:8765 en el navegador
    python servidor.py --no-abrir   # solo levanta el servidor

Solo escucha en 127.0.0.1 (nadie más en la red puede entrar) y la API exige
una llave local (archivo .llave_local, solo legible por ti): la página la
recibe como cookie al abrirla desde el enlace que muestra la consola, así que
otro usuario del mismo equipo no puede usar tus credenciales. Usa solo la
biblioteca estándar de Python y guarda todo en reporte_hoy.json, el mismo
archivo que usa reporte_diario.py desde la consola.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import reporte_diario as rd  # noqa: E402  (después de cargar el .env)

PUERTO = int(os.getenv("REPORTE_PUERTO", "8765"))
WEB_DIR = rd.BASE_DIR / "web"
ESTATICOS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/estilos.css": ("estilos.css", "text/css; charset=utf-8"),
    "/favicon.ico": ("icono.ico", "image/x-icon"),
}
SERVIDOR: ThreadingHTTPServer | None = None  # para «Cerrar programa»


def apagar() -> None:
    """Cierra el servidor cuando termine la operación en curso (no corta una subida a medias)."""
    with CANDADO:
        if SERVIDOR:
            SERVIDOR.shutdown()
CATEGORIAS_BASE = ["Reuniones", "Refinamiento", "QA"]
CANDADO = threading.Lock()  # una operación a la vez sobre reporte_hoy.json
log = logging.getLogger("servidor")
COOKIE = "reporte_llave"


def llave_local() -> str:
    """
    Llave de esta instalación: se crea una vez y se guarda en .llave_local con
    permisos solo para tu usuario. Sirve para que la API solo la use tu navegador.
    """
    archivo = rd.BASE_DIR / ".llave_local"
    if archivo.exists():
        llave = archivo.read_text(encoding="utf-8").strip()
        if len(llave) >= 32:
            return llave
    llave = secrets.token_urlsafe(32)
    archivo.write_text(llave, encoding="utf-8")
    if os.name != "nt":
        os.chmod(archivo, 0o600)
    return llave


LLAVE = llave_local()


class NoEncontrado(Exception):
    pass


def estado_tk_defecto() -> str:
    """ADO_TK_STATE del .env; si trae un valor inválido, la subida lo reporta y aquí se asume Closed."""
    try:
        return rd._estado_tk(os.getenv("ADO_TK_STATE", "Closed")) or "Closed"
    except ValueError:
        return "Closed"


def estado(reporte: rd.Reporte) -> dict:
    """Todo lo que la página necesita para dibujarse."""
    categorias = {t.categoria for t in reporte.tareas if t.categoria} | set(CATEGORIAS_BASE)
    return {
        "reporte": reporte.a_dict(),
        "categorias": sorted(categorias, key=str.lower),
        "config": {  # qué destinos están configurados en el .env
            "devops": bool(os.getenv("ADO_PAT")),
            "drive": bool(os.getenv("GDRIVE_APPS_SCRIPT_URL")),
            "chat": bool(os.getenv("GCHAT_WEBHOOK_URL")),
            "ripor": rd.RiporMCP.configurado(),
        },
        "estado_tk": estado_tk_defecto(),
        "hoy": date.today().isoformat(),  # para avisar si se olvidó «Nuevo día»
        "transportes": [{"id": k, "nombre": v} for k, v in rd.TRANSPORTES.items()],
        "puntos": [{"puntos": k, "texto": v} for k, v in sorted(rd.guia_puntos().items())],
        # Enlace del Doc de Drive del día elegido para RIPOR (vacío = no se conoce).
        "ripor_doc": rd.doc_del_dia(reporte, reporte.ripor_fecha or reporte.fecha),
    }


SESIONES = {"google": rd.estado_google, "ripor": rd.estado_ripor}


def despachar_sesion(metodo: str, partes: list[str]) -> dict:
    """
    /api/sesion/<google|ripor>: GET dice si la sesión sirve; POST abre el inicio
    de sesión en el navegador. No tocan reporte_hoy.json, así que van sin candado.
    """
    if len(partes) != 2 or partes[1] not in SESIONES:
        raise NoEncontrado()
    if metodo == "GET":
        return SESIONES[partes[1]]()
    if metodo == "POST":
        return {"aviso": rd.lanzar_inicio_sesion(partes[1])}
    raise NoEncontrado()


def despachar_consulta(partes: list[str], consulta: dict) -> dict:
    """Consultas de solo lectura a Azure y RIPOR: no tocan reporte_hoy.json (van sin candado)."""
    if partes == ["buscar"]:
        return {"resultados": rd.AzureDevOps().buscar(consulta.get("q", [""])[0])}
    if partes == ["ripor", "asignaciones"]:
        fecha = consulta.get("fecha", [date.today().isoformat()])[0]
        return rd.consultar_ripor("mis_asignaciones", {"fecha": rd._fecha(fecha, "fecha")})
    if partes == ["ripor", "tareas"]:
        proyecto = rd._id_ripor(consulta.get("project_id", [""])[0], "proyecto")
        if not proyecto:
            raise ValueError("Falta el proyecto de RIPOR.")
        return rd.consultar_ripor("tareas_del_proyecto", {"project_id": proyecto})
    raise NoEncontrado()


CONSULTAS = (["buscar"], ["ripor", "asignaciones"], ["ripor", "tareas"])


def despachar(metodo: str, partes: list[str], consulta: dict, cuerpo: dict) -> dict:
    """Rutas de la API que leen o cambian reporte_hoy.json (van dentro del candado)."""
    try:
        reporte = rd.cargar_reporte()
    except ValueError as e:
        raise RuntimeError(f"{rd.ruta_reporte().name} tiene un dato inválido ({e}). Corrígelo o "
                           "renómbralo para empezar uno nuevo.") from None

    if metodo == "GET" and partes == ["estado"]:
        return estado(reporte)

    respuesta: dict = {}
    if metodo == "PUT" and partes == ["reporte"]:
        rd.actualizar_cabecera(reporte, cuerpo)
    elif metodo == "POST" and partes == ["tareas"]:
        # Una tarea nueva de la página no puede traer id, TK, resultados ni origen.
        t = rd.tarea_desde_dict(cuerpo, confiable=False)
        reporte.tareas.append(t)
        respuesta["nueva"] = t.id
    elif metodo == "POST" and partes == ["tareas", "lote"]:
        # Varias tareas a la vez («-Tarea/WI/puntos» por línea); si una falla, no entra ninguna.
        nuevas = rd.tareas_desde_lote(cuerpo)
        reporte.tareas.extend(nuevas)
        respuesta["nuevas"] = [t.id for t in nuevas]
        con_tk = sum(1 for t in nuevas if t.work_items)
        respuesta["aviso"] = (f"Se agregaron {len(nuevas)} tarea(s); {con_tk} con work item "
                              f"(crearán TK) y {len(nuevas) - con_tk} sin work item.")
    elif metodo == "PUT" and len(partes) == 2 and partes[0] == "tareas":
        anterior = reporte.buscar(partes[1])
        nueva = rd.tarea_desde_dict(cuerpo, anterior)
        reporte.tareas[reporte.tareas.index(anterior)] = nueva
        rd.recordar_tarea(reporte, nueva)
    elif metodo == "DELETE" and len(partes) == 2 and partes[0] == "tareas":
        rd.quitar_tarea(reporte, partes[1])
    elif metodo == "POST" and partes == ["subir"]:
        destinos = [d for d in cuerpo.get("destinos", []) if d in rd.DESTINOS]
        if not destinos:
            raise ValueError("Elige al menos un destino.")
        simular = bool(cuerpo.get("simular"))
        respuesta["subida"] = rd.publicar(reporte, list(cuerpo.get("ids") or []), destinos, simular)
        if simular:  # una simulación no cambia nada guardado
            return {**estado(reporte), **respuesta}
    elif metodo == "POST" and partes == ["calendar"]:
        info = rd.importar_calendar(reporte)
        partes_aviso = [f"Se importaron {info['agregadas']} reunión(es) de Calendar"]
        if info["repetidas"]:
            partes_aviso.append(f"{info['repetidas']} ya estaban")
        if info["movidas"]:
            partes_aviso.append(f"{info['movidas']} cambiaron de hora y se actualizaron")
        if info["fuera"]:
            partes_aviso.append(f"{info['fuera']} de otros días no se importaron (solo entran «ayer» y «hoy»)")
        respuesta["aviso"] = "; ".join(partes_aviso) + "."
    elif metodo == "POST" and partes == ["limpiar"]:
        cantidad, archivo = rd.limpiar_tareas(reporte)
        respuesta["aviso"] = (f"Se quitaron {cantidad} tarea(s) de la lista. Copia de respaldo: "
                              f"historial/{archivo.name}.")
    elif metodo == "POST" and partes == ["nuevo-dia"]:
        archivo = rd.nuevo_dia(reporte, forzar=bool(cuerpo.get("forzar")))
        respuesta["aviso"] = (f"Nuevo día {reporte.fecha}: lo de hoy pasó a «ayer». "
                              f"El reporte anterior quedó en historial/{archivo.name}.")
    else:
        raise NoEncontrado()

    rd.guardar_reporte(reporte)
    return {**estado(reporte), **respuesta}


class Manejador(BaseHTTPRequestHandler):
    server_version = "ReporteDiario/1.0"
    timeout = 60  # segundos de espera máxima al leer una petición

    def log_message(self, formato, *args):  # silencia el log por petición
        log.debug(formato, *args)

    def _responder(self, codigo: int, cuerpo: bytes, tipo: str) -> None:
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _json(self, codigo: int, obj: dict) -> None:
        self._responder(codigo, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def _host_valido(self) -> bool:
        # Solo se atiende a 127.0.0.1/localhost (evita ataques de "DNS rebinding").
        return self.headers.get("Host", "") in {f"127.0.0.1:{PUERTO}", f"localhost:{PUERTO}"}

    def _llave_valida(self) -> bool:
        for parte in self.headers.get("Cookie", "").split(";"):
            nombre, _, valor = parte.strip().partition("=")
            if nombre == COOKIE and hmac.compare_digest(valor, LLAVE):
                return True
        return False

    def do_GET(self):
        if not self._host_valido():
            return self._json(403, {"error": "Origen no permitido."})
        ruta = urlparse(self.path)
        k = parse_qs(ruta.query).get("k", [""])[0]
        if ruta.path == "/" and k:
            if not hmac.compare_digest(k, LLAVE):
                return self._json(403, {"error": "Llave inválida: abre la página desde el enlace de la consola."})
            # Cookie solo para esta página (no la puede leer JavaScript) y luego a la URL limpia.
            self.send_response(303)
            self.send_header("Set-Cookie", f"{COOKIE}={LLAVE}; HttpOnly; SameSite=Strict; Path=/; "
                                           "Max-Age=31536000")
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if ruta.path in ESTATICOS:
            nombre, tipo = ESTATICOS[ruta.path]
            return self._responder(200, (WEB_DIR / nombre).read_bytes(), tipo)
        self._api("GET", ruta)

    def do_POST(self):
        self._api("POST", urlparse(self.path))

    def do_PUT(self):
        self._api("PUT", urlparse(self.path))

    def do_DELETE(self):
        self._api("DELETE", urlparse(self.path))

    def _api(self, metodo: str, ruta) -> None:
        if not self._host_valido():
            return self._json(403, {"error": "Origen no permitido."})
        # Cabecera propia: otra página abierta en el navegador no puede enviarla
        # sin permiso CORS, así que no puede usar esta API a tus espaldas.
        if self.headers.get("X-Reporte") != "1":
            return self._json(403, {"error": "Petición no permitida."})
        if not self._llave_valida():
            return self._json(401, {"error": "Abre la página desde el enlace que muestra la consola de "
                                             "«python servidor.py» (incluye tu llave local).", "sin_llave": True})
        partes = ruta.path.strip("/").split("/")
        if not partes or partes[0] != "api":
            return self._json(404, {"error": "No encontrado."})
        try:
            cuerpo = {}
            if metodo in ("POST", "PUT"):
                try:
                    largo = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise ValueError("Content-Length inválido.") from None
                if not 0 <= largo <= 2_000_000:
                    raise ValueError("Petición demasiado grande o inválida.")
                try:
                    cuerpo = json.loads(self.rfile.read(largo) or b"{}")
                except ValueError:
                    raise ValueError("La petición no trae un JSON válido.") from None
                if not isinstance(cuerpo, dict):
                    raise ValueError("La petición debe ser un objeto JSON.")
            if partes[1:2] == ["sesion"]:
                respuesta = despachar_sesion(metodo, partes[1:])
            elif metodo == "POST" and partes[1:] == ["tareas", "lote", "revisar"]:
                # Vista previa: no cambia reporte_hoy.json (va sin candado; consulta Azure).
                respuesta = rd.revisar_lote(cuerpo.get("texto"))
            elif metodo == "POST" and partes[1:] == ["apagar"]:
                threading.Thread(target=apagar, daemon=True).start()
                respuesta = {"aviso": "Programa cerrado."}
            elif metodo == "POST" and partes[1:] == ["tareas", "lote", "archivo"]:
                # Lee un .txt o .docx adjunto y devuelve su texto (no guarda nada).
                respuesta = {"texto": rd.texto_de_archivo(cuerpo.get("nombre"), cuerpo.get("contenido"))}
            elif metodo == "GET" and partes[1:] in CONSULTAS:
                respuesta = despachar_consulta(partes[1:], parse_qs(ruta.query))
            else:
                with CANDADO:
                    respuesta = despachar(metodo, partes[1:], parse_qs(ruta.query), cuerpo)
            self._json(200, respuesta)
        except NoEncontrado:
            self._json(404, {"error": "Ruta no encontrada."})
        except rd.DiaNoHabil as e:
            self._json(409, {"error": str(e), "confirmar": True})
        except rd.TareaNoEncontrada as e:
            self._json(404, {"error": f"La tarea {e} ya no está en la lista (recarga la página)."})
        except (ValueError, RuntimeError) as e:
            self._json(400, {"error": rd.sin_secretos(e)})
        except Exception as e:  # noqa: BLE001 — cualquier otro fallo se muestra en la página
            log.exception("Error atendiendo %s %s", metodo, ruta.path)
            self._json(500, {"error": f"Error inesperado: {rd.sin_secretos(e)}"})


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador)
    except OSError:
        log.error("✖ El puerto %s ya está en uso. ¿Ya hay otra ventana con el servidor abierta? "
                  "Ciérrala o define REPORTE_PUERTO en el .env.", PUERTO)
        return 1

    global SERVIDOR
    SERVIDOR = servidor
    url = f"http://127.0.0.1:{PUERTO}/?k={LLAVE}"
    log.info("Reporte diario disponible en:\n  %s\n(Ese enlace incluye tu llave local: no lo compartas. "
             "Después de abrirlo una vez, basta con http://127.0.0.1:%s/)\n"
             "Deja esta ventana abierta; Ctrl+C para cerrar.", url, PUERTO)
    if "--no-abrir" not in sys.argv:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        log.info("Servidor cerrado.")
    finally:
        servidor.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
