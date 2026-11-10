/**
 * Receptor del reporte diario (Google Apps Script, publicado como Aplicación web).
 *
 * reporte_diario.py le envía por POST un JSON:
 *   { "secreto": "...", "nombre": "Reporte diario - 2026-11-05", "html": "<html>..." }
 * y este script crea —o actualiza, si ya existe— un Google Doc con ese nombre
 * en la carpeta configurada. Responde siempre JSON:
 *   { "ok": true, "accion": "creado|actualizado", "url": "https://docs.google.com/..." }
 *   { "ok": false, "error": "..." }
 *
 * Con { "secreto": "...", "accion": "eventos", "desde": ISO, "hasta": ISO }
 * devuelve las reuniones de tu Google Calendar en ese rango:
 *   { "ok": true, "eventos": [ { "id", "titulo", "inicio", "fin" } ] }
 *
 * Configuración (Configuración del proyecto ⚙ → Propiedades del script):
 *   SECRETO     clave compartida con el .env (GDRIVE_APPS_SCRIPT_SECRET)
 *   CARPETA_ID  ID de la carpeta de Drive destino
 *
 * Requiere el servicio avanzado "Drive API" (v3): Editor → Servicios (+) → Drive API.
 */

const MIME_DOC = 'application/vnd.google-apps.document';

function doPost(e) {
  try {
    const props = PropertiesService.getScriptProperties();
    const secreto = props.getProperty('SECRETO');
    const carpetaId = props.getProperty('CARPETA_ID');
    if (!secreto || !carpetaId) {
      return responder({ ok: false, error: 'Faltan las propiedades SECRETO o CARPETA_ID en el script.' });
    }

    // 1. Validar la petición. Como la app web es pública, el secreto es lo
    //    único que impide que otra persona escriba en tu Drive.
    const datos = JSON.parse(e.postData.contents);
    if (datos.secreto !== secreto) {
      return responder({ ok: false, error: 'Secreto inválido.' });
    }
    if (datos.accion === 'eventos') {
      return responder(listarEventos(datos.desde, datos.hasta));
    }
    if (!datos.nombre || !datos.html) {
      return responder({ ok: false, error: 'La petición debe traer "nombre" y "html".' });
    }
    // Solo Docs del reporte diario ("<prefijo> - AAAA-MM-DD") y de tamaño razonable: quien
    // tuviera el secreto no podría sobrescribir otros archivos de la carpeta.
    if (!/^[^\/]{1,80} - \d{4}-\d{2}-\d{2}$/.test(datos.nombre) || String(datos.html).length > 2000000) {
      return responder({ ok: false, error: 'Nombre o contenido del Doc no válido.' });
    }

    // 2. El HTML se importa como Google Doc (tablas, negritas y enlaces se conservan).
    //    Un candado evita que dos subidas al mismo tiempo creen dos Docs del mismo día.
    const candado = LockService.getScriptLock();
    if (!candado.tryLock(60000)) {
      return responder({ ok: false, error: 'Otra subida a Drive está en curso; intenta de nuevo en un momento.' });
    }
    try {
      return responder(guardarDoc(carpetaId, datos.nombre, datos.html));
    } finally {
      candado.releaseLock();
    }
  } catch (err) {
    return responder({ ok: false, error: String(err) });
  }
}

/** Crea o actualiza el Doc del día y devuelve { ok, accion, url[, aviso] }. */
function guardarDoc(carpetaId, nombre, html) {
  const blob = Utilities.newBlob(html, 'text/html', nombre + '.html');
  const existente = buscarDoc(carpetaId, nombre);
  let id, accion, aviso;

  if (existente) {
    // 3a. Ya existe el Doc de hoy: reemplazar su contenido (mantiene el mismo enlace).
    try {
      conReintentos(() => Drive.Files.update({}, existente.getId(), blob, { supportsAllDrives: true }));
      id = existente.getId();
      accion = 'actualizado';
    } catch (err) {
      // Si Drive sigue sin aceptar la actualización, se crea un Doc nuevo y
      // SOLO cuando ya existe se envía el anterior a la papelera (nunca se
      // queda la carpeta sin reporte). El enlace cambia.
      id = conReintentos(() => crearDoc(carpetaId, nombre, blob));
      accion = 'recreado';
      try {
        existente.setTrashed(true);
      } catch (errPapelera) {
        // El Doc nuevo sí quedó: se responde ok y se avisa del anterior.
        aviso = 'No se pudo enviar el Doc anterior a la papelera: ' + errPapelera;
      }
    }
  } else {
    // 3b. No existe: crearlo dentro de la carpeta.
    id = conReintentos(() => crearDoc(carpetaId, nombre, blob));
    accion = 'creado';
  }

  return { ok: true, accion: accion, url: DriveApp.getFileById(id).getUrl(), aviso: aviso };
}

