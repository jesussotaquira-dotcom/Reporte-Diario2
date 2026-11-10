"""
reporte_diario.py — Núcleo y línea de comandos del reporte diario (daily).

El reporte sigue el formato del equipo:

    Buen día,
    ¿Qué hice ayer?                    → tareas de la sección "ayer"
    ¿Qué voy a hacer hoy?              → tareas de la sección "hoy"
    ¿Tengo algún blocker / stopper? No

y se publica en tres destinos:

  1. Azure DevOps : por cada tarea, un TK hijo de cada work item relacionado
                    (una HU, la Feature de una célula, etc.); aunque dos tareas
                    compartan work item, cada una tiene su propio TK.
                    Las tareas sin work item (p. ej. Pausas Activas) no crean TK.
  2. Google Drive : crea/actualiza el Google Doc "Reporte diario - AAAA-MM-DD".
  3. Google Chat  : envía el reporte al espacio vía webhook.
  4. RIPOR        : un registro de horas por día con el texto del daily y el
                    enlace al Doc de Drive como evidencia, con el conector
                    oficial (ripor-mcp.mjs).

Las tareas se arman desde la interfaz web (python servidor.py), que las guarda
en reporte_hoy.json. Este archivo también se puede usar desde la consola:

    python reporte_diario.py                       # sube todas las tareas (sin RIPOR)
    python reporte_diario.py --solo devops chat    # solo algunos destinos
    python reporte_diario.py --solo ripor          # RIPOR solo si se pide explícitamente
    python reporte_diario.py --dry-run             # muestra qué haría, sin enviar
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("reporte")

# Valores que acepta el tipo de work item TK en Azure DevOps (proceso de Linktic).
TIPOS_TK = {"gestion": "Gestión", "gestión": "Gestión", "producto": "Producto"}
PUNTOS_VALIDOS = {1, 2, 3, 5, 8, 13}
# Qué significa cada valor de puntos, para elegirlo en la página. Cada equipo
# puede poner su propia escala en el .env: ADO_PUNTOS_GUIA="1=…;2=…;3=…".
GUIA_PUNTOS = {
    1: "Muy pequeña: unas horas",
    2: "Pequeña: hasta 1 día",
    3: "Mediana: 1 a 2 días",
    5: "Grande: 2 a 3 días",
    8: "Muy grande: 3 a 5 días",
    13: "Enorme: más de una semana (mejor dividirla)",
}


def guia_puntos() -> dict[int, str]:
    """La guía de puntos, con lo que defina ADO_PUNTOS_GUIA en el .env por encima."""
    guia = dict(GUIA_PUNTOS)
    for parte in os.getenv("ADO_PUNTOS_GUIA", "").split(";"):
        numero, _, texto = parte.partition("=")
        if numero.strip().isdigit() and int(numero) in PUNTOS_VALIDOS and texto.strip():
            guia[int(numero)] = texto.strip()
    return guia
# Estados del TK, del más abierto al más cerrado (el TK no tiene Removed).
ESTADOS_TK = ("New", "Active", "Closed")
NOMBRES_ESTADO = {"new": "New", "nuevo": "New", "active": "Active", "activo": "Active",
                  "abierto": "Active", "closed": "Closed", "cerrado": "Closed"}

# Medios de transporte que acepta RIPOR (mismos ids que su conector).
TRANSPORTES = {
    "vehiculo_particular": "Vehículo particular",
    "moto_particular": "Moto particular",
    "transporte_publico": "Transporte público",
    "bicicleta": "Bicicleta",
    "patineta_electrica": "Patineta eléctrica",
    "caminata": "Caminata",
    "carpooling": "Carro compartido",
    "apps_movilidad": "Apps de movilidad (Uber, Picap, etc.)",
    "trabajo_remoto": "Trabajo remoto",
}

SECCIONES = {"ayer": "¿Qué hice ayer?", "hoy": "¿Qué voy a hacer hoy?"}
PREGUNTA_BLOCKER = "¿Tengo algún blocker / stopper?"
DESTINOS = ("devops", "drive", "chat", "ripor")
# Desde la consola RIPOR solo se usa si se pide con --solo: un registro de
# horas no se puede editar ni borrar después.
DESTINOS_CONSOLA = ("devops", "drive", "chat")


def ruta_reporte(ruta: str | Path | None = None) -> Path:
    """Archivo donde viven las tareas (REPORTE_ARCHIVO permite usar otro)."""
    return Path(ruta or os.getenv("REPORTE_ARCHIVO") or BASE_DIR / "reporte_hoy.json")


# ---------------------------------------------------------------------------
# 1. Modelo de datos del reporte
# ---------------------------------------------------------------------------

@dataclass
class Tarea:
    """Una línea del reporte, p. ej. "Participación en la sesión de Daily Emisión."."""
    seccion: str                           # "ayer" | "hoy"
    descripcion: str                       # texto tal como va en el reporte
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    categoria: str = ""                    # agrupación libre en la interfaz
    work_items: list[int] = field(default_factory=list)  # HU / Feature de las que cuelga el TK
    wi_titulos: dict = field(default_factory=dict)       # id → título, solo para mostrarlo
    tarea: str = ""                        # nombre corto para el título del TK
    tipo: str = ""                         # Tipo_TK: Gestión | Producto (vacío = el general)
    estado_tk: str = ""                    # New | Active | Closed (vacío = ADO_TK_STATE)
    inicio: str = ""                       # "HH:MM" (hora de Colombia)
    fin: str = ""                          # "HH:MM"
    horas: float | None = None             # alternativa a "fin": Fecha Fin = inicio + horas
    puntos: int | None = None              # vacío = los puntos del work item
    crear_tk: bool = True                  # False = solo va a Drive y Chat
    origen: str = "manual"                 # "manual" o "calendar:<id>|<inicio>"
    evento: str = ""                       # título original de la reunión de Calendar
    tks: list[dict] = field(default_factory=list)    # TK creados: {wi, tk, url}
    resultado: dict = field(default_factory=dict)    # último resultado de subida

    @property
    def nombre_tk(self) -> str:
        texto = self.tarea or self.descripcion.splitlines()[0].rstrip(" .")
        return texto[:150]


@dataclass
class Reporte:
    fecha: str                             # día del reporte ("hoy")
    fecha_ayer: str                        # día al que corresponde "¿Qué hice ayer?"
    sprint: int | None = None
    tipo: str = ""                         # Tipo_TK por defecto
    autor: str = ""
    saludo: str = "Buen día"
    blocker: str = "No"
    ripor_proyecto: int | None = None      # proyecto y tarea de RIPOR (vacío = se eligen solos)
    ripor_tarea: int | None = None
    ripor_nombre: str = ""                 # "CECO · proyecto › tarea", para mostrarlo
    ripor_horas: float | None = None       # vacío = 8,5 (lun–jue) / 8 (vie) menos lo ya reportado
    transporte: str = ""                   # medio de transporte (queda hasta que se cambie)
    drive_url: str = ""                    # Doc del día; es la evidencia en RIPOR
    drive_urls: dict = field(default_factory=dict)       # fecha → enlace del Doc de ese día
    ripor_fecha: str = ""                  # día a registrar en RIPOR (vacío = el del reporte)
    ripor_evidencia: str = ""              # enlace pegado a mano (vacío = el Doc de ese día)
    ripor_registros: dict = field(default_factory=dict)  # fecha → cuándo se registró en RIPOR
    drive_doc: dict = field(default_factory=dict)         # fecha → tareas que ya están en el Doc
    tareas: list[Tarea] = field(default_factory=list)
    recordar: dict = field(default_factory=dict)  # reunión de Calendar → work items, categoría, tipo

    def fecha_de(self, t: Tarea) -> str:
        """Fecha del TK: la del reporte para "hoy" y la de ayer para "ayer"."""
        return self.fecha if t.seccion == "hoy" else self.fecha_ayer

    def buscar(self, tarea_id: str) -> Tarea:
        for t in self.tareas:
            if t.id == tarea_id:
                return t
        raise TareaNoEncontrada(tarea_id)

    def a_dict(self) -> dict:
        return asdict(self)


class TareaNoEncontrada(LookupError):
    """La tarea pedida ya no está en el reporte (se borró, se archivó o se limpió)."""


# -- Validaciones (las usan el JSON, la consola y la interfaz web) ----------

def _pascua(anio: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano anónimo)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return date(anio, mes, dia)


def festivos_colombia(anio: int) -> set[date]:
    """Festivos nacionales de Colombia (Ley 51 de 1983: varios se trasladan al lunes siguiente)."""
    def lunes(d: date) -> date:
        return d + timedelta(days=(7 - d.weekday()) % 7)

    pascua = _pascua(anio)
    fijos = {date(anio, 1, 1), date(anio, 5, 1), date(anio, 7, 20), date(anio, 8, 7),
             date(anio, 12, 8), date(anio, 12, 25),
             pascua - timedelta(days=3), pascua - timedelta(days=2)}  # Jueves y Viernes Santo
    trasladables = [date(anio, 1, 6), date(anio, 3, 19), date(anio, 6, 29), date(anio, 8, 15),
                    date(anio, 10, 12), date(anio, 11, 1), date(anio, 11, 11),
                    pascua + timedelta(days=39), pascua + timedelta(days=60), pascua + timedelta(days=68)]
    return fijos | {lunes(d) for d in trasladables}


def es_habil(dia: date) -> bool:
    return dia.weekday() < 5 and dia not in festivos_colombia(dia.year)


def dia_habil_anterior(dia: date) -> date:
    """El día hábil anterior (sin sábados, domingos ni festivos de Colombia)."""
    dia -= timedelta(days=1)
    while not es_habil(dia):
        dia -= timedelta(days=1)
    return dia


class DiaNoHabil(ValueError):
    """Se pidió iniciar el día en un sábado, domingo o festivo."""


def es_doc_drive(url: str) -> bool:
    return bool(re.fullmatch(r"https://docs\.google\.com/\S+", str(url or "")))


def _fecha(valor, nombre: str) -> str:
    try:
        dia = date.fromisoformat(str(valor))
    except ValueError:
        raise ValueError(f"{nombre} inválida {valor!r} (usa AAAA-MM-DD).") from None
    if not 2000 <= dia.year <= 2100:
        raise ValueError(f"{nombre} fuera de rango {valor!r}.")
    return dia.isoformat()


def _num_sprint(valor) -> int | None:
    """Acepta 16, "16", "S16" o "Sprint 16" y devuelve 16."""
    if valor in (None, ""):
        return None
    m = re.search(r"\d+", str(valor))
    if not m:
        raise ValueError(f"sprint inválido {valor!r} (usa por ejemplo 16 o \"S16\").")
    return int(m.group())


def _tipo_tk(valor) -> str:
    if not valor:
        return ""
    tipo = TIPOS_TK.get(str(valor).strip().lower())
    if not tipo:
        raise ValueError(f"tipo inválido {valor!r} (usa \"Gestión\" o \"Producto\").")
    return tipo


def _estado_tk(valor) -> str:
    """Acepta "Closed"/"cerrado", "Active"/"activo"/"abierto" o "New"/"nuevo"."""
    if not valor:
        return ""
    estado = NOMBRES_ESTADO.get(str(valor).strip().lower())
    if not estado:
        raise ValueError(f"estado del TK inválido {valor!r} (usa {', '.join(ESTADOS_TK)}).")
    return estado


def _id_ripor(valor, nombre: str) -> int | None:
    if valor in (None, ""):
        return None
    if not str(valor).isdigit() or int(valor) <= 0:
        raise ValueError(f"{nombre} de RIPOR inválido {valor!r}.")
    return int(valor)


def _transporte(valor) -> str:
    valor = str(valor or "").strip()
    if valor and valor not in TRANSPORTES:
        raise ValueError(f"medio de transporte inválido {valor!r}.")
    return valor


def _hora(valor) -> str:
    """Valida "HH:MM" y la normaliza ("7:30" → "07:30")."""
    valor = str(valor or "").strip()
    if not valor:
        return ""
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", valor)
    if not m:
        raise ValueError(f"hora inválida {valor!r} (usa HH:MM, por ejemplo \"07:30\").")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _horas(valor) -> float | None:
    if valor in (None, ""):
        return None
    try:
        horas = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f"horas inválidas {valor!r} (usa un número, por ejemplo 1.5).") from None
    if not 0 < horas <= 24:
        raise ValueError(f"horas inválidas {valor!r} (deben estar entre 0 y 24).")
    return horas


def _puntos(valor) -> int | None:
    if valor in (None, ""):
        return None
    try:
        puntos = int(float(valor))
    except (TypeError, ValueError):
        puntos = None
    if puntos not in PUNTOS_VALIDOS:
        raise ValueError(f"puntos inválidos {valor!r} (válidos: {sorted(PUNTOS_VALIDOS)}).")
    return puntos


def _bool(valor, defecto: bool = True) -> bool:
    if valor is None:
        return defecto
    if isinstance(valor, str):
        return valor.strip().lower() not in ("", "false", "0", "no", "falso")
    return bool(valor)


def _work_items(valor) -> list[int]:
    """Acepta [123, "456"] o "123, 456" y devuelve [123, 456] sin repetidos."""
    if valor in (None, ""):
        return []
    if isinstance(valor, str):
        partes = re.split(r"[,;\s]+", valor.strip())
    elif isinstance(valor, (int, float)):
        partes = [valor]
    elif isinstance(valor, (list, tuple)):
        partes = list(valor)
    else:
        raise ValueError(f"work items inválidos {valor!r}.")
    ids: list[int] = []
    for p in partes:
        if p in ("", None):
            continue
        if isinstance(p, bool) or not str(p).isdigit() or int(p) <= 0:
            raise ValueError(f"work item inválido {p!r} (usa el número del work item).")
        if int(p) not in ids:
            ids.append(int(p))
    return ids


def _tks_validos(lista) -> list[dict]:
    """TK de una tarea: números enteros y enlaces https (descarta lo que no lo sea)."""
    validos = []
    for x in lista if isinstance(lista, list) else []:
        if not isinstance(x, dict):
            continue
        try:
            tk, wi = int(x.get("tk")), int(x.get("wi"))
        except (TypeError, ValueError):
            continue
        url = str(x.get("url") or "")
        validos.append({**x, "tk": tk, "wi": wi, "url": url if url.startswith("https://") else ""})
    return validos


def tarea_desde_dict(datos: dict, anterior: Tarea | None = None, confiable: bool = True) -> Tarea:
    """
    Valida una tarea (del JSON o del formulario web) y lanza ValueError con un
    mensaje claro si algo está mal. Si se edita una tarea existente (anterior),
    se conservan su id, su origen y lo que ya se subió. Una tarea nueva que
    llega de la página (confiable=False) no puede traer TK, resultados ni
    origen: esos datos los pone solo el programa.
    """
    seccion = str(datos.get("seccion") or "").strip().lower()
    if seccion not in SECCIONES:
        raise ValueError("la sección debe ser 'ayer' o 'hoy'.")
    descripcion = str(datos.get("descripcion") or "").strip()
    if not descripcion:
        raise ValueError("la descripción es obligatoria.")
    inicio, fin = _hora(datos.get("inicio")), _hora(datos.get("fin"))
    if inicio and fin and fin <= inicio:
        raise ValueError("la hora fin debe ser posterior a la de inicio.")

    work_items = _work_items(datos.get("work_items"))
    titulos = datos.get("wi_titulos") if isinstance(datos.get("wi_titulos"), dict) else {}
    t = Tarea(
        seccion=seccion,
        descripcion=descripcion,
        categoria=str(datos.get("categoria") or "").strip(),
        work_items=work_items,
        wi_titulos={str(w): str(titulos[str(w)])[:255] for w in work_items if titulos.get(str(w))},
        tarea=str(datos.get("tarea") or "").strip(),
        tipo=_tipo_tk(datos.get("tipo")),
        estado_tk=_estado_tk(datos.get("estado_tk")),
        inicio=inicio,
        fin=fin,
        horas=_horas(datos.get("horas")),
        puntos=_puntos(datos.get("puntos")),
        crear_tk=_bool(datos.get("crear_tk")),
    )
    previo = asdict(anterior) if anterior else (datos if confiable else {})
    t.id = str(previo.get("id") or t.id)
    t.origen = str(previo.get("origen") or "manual")
    t.evento = str(previo.get("evento") or "")
    t.tks = _tks_validos(previo.get("tks"))
    t.resultado = dict(previo.get("resultado") or {}) if isinstance(previo.get("resultado"), dict) else {}
    return t


def actualizar_cabecera(reporte: Reporte, datos: dict, carga: bool = False) -> None:
    """
    Fecha, sprint, saludo, blocker, etc. (los campos que no son tareas).
    carga=True al leer el JSON guardado: lo que haya quedado incoherente se
    corrige en vez de fallar, para que el reporte siempre se pueda abrir.
    """
    if "fecha" in datos:
        nueva = _fecha(datos["fecha"], "fecha")
        if nueva != reporte.fecha and not carga:
            # Otro día: su Doc de Drive es otro (si ya se subió, se conoce su enlace).
            reporte.drive_url = reporte.drive_urls.get(nueva, "")
        reporte.fecha = nueva
    if "fecha_ayer" in datos:
        ayer = _fecha(datos["fecha_ayer"], "fecha de ayer")
        if ayer >= reporte.fecha and not carga and ayer != reporte.fecha_ayer:
            raise ValueError("La fecha de «ayer» debe ser anterior a la fecha del reporte.")
        reporte.fecha_ayer = ayer
    if reporte.fecha_ayer >= reporte.fecha:  # p. ej. se movió la fecha del reporte hacia atrás
        reporte.fecha_ayer = dia_habil_anterior(date.fromisoformat(reporte.fecha)).isoformat()
    if "sprint" in datos:
        reporte.sprint = _num_sprint(datos["sprint"])
    if "tipo" in datos:
        reporte.tipo = _tipo_tk(datos["tipo"])
    if "ripor_proyecto" in datos:
        reporte.ripor_proyecto = _id_ripor(datos["ripor_proyecto"], "proyecto")
        reporte.ripor_tarea = (_id_ripor(datos.get("ripor_tarea"), "tarea")
                               if reporte.ripor_proyecto else None)
        reporte.ripor_nombre = (str(datos.get("ripor_nombre") or "").strip()[:200]
                                if reporte.ripor_proyecto else "")
    if "ripor_horas" in datos:
        horas = _horas(datos["ripor_horas"])
        if horas is not None and horas < 1:
            raise ValueError("RIPOR pide al menos 1 hora por registro.")
        reporte.ripor_horas = horas
    if "transporte" in datos:
        reporte.transporte = _transporte(datos["transporte"])
    if "ripor_fecha" in datos:
        try:
            fecha = _fecha(datos["ripor_fecha"], "fecha de RIPOR") if datos["ripor_fecha"] else ""
        except ValueError:
            if not carga:
                raise
            fecha = ""
        if fecha and fecha > reporte.fecha:
            if not carga:
                raise ValueError("El día a reportar en RIPOR no puede ser posterior a la fecha del reporte.")
            fecha = ""
        fecha = "" if fecha == reporte.fecha else fecha
        if fecha != reporte.ripor_fecha and not carga and "ripor_evidencia" not in datos:
            reporte.ripor_evidencia = ""  # el enlace pegado era de otro día
        reporte.ripor_fecha = fecha
    if reporte.ripor_fecha and reporte.ripor_fecha >= reporte.fecha:
        reporte.ripor_fecha = ""  # la fecha del reporte se movió hacia atrás
    if "ripor_evidencia" in datos:
        enlace = str(datos["ripor_evidencia"] or "").strip()
        if enlace and not re.fullmatch(r"https?://\S+", enlace):
            raise ValueError("La evidencia debe ser un enlace que empiece por http:// o https://.")
        reporte.ripor_evidencia = enlace
    for campo in ("autor", "saludo", "blocker"):
        if campo in datos:
            setattr(reporte, campo, str(datos[campo] or "").strip())


def reporte_desde_dict(datos: dict) -> Reporte:
    fecha = _fecha(datos.get("fecha") or date.today().isoformat(), "fecha")
    reporte = Reporte(
        fecha=fecha,
        fecha_ayer=dia_habil_anterior(date.fromisoformat(fecha)).isoformat(),
        autor=os.getenv("REPORTE_AUTOR", ""),
        recordar=dict(datos.get("recordar") or {}),
        ripor_registros=dict(datos.get("ripor_registros") or {}),
        drive_urls={f: u for f, u in dict(datos.get("drive_urls") or {}).items() if es_doc_drive(u)},
        drive_doc=dict(datos.get("drive_doc") or {}),
    )
    actualizar_cabecera(reporte, {k: v for k, v in datos.items()
                                  if k in ("fecha_ayer", "sprint", "tipo", "autor", "saludo", "blocker",
                                           "ripor_proyecto", "ripor_tarea", "ripor_nombre", "ripor_horas",
                                           "transporte", "ripor_fecha", "ripor_evidencia")
                                  and v is not None}, carga=True)
    if es_doc_drive(datos.get("drive_url")):
        reporte.drive_url = datos["drive_url"]
        if "drive_urls" not in datos:  # reportes guardados antes de drive_urls
            reporte.drive_urls[reporte.fecha] = reporte.drive_url
    # El medio de transporte queda predeterminado: el guardado o, si no hay, el del .env.
    if not reporte.transporte:
        reporte.transporte = _transporte(os.getenv("RIPOR_TRANSPORTE", ""))

    # Compatibilidad con el formato anterior ({"hus": [...]}).
    filas = datos.get("tareas")
    if filas is None and "hus" in datos:
        filas = [{"seccion": "hoy", "descripcion": h.get("descripcion"), "work_items": [h.get("id")],
                  "tarea": h.get("tarea"), "tipo": h.get("tipo"), "horas": h.get("horas"),
                  "puntos": h.get("puntos"), "inicio": h.get("inicio")} for h in datos["hus"]]

    for i, fila in enumerate(filas or [], start=1):
        try:
            reporte.tareas.append(tarea_desde_dict(fila))
        except ValueError as e:
            resumen = str(fila.get("descripcion") or "")[:40]
            raise ValueError(f"Tarea #{i} ({resumen!r}): {e}") from None
    return reporte


def cargar_reporte(ruta: str | Path | None = None) -> Reporte:
    """Lee el reporte (tolera el BOM que agrega PowerShell). Si no existe, uno vacío."""
    archivo = ruta_reporte(ruta)
    if not archivo.exists():
        return reporte_desde_dict({})
    return reporte_desde_dict(json.loads(archivo.read_text(encoding="utf-8-sig")))


def guardar_reporte(reporte: Reporte, ruta: str | Path | None = None) -> None:
    """Escritura atómica: primero a un temporal y luego se reemplaza."""
    archivo = ruta_reporte(ruta)
    temporal = archivo.with_suffix(".tmp")
    temporal.write_text(json.dumps(reporte.a_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporal, archivo)


# Inicio de cada tarea en el texto de «Varias a la vez»: un guion al empezar la
# línea, o después de un espacio y pegado al texto (" -Otra tarea"). Así
# "HU-282" o "Cotización - Solicitud" no se parten.
_INICIO_TAREA = re.compile(r"(?:^|\s)-(?=\S)")
_LISTA_WI = re.compile(r"\d+(?:\s*,\s*\d+)*")
PUNTOS_LOTE = 1  # puntos de una tarea del lote con work item y sin puntos


def leer_lote(texto: str) -> list[dict]:
    """
    Convierte el texto de «Varias a la vez» en tareas. Formato de cada una:

        -Descripción/WI/puntos

    WI y puntos son opcionales: sin WI la tarea no crea TK; con WI y sin
    puntos lleva PUNTOS_LOTE. El WI puede ser una lista ("346364,482680").
    La descripción puede tener "/": solo se toman como WI y puntos los
    últimos tramos si son números. Devuelve [{linea, descripcion, work_items,
    puntos, puntos_defecto}] o lanza ValueError con todos los errores.
    """
    tareas: list[dict] = []
    errores: list[str] = []
    for n, linea in enumerate(str(texto or "").splitlines(), start=1):
        if not linea.strip():
            continue
        for trozo in _INICIO_TAREA.split(linea):
            trozo = trozo.strip()
            if not trozo:
                continue
            partes = [p.strip() for p in trozo.split("/")]
            wi_txt = puntos_txt = ""
            if len(partes) >= 3 and _LISTA_WI.fullmatch(partes[-2]) and re.fullmatch(r"\d*", partes[-1]):
                wi_txt, puntos_txt, partes = partes[-2], partes[-1], partes[:-2]
            elif len(partes) >= 3 and partes[-2] == "" and partes[-1].isdigit():
                errores.append(f"línea {n} («{trozo}»): tiene puntos pero no work item.")
                continue
            elif len(partes) >= 2 and (_LISTA_WI.fullmatch(partes[-1]) or partes[-1] == ""):
                wi_txt, partes = partes[-1], partes[:-1]
            descripcion = "/".join(partes).strip()
            if not descripcion:
                errores.append(f"línea {n} («{trozo}»): falta la descripción de la tarea.")
                continue
            try:
                work_items = _work_items(wi_txt)
                puntos = _puntos(puntos_txt)
            except ValueError as e:
                errores.append(f"línea {n} («{trozo}»): {e}")
                continue
            defecto = bool(work_items) and puntos is None
            tareas.append({"linea": n, "descripcion": descripcion, "work_items": work_items,
                           "puntos": PUNTOS_LOTE if defecto else (puntos if work_items else None),
                           "puntos_defecto": defecto})
    if errores:
        raise ValueError("revisa el texto: " + " ".join(errores))
    if not tareas:
        raise ValueError("no hay tareas. Escribe una por línea, p. ej. «-Informe semanal/346364/1».")
    return tareas


MAX_ARCHIVO_LOTE = 1_000_000          # bytes del archivo adjunto (cabe en la petición en base64)
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# Viñetas que Word o un editor ponen en lugar del guion ("– Tarea", "• Tarea").
_VINETA = re.compile(r"^\s*[\-–—•·▪◦●○*]\s*")


def _lineas_docx(datos: bytes) -> list[str]:
    """Párrafos de un .docx (sin librerías: es un zip con word/document.xml)."""
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    try:
        with zipfile.ZipFile(io.BytesIO(datos)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 20_000_000:  # evita un zip que se infla al abrirlo
                raise ValueError("el documento es demasiado grande.")
            raiz = ET.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError("no es un documento de Word (.docx) válido. Si es .doc, guárdalo como .docx.") from None
    lineas = []
    for p in raiz.iter(f"{_W}p"):
        texto = "".join((n.text or "") if n.tag == f"{_W}t" else ("\t" if n.tag == f"{_W}tab" else "\n")
                        for n in p.iter() if n.tag in (f"{_W}t", f"{_W}tab", f"{_W}br"))
        # Una lista con viñetas de Word no tiene el guion en el texto: se le pone.
        en_lista = p.find(f"{_W}pPr/{_W}numPr") is not None
        for parte in texto.splitlines() or [""]:
            if en_lista and parte.strip() and not _VINETA.match(parte):
                parte = "-" + parte.strip()
            lineas.append(parte)
    return lineas


def texto_de_archivo(nombre: str, contenido_b64: str) -> str:
    """
    Lee un .txt o .docx adjunto en la página y devuelve su texto en el formato
    «-Tarea/WI/puntos» (las viñetas «–», «•»… se convierten en «-»). No agrega
    nada: el texto va al cuadro de «Varias a la vez» para revisarlo.
    """
    import binascii
    nombre = str(nombre or "").strip()
    extension = Path(nombre).suffix.lower()
    if extension not in (".txt", ".docx"):
        raise ValueError(f"«{nombre}» no es .txt ni .docx (Word).")
    try:
        datos = base64.b64decode(str(contenido_b64 or ""), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("el archivo no llegó bien; vuelve a adjuntarlo.") from None
    if len(datos) > MAX_ARCHIVO_LOTE:
        raise ValueError(f"el archivo pesa más de {MAX_ARCHIVO_LOTE // 1_000_000} MB.")
    if extension == ".docx":
        lineas = _lineas_docx(datos)
    else:
        for codificacion in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                if codificacion == "utf-16" and not datos.startswith((b"\xff\xfe", b"\xfe\xff")):
                    continue
                lineas = datos.decode(codificacion).splitlines()
                break
            except UnicodeDecodeError:
                continue
    lineas = [_VINETA.sub("-", l, count=1) if _VINETA.match(l) else l for l in lineas]
    texto = "\n".join(l.rstrip() for l in lineas if l.strip())
    if not texto:
        raise ValueError(f"«{nombre}» no tiene texto.")
    return texto


def tareas_desde_lote(datos: dict) -> list[Tarea]:
    """Tareas del lote con la sección y la categoría elegidas en la página."""
    titulos = datos.get("wi_titulos") if isinstance(datos.get("wi_titulos"), dict) else {}
    return [tarea_desde_dict({"seccion": datos.get("seccion"), "categoria": datos.get("categoria"),
                              "tipo": datos.get("tipo"), "estado_tk": datos.get("estado_tk"),
                              "descripcion": x["descripcion"], "work_items": x["work_items"],
                              "puntos": x["puntos"], "wi_titulos": titulos}, confiable=False)
            for x in leer_lote(datos.get("texto"))]


def revisar_lote(texto: str) -> dict:
    """Vista previa del lote: las tareas leídas y, si se puede, el título de cada WI."""
    tareas = leer_lote(texto)
    ids = sorted({w for x in tareas for w in x["work_items"]})
    info: dict[int, dict] = {}
    aviso = ""
    if ids:
        try:
            info = AzureDevOps().info_wis(ids)
        except (RuntimeError, requests.RequestException) as e:
            aviso = f"No se pudieron verificar los work items en Azure DevOps ({sin_secretos(e)})."
    faltan = [w for w in ids if not aviso and w not in info]
    return {"tareas": tareas, "wis": {str(k): v for k, v in info.items()},
            "faltan": faltan, "aviso": aviso}


def recordar_tarea(reporte: Reporte, t: Tarea) -> None:
    """
    Memoriza a qué work items, categoría y tipo corresponde una reunión de
    Calendar, para que la próxima vez que se importe ya venga relacionada.
    """
    clave = t.evento or t.tarea  # el título de la reunión, aunque se le cambie el nombre corto
    if t.origen.startswith("calendar:") and clave:
        reporte.recordar[clave] = {"work_items": t.work_items, "wi_titulos": t.wi_titulos,
                                   "categoria": t.categoria, "tipo": t.tipo}


def nuevo_dia(reporte: Reporte, hoy: date | None = None, forzar: bool = False) -> Path:
    """
    Empieza el reporte de un nuevo día: lo de "hoy" pasa a "ayer", lo que
    estaba en "ayer" se archiva en historial/ y el blocker vuelve a "No".
    En sábado, domingo o festivo pide confirmación (forzar=True), porque
    iniciarlo ese día haría que el siguiente día hábil tome como «ayer» un
    día sin trabajo.
    """
    hoy = hoy or date.today()
    if date.fromisoformat(reporte.fecha) >= hoy:
        raise ValueError(f"El reporte ya es del {reporte.fecha}; el nuevo día solo se inicia "
                         "desde un reporte de un día anterior.")
    if not es_habil(hoy) and not forzar:
        tipo = "festivo" if hoy.weekday() < 5 else ("sábado" if hoy.weekday() == 5 else "domingo")
        raise DiaNoHabil(f"Hoy ({hoy.isoformat()}) es {tipo}. Si inicias el día ahora, el próximo día hábil "
                         "tomará este como «ayer». ¿Trabajaste hoy y quieres iniciarlo igual?")
    carpeta = ruta_reporte().parent / "historial"  # junto al reporte
    carpeta.mkdir(exist_ok=True)
    # Nunca se pisa un archivo del historial (p. ej. si se movió la fecha hacia atrás).
    archivo = carpeta / f"reporte_{reporte.fecha}.json"
    n = 2
    while archivo.exists():
        archivo = carpeta / f"reporte_{reporte.fecha}_{n}.json"
        n += 1
    archivo.write_text(json.dumps(reporte.a_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    pendientes = [t for t in reporte.tareas if t.seccion == "hoy"]
    for t in pendientes:
        t.seccion = "ayer"
        t.resultado = {}
    reporte.tareas = pendientes
    # "Ayer" es el último día reportado (así los TK ya creados no se duplican).
    reporte.fecha_ayer = reporte.fecha
    reporte.fecha = hoy.isoformat()
    reporte.blocker = "No"
    reporte.drive_url = ""  # el Doc de Drive es uno por día (los anteriores quedan en drive_urls)
    reporte.ripor_fecha = reporte.ripor_evidencia = ""
    reporte.ripor_horas = None  # las horas forzadas eran para ese día
    reporte.drive_doc = {}
    return archivo


def quitar_tarea(reporte: Reporte, tarea_id: str) -> None:
    """Elimina una tarea de la lista y del contenido conocido del Doc del día."""
    reporte.tareas.remove(reporte.buscar(tarea_id))
    for fecha, lista in reporte.drive_doc.items():
        reporte.drive_doc[fecha] = [x for x in lista if x.get("id") != tarea_id]


def limpiar_tareas(reporte: Reporte) -> tuple[int, Path]:
    """
    Vacía la lista de tareas. Antes guarda una copia en historial/ por si se
    necesita recuperar algo; lo ya publicado (TK, Doc, Chat, RIPOR) no se toca.
    """
    carpeta = ruta_reporte().parent / "historial"  # junto al reporte
    carpeta.mkdir(exist_ok=True)
    archivo = carpeta / f"limpieza_{reporte.fecha}_{datetime.now():%H%M%S}.json"
    archivo.write_text(json.dumps(reporte.a_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    cantidad = len(reporte.tareas)
    reporte.tareas = []
    return cantidad, archivo


def requerida(nombre: str) -> str:
    """Lee una variable de entorno obligatoria o falla con un mensaje claro."""
    valor = os.getenv(nombre, "").strip()
    if not valor:
        raise RuntimeError(f"Falta la variable de entorno {nombre} (revisa el .env).")
    return valor


def zona_horaria() -> timezone:
    texto = os.getenv("ADO_TZ_OFFSET", "-05:00").strip()
    m = re.fullmatch(r"([+-])(\d{2}):(\d{2})", texto)
    if not m:
        raise RuntimeError(f"ADO_TZ_OFFSET inválido {texto!r} (usa por ejemplo -05:00).")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


def sesion_http() -> requests.Session:
    """
    Sesión con reintentos automáticos ante 429 y errores 5xx, SOLO para
    lecturas (GET). Un POST o PATCH no se reintenta: si el servidor ya lo
    procesó y solo tardó en responder, repetirlo duplicaría el TK, el mensaje
    de Chat o el Doc. Los errores de conexión (la petición no salió) sí se
    reintentan con cualquier método.
    """
    reintentos = Retry(total=3, backoff_factor=1.5,
                       status_forcelist=(429, 500, 502, 503, 504),
                       allowed_methods=frozenset({"GET", "HEAD"}))
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=reintentos))
    return s


def sin_secretos(texto: str) -> str:
    """Quita de un mensaje de error lo que va después de "?" en las URL (claves y tokens)."""
    return re.sub(r"((?:https?://|/)[^\s?'\"]*)\?[^\s'\"),=]*=[^\s'\"),]*", r"\1?…", str(texto))


# ---------------------------------------------------------------------------
# 2. Azure DevOps — un TK hijo por cada work item relacionado
# ---------------------------------------------------------------------------

class AzureDevOps:
    """Cliente mínimo de la REST API de Work Items (api-version 7.1)."""

    API = "api-version=7.1"

    def __init__(self, dry_run: bool = False):
        self.org_url = requerida("ADO_ORG_URL").rstrip("/")    # https://dev.azure.com/Linktic
        self.tipo_wi = os.getenv("ADO_WORK_ITEM_TYPE", "TK").strip()
        self.asignado = os.getenv("ADO_ASSIGNED_TO", "").strip()
        # Estado con el que queda el TK si la tarea no dice otro (por defecto, cerrado).
        self.estado = _estado_tk(os.getenv("ADO_TK_STATE", "Closed")) or "Closed"
        self.zona = os.getenv("ADO_TZ_OFFSET", "-05:00").strip()
        self.hora_inicio = _hora(os.getenv("ADO_TK_HORA_INICIO", "07:30"))
        self.hora_fin = _hora(os.getenv("ADO_TK_HORA_FIN", "17:00"))
        # Puntos si el work item no tiene (p. ej. la Feature de una célula).
        self.puntos_defecto = _puntos(os.getenv("ADO_TK_PUNTOS_DEFECTO", "1"))
        self.dry_run = dry_run
        self._wis: dict[int, dict] = {}                     # caché de work items leídos
        self._iteraciones: dict[str, dict[int, str]] = {}   # caché: proyecto → {n: ruta}

        # Autenticación Basic con usuario vacío y el PAT como contraseña.
        pat = requerida("ADO_PAT")
        token = base64.b64encode(f":{pat}".encode()).decode()
        self.http = sesion_http()
        self.http.headers.update({"Authorization": f"Basic {token}",
                                  "Accept": "application/json"})

        # Registro local de TK ya creados: evita duplicarlos si se sube dos veces.
        # Se lee la primera vez que se necesita (buscar work items no lo usa).
        self.ruta_registro = ruta_reporte().parent / "tks_creados.json"
        self._registro: dict | None = None

    @property
    def registro(self) -> dict:
        if self._registro is None:
            self._registro = self._leer_registro()
        return self._registro

    @registro.setter
    def registro(self, valor: dict) -> None:
        self._registro = valor

    # -- helpers --------------------------------------------------------------

    def _check(self, resp: requests.Response, accion: str) -> dict:
        if resp.status_code == 401:
            raise RuntimeError(f"{accion}: 401 — el PAT venció o no tiene el alcance "
                               "'Work Items (Read & write)'.")
        if not resp.ok:
            try:
                motivo = resp.json().get("message") or resp.text
            except ValueError:
                motivo = resp.text
            raise RuntimeError(f"{accion}: HTTP {resp.status_code} — {motivo[:300]}")
        return resp.json()

    def _url_web(self, proyecto: str, wid: int) -> str:
        return f"{self.org_url}/{quote(proyecto)}/_workitems/edit/{wid}"

    @staticmethod
    def _a_html(texto: str) -> str:
        """System.Description es HTML: escapamos y respetamos los saltos de línea."""
        return "<br>".join(html.escape(linea) for linea in texto.splitlines())

    def _fecha_hora(self, fecha: str, hora: str) -> datetime:
        return datetime.fromisoformat(f"{fecha}T{hora}:00{self.zona}")

    def _fechas(self, t: Tarea, fecha: str) -> tuple[datetime, datetime]:
        """Fecha de Inicio y Fecha Fin del TK."""
        inicio = self._fecha_hora(fecha, t.inicio or self.hora_inicio)
        if t.fin:
            fin = self._fecha_hora(fecha, t.fin)
        elif t.horas:
            fin = inicio + timedelta(hours=t.horas)
        else:
            fin = self._fecha_hora(fecha, self.hora_fin)
        if fin <= inicio:  # p. ej. una tarea que empieza después de la hora fin por defecto
            fin = inicio + timedelta(minutes=30)
        return inicio, fin

    # -- consultas ------------------------------------------------------------

    def leer_wi(self, wid: int) -> dict:
        """Trae los campos del work item padre que el TK debe heredar."""
        if wid not in self._wis:
            campos = ("System.Title,System.TeamProject,System.AreaPath,System.IterationPath,"
                      "Microsoft.VSTS.Scheduling.StoryPoints,Custom.PuntosdeHistoria")
            r = self.http.get(f"{self.org_url}/_apis/wit/workitems/{wid}"
                              f"?fields={campos}&{self.API}", timeout=30)
            self._wis[wid] = self._check(r, f"Leer work item {wid}")["fields"]
        return self._wis[wid]

    def info_wis(self, ids: list[int]) -> dict[int, dict]:
        """Título, tipo, proyecto y estado de varios work items (los que no existen no salen)."""
        info: dict[int, dict] = {}
        campos = "System.Id,System.Title,System.WorkItemType,System.TeamProject,System.State"
        for i in range(0, len(ids), 200):  # la API acepta hasta 200 ids por consulta
            r = self.http.get(f"{self.org_url}/_apis/wit/workitems?ids={','.join(map(str, ids[i:i + 200]))}"
                              f"&fields={campos}&errorPolicy=omit&{self.API}", timeout=30)
            for w in self._check(r, "Leer work items")["value"]:
                if w:  # errorPolicy=omit devuelve null para los que no existen
                    f = w["fields"]
                    info[w["id"]] = {"id": w["id"], "titulo": f["System.Title"],
                                     "tipo": f["System.WorkItemType"],
                                     "proyecto": f["System.TeamProject"], "estado": f["System.State"]}
        return info

    def buscar(self, texto: str, limite: int = 15) -> list[dict]:
        """Busca HUs/Features por número de work item o por texto del título ("HU533")."""
        texto = texto.strip()
        if not texto:
            return []
        if texto.isdigit():
            ids = [int(texto)]
        else:
            q = texto.replace("'", "''")
            wiql = {"query": (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.Title] CONTAINS '{q}' "
                "AND [System.WorkItemType] IN ('User Story', 'Feature', 'Epic', 'Requirement') "
                "AND [System.State] <> 'Removed' ORDER BY [System.ChangedDate] DESC")}
            r = self.http.post(f"{self.org_url}/_apis/wit/wiql?$top={limite}&{self.API}",
                               json=wiql, timeout=30)
            ids = [w["id"] for w in self._check(r, "Buscar work items")["workItems"]][:limite]
        if not ids:
            return []
        campos = "System.Id,System.Title,System.WorkItemType,System.TeamProject,System.State"
        r = self.http.get(f"{self.org_url}/_apis/wit/workitems?ids={','.join(map(str, ids))}"
                          f"&fields={campos}&errorPolicy=omit&{self.API}", timeout=30)
        resultados = []
        for w in self._check(r, "Leer work items")["value"]:
            if not w:  # errorPolicy=omit devuelve null para los que no existen
                continue
            f = w["fields"]
            # Los procesos de Linktic usan estados propios ("Removido", "X. Removido").
            if not texto.isdigit() and re.search(r"remov", f["System.State"], re.IGNORECASE):
                continue
            resultados.append({"id": w["id"], "titulo": f["System.Title"],
                               "tipo": f["System.WorkItemType"],
                               "proyecto": f["System.TeamProject"], "estado": f["System.State"]})
        return resultados

    def ruta_sprint(self, proyecto: str, numero: int) -> str | None:
        """Busca la iteración llamada "Sprint <n>" dentro del proyecto."""
        if proyecto not in self._iteraciones:
            r = self.http.get(f"{self.org_url}/{quote(proyecto)}/_apis/wit/"
                              f"classificationnodes/iterations?$depth=6&{self.API}", timeout=30)
            raiz = self._check(r, f"Leer sprints de {proyecto}")
            encontrados: dict[int, str] = {}

            def recorrer(nodo: dict, ruta: str) -> None:
                m = re.fullmatch(r"Sprint (\d+)", nodo["name"].strip(), re.IGNORECASE)
                if m:
                    encontrados.setdefault(int(m.group(1)), ruta)
                for hijo in nodo.get("children", []):
                    recorrer(hijo, f"{ruta}\\{hijo['name']}")

            # La ruta de iteración empieza por el nombre del proyecto (no por "Iteration").
            for hijo in raiz.get("children", []):
                recorrer(hijo, f"{proyecto}\\{hijo['name']}")
            self._iteraciones[proyecto] = encontrados
        return self._iteraciones[proyecto].get(numero)

    def puntos_de(self, puntos: int | None, padre: dict, avisos: list[str]) -> int | None:
        """Puntos de la tarea, o los del work item (su nivel de dificultad), o los por defecto."""
        if puntos:
            return puntos
        valor = (padre.get("Microsoft.VSTS.Scheduling.StoryPoints")
                 or padre.get("Custom.PuntosdeHistoria"))
        if valor not in (None, ""):
            puntos = int(float(valor))
            if puntos in PUNTOS_VALIDOS:
                return puntos
            avisos.append(f"el work item tiene {valor} puntos, que el TK no acepta; "
                          f"se usó {self.puntos_defecto}")
        return self.puntos_defecto

    # -- creación ---------------------------------------------------------------

    def _leer_registro(self) -> dict:
        if not self.ruta_registro.exists():
            return {}
        try:
            return json.loads(self.ruta_registro.read_text(encoding="utf-8-sig"))
        except ValueError:
            raise RuntimeError(f"{self.ruta_registro.name} está dañado; revísalo antes de subir "
                               "(sin él se podrían duplicar TK).") from None

    def _guardar_registro(self) -> list[str]:
        """Mezcla con lo que haya en disco y escribe de forma atómica. Devuelve avisos."""
        try:
            self.registro = {**self._leer_registro(), **self.registro}
            temporal = self.ruta_registro.with_suffix(".tmp")
            temporal.write_text(json.dumps(self.registro, indent=2), encoding="utf-8")
            os.replace(temporal, self.ruta_registro)
            return []
        except (OSError, RuntimeError) as e:
            return [f"no se pudo guardar {self.ruta_registro.name} ({e}); el TK sí quedó en la tarea"]

    def hijo_con_titulo(self, wi: int, titulo: str) -> int | None:
        """Busca entre los hijos del work item uno con ese título exacto (para no duplicar)."""
        t = titulo.replace("'", "''")
        wiql = {"query": (
            "SELECT [System.Id] FROM WorkItemLinks "
            f"WHERE [Source].[System.Id] = {int(wi)} "
            "AND [System.Links.LinkType] = 'System.LinkTypes.Hierarchy-Forward' "
            f"AND [Target].[System.Title] = '{t}' "
            "AND [Target].[System.CreatedDate] >= @Today - 2 MODE (MustContain)")}
        r = self.http.post(f"{self.org_url}/_apis/wit/wiql?{self.API}", json=wiql, timeout=30)
        # Los TK ya registrados son de otras tareas (p. ej. la misma reunión de otro día).
        conocidos = {v for v in self.registro.values() if isinstance(v, int)}
        conocidos |= {v["tk"] for v in self.registro.values() if isinstance(v, dict) and "tk" in v}
        candidatos = [rel["target"]["id"] for rel in self._check(r, "Buscar TK existente").get("workItemRelations", [])
                      if rel.get("rel") and rel.get("target") and rel["target"]["id"] not in conocidos]
        return max(candidatos) if candidatos else None  # el más reciente

    def _patch(self, tk_id: int, operaciones: list[dict], accion: str) -> dict:
        r = self.http.patch(f"{self.org_url}/_apis/wit/workitems/{tk_id}?{self.API}",
                            data=json.dumps(operaciones),
                            headers={"Content-Type": "application/json-patch+json"}, timeout=30)
        return self._check(r, accion)

    def _cambiar_estado(self, tk_id: int, actual: str, destino: str) -> list[str]:
        """Lleva el TK al estado pedido; si el proceso no permite el salto directo
        (p. ej. New → Closed), pasa antes por Active. Devuelve avisos."""
        if not destino or actual == destino:
            return []
        cambio = lambda estado: [{"op": "add", "path": "/fields/System.State", "value": estado}]  # noqa: E731
        try:
            self._patch(tk_id, cambio(destino), "Cambiar estado")
            return []
        except RuntimeError as e:
            if destino == "Active" or actual == "Active":
                return [f"{e} (el TK sí quedó guardado, en {actual})"]
        try:
            self._patch(tk_id, cambio("Active"), "Cambiar estado")
            self._patch(tk_id, cambio(destino), "Cambiar estado")
            return []
        except RuntimeError as e:
            return [f"{e} (el TK sí quedó guardado)"]

    def tk_existente(self, t: Tarea, wi: int, fecha: str) -> int | None:
        """TK ya creado para esta tarea en ese work item y fecha (también con registros anteriores)."""
        # La tarea ya tiene TK en ese work item: no se crea otro aunque cambie su sección o fecha.
        for x in t.tks:
            if x.get("wi") == wi and x.get("tk"):
                return x["tk"]
        tk_id = self.registro.get(f"{fecha}|{wi}|{t.id}")
        if isinstance(tk_id, int):
            return tk_id
        # Registro de la versión que juntaba tareas: {"tk": n, "tareas": [ids]} por fecha|wi.
        dato = self.registro.get(f"{fecha}|{wi}")
        if isinstance(dato, dict) and t.id in dato.get("tareas", []):
            return dato["tk"]
        # Registro de la primera versión: clave fecha|wi|nombre corto.
        antiguo = self.registro.get(f"{fecha}|{wi}|{t.nombre_tk}")
        return antiguo if isinstance(antiguo, int) else None

    def estado_de(self, tk_id: int) -> str:
        r = self.http.get(f"{self.org_url}/_apis/wit/workitems/{tk_id}?fields=System.State&{self.API}",
                          timeout=30)
        return self._check(r, f"Leer TK {tk_id}")["fields"].get("System.State", "")

    def crear_tk(self, t: Tarea, wi: int, fecha: str, reporte: Reporte) -> dict:
        """
        Crea el TK de una tarea en un work item (un TK por actividad, aunque compartan
        work item). Si la tarea ya tiene TK y se editó después de subirla, lo actualiza.
        """
        padre = self.leer_wi(wi)
        proyecto = padre["System.TeamProject"]
        clave = f"{fecha}|{wi}|{t.id}"
        titulo = f"{padre['System.Title']}_{t.nombre_tk}"[:255]

        # Datos del TK siguiendo la convención del equipo (ver TK 559578).
        avisos: list[str] = []
        inicio, fin = self._fechas(t, fecha)
        puntos = self.puntos_de(t.puntos, padre, avisos)
        tipo = t.tipo or reporte.tipo or _tipo_tk(os.getenv("ADO_TK_TIPO", ""))
        estado = t.estado_tk or self.estado
        campos = {
            "System.Title": titulo,
            "System.Description": self._a_html(t.descripcion),
            "Custom.FechadeInicio": inicio.isoformat(),
            "Custom.FechaFin": fin.isoformat(),
            "Custom.Tipo_TK": tipo or None,
            "Custom.PuntosdeHistoria": str(puntos) if puntos else None,
        }
        # Huella de lo que se envió: si la tarea cambia, cambia la huella y el TK se actualiza.
        huella = hashlib.sha1(json.dumps({**campos, "estado": estado}, sort_keys=True)
                              .encode("utf-8")).hexdigest()[:16]

        def add(campo: str, valor) -> dict:
            return {"op": "add", "path": f"/fields/{campo}", "value": valor}

        def resultado(tk_id: int, estado_r: str, detalle: str) -> dict:
            nota = f" Aviso: {'; '.join(avisos)}." if avisos else ""
            return {"wi": wi, "tk": tk_id, "url": self._url_web(proyecto, tk_id) if tk_id else "",
                    "estado": estado_r, "huella": huella, "detalle": f"{detalle}{nota}"}

        tk_id = self.tk_existente(t, wi, fecha)
        if not tk_id and self.registro.get(clave) == "incierto":
            # La vez pasada Azure no respondió: se busca si el TK sí quedó creado.
            tk_id = self.hijo_con_titulo(wi, titulo)
            if tk_id:
                if self.dry_run:
                    return resultado(tk_id, "simulado", f"TK {tk_id} ya estaba creado en WI {wi}; se registraría.")
                self.registro[clave] = tk_id
                avisos += self._guardar_registro()
                avisos += self._cambiar_estado(tk_id, self.estado_de(tk_id), estado)
                return resultado(tk_id, "existente", f"TK {tk_id} sí se había creado en WI {wi}; quedó registrado.")
        if tk_id:
            previa = next((x.get("huella") for x in t.tks if x.get("tk") == tk_id), None)
            if previa is None or previa == huella:
                # Sin cambios (o TK de una versión anterior, sin huella: no se toca).
                return resultado(tk_id, "existente", f"TK {tk_id} ya existía en WI {wi} (no se duplica).")
            if self.dry_run:
                return resultado(tk_id, "simulado", f"Actualizaría el TK {tk_id} en WI {wi} con los cambios de la tarea.")
            self._patch(tk_id, [add(k, v) for k, v in campos.items() if v], f"Actualizar TK {tk_id}")
            avisos += self._cambiar_estado(tk_id, self.estado_de(tk_id), estado)
            return resultado(tk_id, "actualizado", f"TK {tk_id} actualizado en WI {wi} con los cambios de la tarea.")

        iteracion = padre["System.IterationPath"]
        if reporte.sprint:
            ruta = self.ruta_sprint(proyecto, reporte.sprint)
            if ruta:
                iteracion = ruta
            else:
                avisos.append(f"no existe 'Sprint {reporte.sprint}' en {proyecto}; "
                              "quedó en la iteración del work item")

        patch = [add(k, v) for k, v in campos.items() if v] + [
            add("System.AreaPath", padre["System.AreaPath"]),
            add("System.IterationPath", iteracion),
            # Enlace jerárquico: el TK queda como hijo del work item.
            {"op": "add", "path": "/relations/-", "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"{self.org_url}/_apis/wit/workItems/{wi}",
            }},
        ]
        if reporte.sprint:
            patch.append(add("System.Tags", f"S{reporte.sprint}"))
        if self.asignado:
            patch.append(add("System.AssignedTo", self.asignado))
            patch.append(add("Custom.Asignado", self.asignado))

        resumen = (f"{iteracion.rsplit(chr(92), 1)[-1]}"
                   f"{' · S' + str(reporte.sprint) if reporte.sprint else ''} · {tipo or 'sin tipo'} · "
                   f"{puntos or 'sin'} pts · {inicio:%Y-%m-%d %H:%M}–{fin:%H:%M} · {estado}")

        if self.dry_run:
            return resultado(None, "simulado", f"Crearía TK «{titulo}» ({resumen}).")

        url = (f"{self.org_url}/{quote(proyecto)}/_apis/wit/workitems/"
               f"${quote(self.tipo_wi)}?{self.API}")
        try:
            r = self.http.post(url, data=json.dumps(patch),
                               headers={"Content-Type": "application/json-patch+json"}, timeout=60)
        except (requests.ReadTimeout, requests.ConnectionError) as e:
            # Si la petición no llegó a salir (sin red, DNS, SSL), no hay nada que comprobar.
            if isinstance(e, requests.ConnectTimeout) or re.search(
                    r"NewConnectionError|NameResolution|getaddrinfo|SSLError|Connection refused", repr(e)):
                raise
            # Salió pero no hubo respuesta: el TK pudo quedar creado.
            self.registro[clave] = "incierto"
            self._guardar_registro()
            raise RuntimeError("Azure no respondió a tiempo y no se sabe si creó el TK. Al volver a "
                               "subir, primero se comprueba si ya existe.") from None
        tk = self._check(r, f"Crear {self.tipo_wi} en WI {wi}")
        tk_id = tk["id"]

        # Se registra ANTES de cambiar el estado: si eso falla, no se vuelve a crear.
        self.registro[clave] = tk_id
        avisos += self._guardar_registro()
        avisos += self._cambiar_estado(tk_id, tk["fields"].get("System.State", ""), estado)
        return resultado(tk_id, "creado", f"TK {tk_id} creado en WI {wi} ({resumen}).")

    def publicar_tarea(self, t: Tarea, reporte: Reporte) -> dict:
        """Crea (o actualiza, si la tarea cambió) los TK de una tarea: uno en cada work item."""
        if not t.work_items:
            return {"estado": "omitido", "detalle": "Sin work item relacionado: no crea TK."}
        if not t.crear_tk:
            return {"estado": "omitido", "detalle": "Marcada para no crear TK."}

        hechos, errores = [], []
        for wi in t.work_items:
            try:
                hechos.append(self.crear_tk(t, wi, reporte.fecha_de(t), reporte))
            except Exception as e:  # un work item fallido no detiene los demás
                errores.append(f"WI {wi}: {sin_secretos(e)}")

        # Los TK reales quedan guardados en la tarea con la huella de lo enviado
        # (se muestran en Drive y en la página, y permiten detectar cambios).
        if not self.dry_run:
            for x in hechos:
                if not x["tk"]:
                    continue
                previo = next((y for y in t.tks if y["wi"] == x["wi"] and y["tk"] == x["tk"]), None)
                if previo:
                    previo["huella"] = x["huella"]
                else:
                    t.tks.append({"wi": x["wi"], "tk": x["tk"], "url": x["url"], "huella": x["huella"]})

        estado = "error" if errores else ("simulado" if self.dry_run else "ok")
        return {"estado": estado, "detalle": " ".join([x["detalle"] for x in hechos] + errores),
                "tks": hechos}


# ---------------------------------------------------------------------------
# 3. Google Apps Script (Drive y Calendar) — autenticado con el token de gcloud
# ---------------------------------------------------------------------------

SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
# En Windows, que los procesos de fondo (gcloud, node) no abran una consola.
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ruta_gcloud() -> str:
    """gcloud del .env, del PATH o de la ruta donde lo deja el instalador (Windows o Mac)."""
    candidatas = [os.getenv("GCLOUD_PATH", "").strip(), shutil.which("gcloud") or "",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"),
                  str(Path.home() / "google-cloud-sdk" / "bin" / "gcloud"),
                  "/opt/homebrew/bin/gcloud", "/usr/local/bin/gcloud"]
    for ruta in candidatas:
        if ruta and Path(ruta).exists():
            return ruta
    raise RuntimeError("No se encontró gcloud. Instala Google Cloud CLI o define GCLOUD_PATH en el .env.")


def token_gcloud() -> str:
    """
    Pide a Google Cloud CLI un token de acceso de tu cuenta. gcloud lo renueva
    solo; requiere haber iniciado sesión con:
        gcloud auth login --enable-gdrive-access
    (sin el permiso de Drive, Apps Script responde 401).
    """
    r = subprocess.run([ruta_gcloud(), "auth", "print-access-token"], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60, creationflags=SIN_VENTANA)
    if r.returncode != 0 or not r.stdout.strip():
        if "reauth" in r.stderr.lower():
            # La organización exige volver a iniciar sesión cada cierto tiempo (~16 h).
            raise RuntimeError("Tu sesión de Google venció (Linktic pide iniciar sesión de nuevo cada "
                               "día). Usa el botón «Iniciar sesión en Google» de la página o ejecuta: "
                               "gcloud auth login --enable-gdrive-access")
        raise RuntimeError("gcloud no entregó un token; inicia sesión con el botón de la página o con "
                           f"'gcloud auth login --enable-gdrive-access'. Detalle: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def estado_google() -> dict:
    """¿Hay una sesión de Google con permiso de Drive? Solo consulta, no abre nada."""
    if not os.getenv("GDRIVE_APPS_SCRIPT_URL"):
        return {"configurado": False}
    try:
        token = token_gcloud()
        r = sesion_http().post("https://oauth2.googleapis.com/tokeninfo",
                               data={"access_token": token}, timeout=20)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, requests.RequestException) as e:
        return {"configurado": True, "ok": False, "detalle": sin_secretos(e)}
    if not r.ok:
        return {"configurado": True, "ok": False, "detalle": "Google no aceptó la sesión guardada."}
    info = r.json()
    if SCOPE_DRIVE not in info.get("scope", "").split():
        return {"configurado": True, "ok": False, "cuenta": info.get("email", ""),
                "detalle": "La sesión no tiene permiso de Drive: vuelve a iniciar sesión."}
    return {"configurado": True, "ok": True, "cuenta": info.get("email", "")}


_inicios: dict[str, tuple[subprocess.Popen, float]] = {}  # inicios de sesión en curso y cuándo empezaron


def lanzar_inicio_sesion(servicio: str) -> str:
    """
    Abre el inicio de sesión de Google (gcloud) o de RIPOR (node) en el
    navegador, sin esperar a que termine: la página consulta el estado
    hasta que la sesión quede lista.
    """
    previo, desde = _inicios.get(servicio, (None, 0.0))
    if previo and previo.poll() is None:
        if time.monotonic() - desde < 20:  # doble clic: no se abren dos ventanas
            return "El inicio de sesión ya está abierto en el navegador; complétalo allí."
        # Quedó esperando (p. ej. se cerró la pestaña): se termina y se abre uno nuevo. En
        # Windows gcloud es un .cmd: kill() solo cerraría cmd.exe y no el proceso que escucha.
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(previo.pid)], capture_output=True,
                           creationflags=SIN_VENTANA)
        else:
            previo.kill()
    if servicio == "google":
        comando = [ruta_gcloud(), "auth", "login", "--enable-gdrive-access"]
    elif servicio == "ripor":
        ripor = RiporMCP()
        if not ripor.ruta.exists():
            raise RuntimeError(f"No se encontró el conector de RIPOR en {ripor.ruta}.")
        ripor.verificar()
        comando = [ripor.node, str(ripor.ruta), "login"]
    else:
        raise ValueError(f"Servicio desconocido {servicio!r}.")
    try:
        proceso = subprocess.Popen(comando, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=SIN_VENTANA)
        _inicios[servicio] = (proceso, time.monotonic())
    except OSError as e:
        raise RuntimeError(f"No se pudo abrir el inicio de sesión: {e}") from None
    return "Se abrió el navegador: inicia sesión con tu cuenta de Linktic."


def llamar_apps_script(payload: dict) -> dict:
    """
    Envía una petición a la Aplicación web de Apps Script (apps_script/Code.gs).
    La app web está restringida a Linktic, así que va autenticada con el token
    de tu cuenta que entrega gcloud.
    """
    url_script = requerida("GDRIVE_APPS_SCRIPT_URL")
    secreto = requerida("GDRIVE_APPS_SCRIPT_SECRET")
    # Apps Script responde con un redirect 302 a script.googleusercontent.com;
    # requests lo sigue solo y la respuesta final es el JSON del script.
    resp = sesion_http().post(
        url_script, timeout=90,
        headers={"Authorization": f"Bearer {token_gcloud()}"},
        json={"secreto": secreto, **payload})
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Apps Script respondió HTTP {resp.status_code}: el token no tiene "
                           "permiso de Drive o la cuenta de gcloud no es la dueña del script. "
                           "Ejecuta 'gcloud auth login --enable-gdrive-access'.")
    if not resp.ok:
        raise RuntimeError(f"Apps Script respondió HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        resultado = resp.json()
    except ValueError:
        # Suele ser la página de inicio de sesión de Google o un error de Apps Script
        # (cuota, tiempo máximo de ejecución): se muestra un extracto del texto.
        texto = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", resp.text)).strip()[:200]
        raise RuntimeError("Apps Script no devolvió JSON (revisa que la URL termine en /exec y sea una "
                           f"'Aplicación web'). Respondió: {texto or 'vacío'}") from None
    if not resultado.get("ok"):
        raise RuntimeError(f"Apps Script: {resultado.get('error')}")
    return resultado


def reporte_a_html(r: Reporte, tareas: list[Tarea]) -> str:
    """HTML sencillo que Drive convierte a Google Doc."""
    e = html.escape

    def item(t: Tarea) -> str:
        enlaces = ", ".join(f'<a href="{e(x["url"])}">TK {e(str(x["tk"]))}</a> (WI {e(str(x["wi"]))})'
                            for x in t.tks)
        return f"<li>{AzureDevOps._a_html(t.descripcion)}{' — ' + enlaces if enlaces else ''}</li>"

    partes = [f"<h1>Reporte diario — {e(r.fecha)}</h1>"]
    if r.autor:
        partes.append(f"<p><b>Autor:</b> {e(r.autor)}"
                      f"{f' · <b>Sprint:</b> {r.sprint}' if r.sprint else ''}</p>")
    partes.append(f"<p>{e(r.saludo.strip().rstrip(','))},</p>")
    for clave, pregunta in SECCIONES.items():
        items = [item(t) for t in tareas if t.seccion == clave]
        partes.append(f"<h3>{e(pregunta)}</h3>")
        partes.append(f"<ul>{''.join(items)}</ul>" if items else "<p>—</p>")
    partes.append(f"<h3>{e(PREGUNTA_BLOCKER)}</h3><p>{e(r.blocker or 'No')}</p>")
    return f"<html><body>{''.join(partes)}</body></html>"


def publicar_drive(reporte: Reporte, tareas: list[Tarea], dry_run: bool = False) -> dict:
    """Crea o actualiza el Google Doc del día a través de Apps Script."""
    nombre = f"{os.getenv('GDRIVE_FILE_PREFIX', 'Reporte diario')} - {reporte.fecha}"
    contenido = reporte_a_html(reporte, tareas)

    # Copia local, útil como respaldo o para revisar el formato.
    salida = BASE_DIR / "salidas"
    salida.mkdir(exist_ok=True)
    (salida / f"{nombre}.html").write_text(contenido, encoding="utf-8")

    if dry_run:
        return {"accion": "simulado", "url": None}
    return llamar_apps_script({"nombre": nombre, "html": contenido})


def importar_calendar(reporte: Reporte) -> dict:
    """
    Trae las reuniones de Google Calendar de "ayer" y de "hoy" y las agrega
    como tareas ("Participación en la sesión de <reunión>."). Omite las de todo
    el día, las rechazadas y las que ya estaban importadas.
    """
    tz = zona_horaria()
    # Como mucho 9 días atrás (el Apps Script no acepta rangos mayores a 10 días).
    desde = max(datetime.fromisoformat(reporte.fecha_ayer),
                datetime.fromisoformat(reporte.fecha) - timedelta(days=9)).replace(tzinfo=tz)
    hasta = datetime.fromisoformat(reporte.fecha).replace(tzinfo=tz) + timedelta(days=1)
    try:
        datos = llamar_apps_script({"accion": "eventos", "desde": desde.isoformat(),
                                    "hasta": hasta.isoformat()})
    except RuntimeError as e:
        if '"nombre" y "html"' in str(e):  # el Code.gs publicado todavía no conoce "eventos"
            raise RuntimeError("El Apps Script publicado aún no tiene la función de Calendar: "
                               "actualiza Code.gs y publica una nueva versión (README, paso 4).") from None
        raise

    def evento_y_dia(origen: str) -> str:
        """«calendar:<id>|<inicio ISO>» → «<id>|<AAAA-MM-DD>»: la misma reunión ese día."""
        ident, _, cuando = origen.removeprefix("calendar:").partition("|")
        return f"{ident}|{cuando[:10]}"

    existentes = {evento_y_dia(t.origen): t for t in reporte.tareas if t.origen.startswith("calendar:")}
    dias = {reporte.fecha_ayer: "ayer", reporte.fecha: "hoy"}
    agregadas = repetidas = movidas = fuera = 0
    for ev in datos.get("eventos", []):
        inicio = datetime.fromisoformat(ev["inicio"]).astimezone(tz)
        fin = datetime.fromisoformat(ev["fin"]).astimezone(tz)
        seccion = dias.get(inicio.date().isoformat())
        if not seccion:  # p. ej. un día intermedio si no se abrió la herramienta
            fuera += 1
            continue
        origen = f"calendar:{ev['id']}|{inicio.isoformat()}"
        hora_inicio = inicio.strftime("%H:%M")
        hora_fin = fin.strftime("%H:%M") if fin.date() == inicio.date() else "23:59"
        ya = existentes.get(evento_y_dia(origen))
        if ya:
            if ya.origen != origen:  # la movieron de hora: se actualiza la tarea, no se duplica
                ya.origen, ya.inicio = origen, hora_inicio
                ya.fin = hora_fin if hora_fin > hora_inicio else ""
                movidas += 1
            else:
                repetidas += 1
            continue
        titulo = (ev.get("titulo") or "").strip()[:200] or "(reunión sin título)"
        previo = reporte.recordar.get(titulo, {})
        reporte.tareas.append(Tarea(
            seccion=seccion,
            descripcion=f"Participación en la sesión de {titulo}.",
            categoria=previo.get("categoria") or "Reuniones",
            work_items=list(previo.get("work_items") or []),
            wi_titulos=dict(previo.get("wi_titulos") or {}),
            tarea=titulo,
            tipo=previo.get("tipo") or "Gestión",
            inicio=hora_inicio,
            fin=hora_fin if hora_fin > hora_inicio else "",
            origen=origen,
            evento=titulo,
        ))
        existentes[evento_y_dia(origen)] = reporte.tareas[-1]
        agregadas += 1
    return {"agregadas": agregadas, "repetidas": repetidas, "movidas": movidas, "fuera": fuera}


# ---------------------------------------------------------------------------
# 4. Google Chat — mensaje vía webhook entrante
# ---------------------------------------------------------------------------

def mensaje_chat(r: Reporte, tareas: list[Tarea], negrita: bool = True) -> str:
    """El daily con el formato del equipo; en Chat las preguntas van en *negrita*."""
    n = (lambda texto: f"*{texto}*") if negrita else (lambda texto: texto)  # noqa: E731
    # En Google Chat «<users/all>» menciona a todo el espacio y «<url|texto>» esconde un
    # enlace: el texto (que puede venir de un título de reunión puesto por otra persona)
    # se envía sin «<».
    limpio = lambda texto: str(texto).replace("<", "‹")  # noqa: E731
    lineas = [f"{limpio(r.saludo.strip().rstrip(','))},"]
    for clave, pregunta in SECCIONES.items():
        lineas.append(n(pregunta))
        lineas += [limpio(t.descripcion) for t in tareas if t.seccion == clave] or ["—"]
    lineas.append(f"{n(PREGUNTA_BLOCKER)} {limpio(r.blocker.strip() or 'No')}")
    return "\n".join(lineas)


def publicar_chat(reporte: Reporte, tareas: list[Tarea], dry_run: bool = False) -> str:
    webhook = requerida("GCHAT_WEBHOOK_URL")
    texto = mensaje_chat(reporte, tareas)
    if dry_run:
        return texto
    resp = sesion_http().post(webhook, json={"text": texto},
                              headers={"Content-Type": "application/json; charset=UTF-8"},
                              timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Google Chat respondió HTTP {resp.status_code}: {resp.text[:300]}")
    return texto


# ---------------------------------------------------------------------------
# 5. RIPOR — registro de horas con el conector oficial (ripor-mcp.mjs)
# ---------------------------------------------------------------------------

class RiporMCP:
    """
    Cliente mínimo del conector de RIPOR, que es un servidor MCP por stdio
    (JSON-RPC, un mensaje por línea). Se usa con `with`: arranca node, hace el
    saludo del protocolo y al salir lo cierra. Las reglas de RIPOR (horas por
    día, 50 caracteres, evidencia, transporte, días bloqueados…) las valida el
    propio conector; la sesión es la de `node ripor-mcp.mjs login`.
    """

    def __init__(self):
        self.ruta = Path(os.getenv("RIPOR_MCP_PATH", "").strip() or Path.home() / "ripor-mcp.mjs")
        candidatas = ([r"C:\Program Files\nodejs\node.exe"] if os.name == "nt"
                      else ["/opt/homebrew/bin/node", "/usr/local/bin/node"])
        self.node = (os.getenv("RIPOR_NODE_PATH", "").strip() or shutil.which("node")
                     or next((c for c in candidatas if Path(c).exists()), candidatas[0]))
        self._id = 0

    @staticmethod
    def configurado() -> bool:
        return RiporMCP().ruta.exists()

    def verificar(self) -> None:
        """Si el .env trae RIPOR_MCP_SHA256, el conector debe coincidir antes de ejecutarlo."""
        esperado = os.getenv("RIPOR_MCP_SHA256", "").strip().lower()
        if esperado and hashlib.sha256(self.ruta.read_bytes()).hexdigest() != esperado:
            raise RuntimeError(f"{self.ruta.name} no coincide con RIPOR_MCP_SHA256 del .env: no se ejecuta. "
                               "Si actualizaste el conector a propósito, actualiza también ese valor.")

    def __enter__(self) -> "RiporMCP":
        if not self.ruta.exists():
            raise RuntimeError(f"No se encontró el conector de RIPOR en {self.ruta}. "
                               "Descárgalo (README, paso 6) o define RIPOR_MCP_PATH en el .env.")
        self.verificar()
        self._err = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen([self.node, str(self.ruta), "serve"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=self._err,
                                         creationflags=SIN_VENTANA)
        except OSError as e:
            self._err.close()
            raise RuntimeError(f"No se pudo ejecutar Node.js ({self.node}): {e}. "
                               "RIPOR necesita Node 22 o superior.") from None
        self._lineas: queue.Queue = queue.Queue()
        threading.Thread(target=self._leer, daemon=True).start()
        try:
            self._pedir("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "reporte-diario", "version": "1.0"}})
            self._enviar({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — si no cierra solo, se termina
            self.proc.kill()
        self._err.close()

    def _leer(self) -> None:
        for linea in self.proc.stdout:
            self._lineas.put(linea)
        self._lineas.put(None)

    def _enviar(self, mensaje: dict) -> None:
        try:
            self.proc.stdin.write((json.dumps(mensaje) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except OSError:
            raise RuntimeError(f"El conector de RIPOR se cerró. {self._stderr()}") from None

    def _stderr(self) -> str:
        self._err.seek(0)
        return self._err.read().decode("utf-8", "replace").strip()[-500:]

    def _pedir(self, metodo: str, params: dict, espera: float = 90) -> dict:
        self._id += 1
        ident = self._id
        self._enviar({"jsonrpc": "2.0", "id": ident, "method": metodo, "params": params})
        limite = time.monotonic() + espera
        while True:
            restante = limite - time.monotonic()
            try:
                if restante <= 0:
                    raise queue.Empty
                linea = self._lineas.get(timeout=restante)
            except queue.Empty:
                raise RuntimeError(f"RIPOR no respondió en {espera:.0f} s.") from None
            if linea is None:
                raise RuntimeError(f"El conector de RIPOR se cerró. {self._stderr()}")
            try:
                mensaje = json.loads(linea)
            except ValueError:
                continue
            if mensaje.get("id") != ident or "method" in mensaje:
                continue  # notificaciones u otras respuestas
            if "error" in mensaje:
                error = mensaje["error"]
                raise RuntimeError(f"RIPOR: {error.get('message') if isinstance(error, dict) else error}")
            if not isinstance(mensaje.get("result"), dict):
                raise RuntimeError("RIPOR respondió algo inesperado.")
            return mensaje["result"]

    def llamar(self, herramienta: str, argumentos: dict | None = None, espera: float = 90) -> dict:
        """Ejecuta una herramienta del conector y devuelve su JSON."""
        r = self._pedir("tools/call", {"name": herramienta, "arguments": argumentos or {}}, espera)
        texto = "".join(c.get("text", "") for c in r.get("content") or []
                        if isinstance(c, dict) and c.get("type") == "text")
        if r.get("isError"):
            raise RuntimeError(f"RIPOR: {texto}")
        try:
            datos = json.loads(texto)
        except ValueError:
            raise RuntimeError(f"RIPOR respondió algo inesperado: {texto[:200]}") from None
        if not isinstance(datos, dict):
            raise RuntimeError("RIPOR respondió algo inesperado.")
        return datos


def consultar_ripor(herramienta: str, argumentos: dict | None = None) -> dict:
    """Una consulta suelta (asignaciones, tareas…) para la interfaz web."""
    with RiporMCP() as ripor:
        return ripor.llamar(herramienta, argumentos)


def estado_ripor() -> dict:
    """¿El conector de RIPOR puede ejecutarse y tiene sesión? Solo consulta."""
    ripor = RiporMCP()
    if not ripor.ruta.exists():
        return {"configurado": False}
    try:
        ripor.verificar()
    except RuntimeError as e:
        return {"configurado": True, "ok": False, "bloqueado": True, "detalle": str(e)}
    try:
        r = subprocess.run([ripor.node, str(ripor.ruta), "estado"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=60, creationflags=SIN_VENTANA)
    except FileNotFoundError:
        return {"configurado": True, "ok": False, "bloqueado": True,
                "detalle": "No se encontró Node.js: instálalo (versión 22 o superior) o define RIPOR_NODE_PATH."}
    except OSError as e:
        # Típico en equipos de Linktic: la política de la empresa bloquea node.exe.
        return {"configurado": True, "ok": False, "bloqueado": True,
                "detalle": f"No se pudo ejecutar Node.js ({e}). Pide a TI que lo habilite."}
    except subprocess.TimeoutExpired:
        return {"configurado": True, "ok": False, "detalle": "El conector de RIPOR no respondió."}
    salida = f"{r.stderr}\n{r.stdout}"
    m = re.search(r"te reconoce como: (.+?) <", salida)
    if m:
        return {"configurado": True, "ok": True, "cuenta": m.group(1).strip()}
    if re.search(r"Sesión: no hay|SIN_SESION|SESION_VENCIDA|venció", salida):
        return {"configurado": True, "ok": False, "detalle": "No hay sesión de RIPOR en este equipo."}
    return {"configurado": True, "ok": False, "detalle": salida.strip()[-300:] or "Estado desconocido."}


def _incumplimientos(r: dict) -> str:
    reglas = r.get("incumplimientos") or []
    texto = "; ".join(f"[{x.get('regla')}] {x.get('mensaje')}" for x in reglas)
    return texto or r.get("rechazado_por_ripor") or "RIPOR no aceptó el registro."


def doc_del_dia(reporte: Reporte, fecha: str) -> str:
    """Enlace del Doc de Drive de esa fecha: el guardado o el del reporte archivado en historial/."""
    candidatos = [reporte.drive_urls.get(fecha), reporte.drive_url if fecha == reporte.fecha else ""]
    archivo = ruta_reporte().parent / "historial" / f"reporte_{fecha}.json"
    try:
        candidatos.append(json.loads(archivo.read_text(encoding="utf-8-sig")).get("drive_url"))
    except (OSError, ValueError, AttributeError):
        pass
    return next((u for u in candidatos if es_doc_drive(u)), "")


def destino_ripor(ripor: "RiporMCP", reporte: Reporte, fecha: str) -> tuple[int, int, str]:
    """
    Proyecto y tarea de RIPOR: los elegidos en la página o, si no hay, se eligen
    solos (el único proyecto vigente en la fecha y su «Tarea personalizada»).
    """
    proyecto, tarea, nombre = reporte.ripor_proyecto, reporte.ripor_tarea, reporte.ripor_nombre
    if not proyecto:
        asignaciones = ripor.llamar("mis_asignaciones", {"fecha": fecha})["asignaciones"]
        vigentes = [a for a in asignaciones if a.get("cubre_la_fecha")]
        if len(vigentes) != 1:
            raise RuntimeError(f"Tienes {len(vigentes)} proyectos vigentes en RIPOR el {fecha}: "
                               "elige uno en «Datos del reporte → RIPOR».")
        proyecto, tarea = vigentes[0]["project_id"], None
        nombre = f"{vigentes[0]['ceco']} · {vigentes[0]['proyecto']}"
    if not tarea:
        tareas = ripor.llamar("tareas_del_proyecto", {"project_id": proyecto})["tareas"]
        personalizadas = [x for x in tareas if re.search(r"personalizad", x["nombre"], re.IGNORECASE)]
        candidatas = personalizadas or tareas
        if len(candidatas) != 1:
            raise RuntimeError("No se encontró una única «Tarea personalizada» en el proyecto de RIPOR: "
                               "elígela en «Datos del reporte → RIPOR».")
        tarea = candidatas[0]["task_id"]
        nombre = f"{nombre.split(' › ')[0]} › {candidatas[0]['nombre']}"
    # Lo elegido automáticamente NO se guarda: si el mes siguiente cambia el proyecto
    # vigente, se vuelve a elegir solo.
    return proyecto, tarea, nombre


def daily_de(reporte: Reporte, fecha: str, tareas: list[Tarea]) -> str:
    """
    Texto del daily de esa fecha para RIPOR: el del reporte completo si es el
    día del reporte; el del reporte archivado en historial/ si es otro día; si
    no hay archivo, el de las tareas seleccionadas.
    """
    if fecha != reporte.fecha:
        archivo = ruta_reporte().parent / "historial" / f"reporte_{fecha}.json"
        try:
            anterior = reporte_desde_dict(json.loads(archivo.read_text(encoding="utf-8-sig")))
            return mensaje_chat(anterior, anterior.tareas, negrita=False)
        except (OSError, ValueError, AttributeError):
            return mensaje_chat(reporte, tareas, negrita=False)
    return mensaje_chat(reporte, reporte.tareas, negrita=False)


def _normalizar(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip().lower()


def publicar_ripor(reporte: Reporte, tareas: list[Tarea], dry_run: bool = False) -> dict:
    """Registra el día en RIPOR; nunca lanza excepciones (los fallos vuelven como estado "error")."""
    try:
        return _publicar_ripor(reporte, tareas, dry_run)
    except Exception as e:  # noqa: BLE001 — un fallo de RIPOR no debe tumbar la subida completa
        return {"estado": "error", "detalle": sin_secretos(e)}


def _publicar_ripor(reporte: Reporte, tareas: list[Tarea], dry_run: bool) -> dict:
    """
    Registra el día en RIPOR, como se hace a mano: un solo registro con
      - la descripción: el daily de ese día (ver daily_de),
      - las horas: 8,5 (lun–jue) / 8 (vie), descontando lo ya reportado ese día,
      - la evidencia: el enlace del Doc de ese día en Drive (o el pegado a mano),
      - el medio de transporte guardado en la página.
    El día es el del reporte salvo que se elija otro (p. ej. uno que faltó). En
    simulación solo valida (preparar_reporte). Un registro no se puede editar ni
    borrar desde aquí: cada fecha se registra una sola vez, y antes de enviar se
    comprueba en RIPOR que ese día no tenga ya un registro de la misma tarea.
    """
    fecha = reporte.ripor_fecha or reporte.fecha
    if reporte.ripor_registros.get(fecha):
        olvido = (" ¿Olvidaste pulsar «Nuevo día»?" if fecha < date.today().isoformat()
                  and fecha == reporte.fecha else "")
        return {"estado": "omitido", "detalle": f"El daily del {fecha} ya se registró en RIPOR "
                                                f"({reporte.ripor_registros[fecha]}); no se repite.{olvido}"}
    evidencia = reporte.ripor_evidencia or doc_del_dia(reporte, fecha)
    if not evidencia:
        if fecha != reporte.fecha:
            return {"estado": "error", "detalle": f"No tengo el enlace del Doc de Drive del {fecha}: "
                                                  "pégalo en «Evidencia» (Datos del reporte → RIPOR)."}
        if not dry_run:
            return {"estado": "error", "detalle": "La evidencia de RIPOR es el Doc del día en Drive: "
                                                  "sube también a Drive."}
        evidencia = "https://docs.google.com/document/d/pendiente"  # el Doc aún no existe en la simulación
    if not reporte.transporte:
        return {"estado": "error", "detalle": "Elige el medio de transporte en «Datos del reporte → RIPOR»."}
    # RIPOR no acepta nada que parezca HTML en la descripción.
    descripcion = daily_de(reporte, fecha, tareas).replace("<", "‹")[:5000]

    try:
        with RiporMCP() as ripor:
            proyecto, tarea, nombre = destino_ripor(ripor, reporte, fecha)
            # Siempre se mira el día en RIPOR antes de registrar: si ya hay un registro
            # de esa tarea (hecho a mano, o uno anterior cuya respuesta no llegó), no se
            # envía otro. Un registro no se puede deshacer desde aquí.
            dia = ripor.llamar("estado_de_fecha", {"fecha": fecha})
            # Se compara proyecto (CECO o nombre) Y tarea: «Tarea personalizada» existe en
            # todos los proyectos y un registro en otro proyecto no cuenta.
            parte_proyecto, _, parte_tarea = nombre.partition(" › ")
            ceco, _, nombre_proyecto = parte_proyecto.partition(" · ")
            previas = [a for a in dia.get("actividades_del_dia") or []
                       if _normalizar(a.get("tarea")) == _normalizar(parte_tarea)
                       and (_normalizar(a.get("ceco")) == _normalizar(ceco)
                            or _normalizar(a.get("proyecto")) == _normalizar(nombre_proyecto))]
            if previas:
                horas_previas = sum(float(a.get("horas") or 0) for a in previas)
                return {"estado": "omitido", "detalle": f"RIPOR ya tiene un registro de {nombre} el {fecha} "
                                                        f"({horas_previas:g} h); no se envía otro. "
                                                        "Si falta algo, corrígelo en ripor.co."}
            horas = reporte.ripor_horas
            if not horas:
                if not dia.get("puede_reportar"):
                    motivos = "; ".join(f"[{m.get('regla')}] {m.get('mensaje')}" for m in dia.get("motivos") or [])
                    return {"estado": "error", "detalle": f"RIPOR no deja reportar el {fecha}: {motivos}"}
                horas = (dia.get("horas") or {}).get("disponibles")
                if not horas:
                    return {"estado": "error", "detalle": f"RIPOR no informó las horas disponibles del {fecha}."}
            datos = {"fecha": fecha, "project_id": proyecto, "task_id": tarea, "horas": horas,
                     "descripcion": descripcion, "evidencia": evidencia,
                     "medio_transporte": reporte.transporte}
            if dry_run:
                r = ripor.llamar("preparar_reporte", datos)
                if not r.get("se_puede_registrar"):
                    return {"estado": "error", "detalle": _incumplimientos(r)}
                archivado = (ruta_reporte().parent / "historial" / f"reporte_{fecha}.json").exists()
                origen_texto = ("" if fecha == reporte.fecha or archivado else
                                " (no hay reporte archivado de ese día: se usan las tareas seleccionadas)")
                return {"estado": "simulado",
                        "detalle": f"Registraría {horas} h el {fecha} en {nombre}; evidencia: {evidencia}; "
                                   f"el día quedaría en {r.get('resumen', {}).get('horas_del_dia_despues')} h. "
                                   f"Descripción{origen_texto}: {descripcion}"}
            try:
                r = ripor.llamar("registrar_reporte", datos, espera=180)
            except RuntimeError as e:
                if "no respondió" not in str(e) and "se cerró" not in str(e):
                    raise
                return {"estado": "error", "detalle": f"{e} Puede que el registro sí haya quedado: al volver "
                                                      "a subir se comprueba primero en RIPOR y no se duplica."}
    except RuntimeError as e:
        return {"estado": "error", "detalle": sin_secretos(e)}
    if not r.get("guardado"):
        return {"estado": "error", "detalle": _incumplimientos(r)}
    reporte.ripor_registros[fecha] = datetime.now().isoformat(timespec="seconds")
    # Tras registrar se vuelve a lo automático: «hoy», el Doc del día y las horas del día.
    reporte.ripor_fecha = reporte.ripor_evidencia = ""
    reporte.ripor_horas = None
    aviso = f" Aviso: {r['aviso']}" if r.get("aviso") else ""
    return {"estado": "ok", "detalle": f"{horas} h registradas el {fecha} en {nombre}; el día suma "
                                       f"{r.get('horas_del_dia')} h.{aviso}"}


# ---------------------------------------------------------------------------
# 6. Orquestación (la usan la consola y la interfaz web)
# ---------------------------------------------------------------------------

def publicar(reporte: Reporte, ids: list[str] | None = None,
             destinos=DESTINOS, dry_run: bool = False) -> dict:
    """
    Sube las tareas indicadas (todas si ids es None) a los destinos elegidos y
    devuelve el resultado por tarea y por destino:
        {"tareas": {id: {"azure": {...}, "drive": {...}, "chat": {...}}}, ...}
    Cada resultado trae "estado" (ok | error | omitido | simulado) y "detalle".
    """
    elegidas = set(ids) if ids is not None else None
    tareas = [t for t in reporte.tareas if elegidas is None or t.id in elegidas]
    if not tareas:
        raise ValueError("No hay tareas seleccionadas para subir.")
    res: dict[str, dict] = {t.id: {} for t in tareas}
    salida = {"tareas": res, "drive_url": None, "mensaje_chat": None}

    def a_todas(destino: str, estado: str, detalle: str) -> None:
        for t in tareas:
            res[t.id][destino] = {"estado": estado, "detalle": detalle}

    # Primero Azure, para que Drive ya tenga los enlaces a los TK.
    if "devops" in destinos:
        try:
            ado = AzureDevOps(dry_run)
        except Exception as e:
            a_todas("azure", "error", str(e))
        else:
            for t in tareas:
                res[t.id]["azure"] = ado.publicar_tarea(t, reporte)

    if "drive" in destinos:
        # El Doc del día es acumulativo: lleva lo seleccionado, lo que ya se había
        # subido a Drive hoy y lo que ya estaba en el Doc aunque se haya quitado de
        # la lista con «Limpiar tareas» (lo publicado no se pierde).
        en_doc = [t for t in reporte.tareas if t.id in res
                  or t.resultado.get("drive", {}).get("estado") == "ok"]
        actuales = {t.id for t in reporte.tareas}
        # La misma tarea puede volver con otro id (p. ej. reimportada de Calendar tras Limpiar).
        vistas = {(t.seccion, _normalizar(t.descripcion)) for t in en_doc}
        for x in reporte.drive_doc.get(reporte.fecha, []):
            if x.get("id") in actuales or (x.get("seccion"), _normalizar(x.get("descripcion"))) in vistas:
                continue
            try:
                en_doc.append(tarea_desde_dict(x))
                vistas.add((x.get("seccion"), _normalizar(x.get("descripcion"))))
            except ValueError:
                pass
        try:
            doc = publicar_drive(reporte, en_doc, dry_run)
            salida["drive_url"] = doc.get("url")
            if doc.get("url"):
                reporte.drive_url = reporte.drive_urls[reporte.fecha] = doc["url"]
            if not dry_run:
                reporte.drive_doc = {reporte.fecha: [
                    {"id": t.id, "seccion": t.seccion, "descripcion": t.descripcion, "tks": t.tks}
                    for t in en_doc]}
            detalle = f"Incluida en el documento ({doc.get('accion')})."
            if doc.get("aviso"):
                detalle += f" Aviso: {doc['aviso']}"
            if doc.get("accion") == "recreado" and reporte.ripor_registros.get(reporte.fecha):
                detalle += (" Ojo: el Doc se recreó y cambió de enlace; el registro de RIPOR de hoy "
                            "apunta al anterior (revísalo en ripor.co).")
            a_todas("drive", "simulado" if dry_run else "ok",
                    "Se incluiría en el documento del día." if dry_run else detalle)
        except Exception as e:
            a_todas("drive", "error", sin_secretos(e))

    if "chat" in destinos:
        try:
            salida["mensaje_chat"] = publicar_chat(reporte, tareas, dry_run)
            a_todas("chat", "simulado" if dry_run else "ok",
                    "Se incluiría en el mensaje." if dry_run else "Incluida en el mensaje enviado.")
        except Exception as e:
            a_todas("chat", "error", sin_secretos(e))

    # RIPOR al final: su evidencia es el enlace del Doc de Drive del día.
    if "ripor" in destinos:
        r = publicar_ripor(reporte, tareas, dry_run)
        a_todas("ripor", r["estado"], r["detalle"])

    if not dry_run:
        marca = datetime.now().isoformat(timespec="seconds")
        for t in tareas:
            t.resultado = {**t.resultado, **res[t.id], "fecha": marca}
    return salida


ICONOS = {"ok": "✔", "error": "✖", "omitido": "–", "simulado": "◌"}


def main() -> int:
    # La consola de Windows usa cp1252 por defecto y rompe con tildes/emojis.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Publica el reporte diario.")
    parser.add_argument("entrada", nargs="?", default=None,
                        help="JSON del reporte (por defecto reporte_hoy.json)")
    parser.add_argument("--solo", nargs="+", choices=list(DESTINOS),
                        help="Publicar solo en estos destinos")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar lo que se haría sin enviar nada")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(BASE_DIR / ".env")

    ruta = ruta_reporte(args.entrada)
    try:
        reporte = cargar_reporte(ruta)
    except (OSError, ValueError) as e:  # JSON ausente, mal formado o con datos inválidos
        log.error("✖ No se publicó nada. Error en %s: %s", ruta.name, e)
        return 1
    if not reporte.tareas:
        log.error("✖ %s no tiene tareas. Agrégalas con: python servidor.py", ruta.name)
        return 1

    log.info("Reporte %s: %d tarea(s).\n", reporte.fecha, len(reporte.tareas))
    destinos = list(args.solo or DESTINOS_CONSOLA)
    if not args.solo and all(t.resultado.get("chat", {}).get("estado") == "ok" for t in reporte.tareas):
        destinos.remove("chat")
        log.info("Chat: el daily ya se envió; no se repite (usa --solo chat para reenviarlo).\n")
    salida = publicar(reporte, None, destinos, args.dry_run)

    hay_error = False
    for t in reporte.tareas:
        log.info("• [%s] %s", t.seccion, t.descripcion.splitlines()[0][:90])
        for destino, r in salida["tareas"][t.id].items():
            hay_error |= r["estado"] == "error"
            log.info("    %s %-6s %s", ICONOS.get(r["estado"], "?"), destino, r["detalle"])
    if salida["drive_url"]:
        log.info("\nDocumento: %s", salida["drive_url"])
    if args.dry_run and salida["mensaje_chat"]:
        log.info("\nMensaje de Chat que se enviaría:\n%s", salida["mensaje_chat"])

    if not args.dry_run:
        guardar_reporte(reporte, ruta)
    log.info("\n%s", "⚠ Hubo errores (ver ✖ arriba)." if hay_error else "✔ Listo.")
    return 1 if hay_error else 0


if __name__ == "__main__":
    sys.exit(main())