/**
 * Busca en la carpeta un Google Doc (no otro tipo de archivo) con ese nombre
 * exacto. Si hubiera varios, devuelve el modificado más recientemente.
 */
function buscarDoc(carpetaId, nombre) {
  const archivos = DriveApp.getFolderById(carpetaId).getFilesByName(nombre);
  let elegido = null;
  while (archivos.hasNext()) {
    const f = archivos.next();
    if (f.isTrashed() || f.getMimeType() !== MIME_DOC) continue;
    if (!elegido || f.getLastUpdated() > elegido.getLastUpdated()) elegido = f;
  }
  return elegido;
}

function crearDoc(carpetaId, nombre, blob) {
  const archivo = Drive.Files.create(
    { name: nombre, mimeType: MIME_DOC, parents: [carpetaId] },
    blob,
    { supportsAllDrives: true }
  );
  return archivo.id;
}

/**
 * Ejecuta fn y, si Drive responde un error transitorio ("Internal Error",
 * "Backend Error", límite de tasa), reintenta hasta 3 veces con espera creciente.
 */
function conReintentos(fn) {
  const esperas = [2000, 5000, 10000];
  for (let i = 0; ; i++) {
    try {
      return fn();
    } catch (err) {
      const transitorio = /Internal Error|Backend Error|Rate Limit|User Rate|503|500/i.test(String(err));
      if (!transitorio || i >= esperas.length) throw err;
      Utilities.sleep(esperas[i]);
    }
  }
}

/**
 * Reuniones del calendario principal entre dos fechas. Omite las de todo el
 * día y las que rechazaste; el resto se devuelve con su hora de inicio y fin.
 */
function listarEventos(desde, hasta) {
  const inicio = new Date(desde);
  const fin = new Date(hasta);
  if (isNaN(inicio) || isNaN(fin) || fin <= inicio) {
    return { ok: false, error: 'Las fechas "desde" y "hasta" no son válidas.' };
  }
  // La página pide como mucho unos pocos días (ayer y hoy, con un fin de semana en medio).
  if (fin - inicio > 10 * 24 * 3600 * 1000) {
    return { ok: false, error: 'El rango de fechas es demasiado amplio (máximo 10 días).' };
  }
  const eventos = CalendarApp.getDefaultCalendar().getEvents(inicio, fin)
    .filter((ev) => !ev.isAllDayEvent() && ev.getMyStatus() !== CalendarApp.GuestStatus.NO)
    .map((ev) => ({
      id: ev.getId(),
      titulo: ev.getTitle(),
      inicio: ev.getStartTime().toISOString(),
      fin: ev.getEndTime().toISOString(),
    }));
  return { ok: true, eventos: eventos };
}

function responder(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Ejecútala UNA vez desde el editor (botón ▶ Ejecutar) para conceder los
 * permisos de Drive y Calendar y comprobar la configuración. Revisa el resultado en
 * "Registro de ejecución".
 */
function probarConfiguracion() {
  const props = PropertiesService.getScriptProperties();
  const carpetaId = props.getProperty('CARPETA_ID');
  if (!props.getProperty('SECRETO')) throw new Error('Falta la propiedad SECRETO.');
  if (!carpetaId) throw new Error('Falta la propiedad CARPETA_ID.');

  const carpeta = DriveApp.getFolderById(carpetaId);
  Drive.Files.list({ pageSize: 1, fields: 'files(id)' });  // fuerza el permiso del servicio avanzado
  const calendario = CalendarApp.getDefaultCalendar();  // fuerza el permiso de Calendar
  Logger.log('OK. Carpeta destino: "%s" (%s)', carpeta.getName(), carpeta.getUrl());
  Logger.log('OK. Calendario: "%s"', calendario.getName());
}
