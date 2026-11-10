"use strict";

// Interfaz del reporte diario. Habla con servidor.py (API en /api/...) y
// dibuja todo con DOM puro, sin librerías.

const estado = {
  reporte: null,         // reporte_hoy.json tal como lo devuelve el servidor
  categorias: [],
  config: {},            // qué destinos están configurados en el .env
  estadoTk: "Closed",    // estado por defecto del TK (ADO_TK_STATE)
  transportes: [],       // medios de transporte que acepta RIPOR
  seleccion: new Set(),  // ids de las tareas marcadas
  editando: null,        // id de la tarea en edición (null = tarea nueva)
  modo: "una",           // "una" = formulario normal; "varias" = texto «-Tarea/WI/puntos»
  lote: null,            // última revisión del texto de «Varias a la vez»: {texto, tareas, wis, faltan, aviso}
  workItems: [],         // work items del formulario
  titulosWi: new Map(),  // id → título, para mostrarlo en las fichas
  subida: null,          // resultado de la última subida (o simulación) en esta sesión
  ripor: { asignaciones: null, tareas: new Map(), guardando: false },  // listas de RIPOR (se cargan a pedido)
  relacionando: null,    // id de la tarea con el buscador de work items abierto en la lista
  rapido: { texto: "", resultados: null, buscando: false, error: "", enfocar: false },  // ese buscador
  sesiones: { google: null, ripor: null },   // estado de cada sesión (null = comprobando)
  abiertos: new Set(),   // categorías y secciones desplegadas en la lista
  puntos: [],            // guía de puntos de historia: [{puntos, texto}]
  riporDoc: "",          // enlace del Doc de Drive del día elegido para RIPOR (si se conoce)
  guardandoTarea: false, // hay un alta/edición de tarea en curso
  cambiandoWis: new Set(), // tareas con un cambio de work items en curso
  busquedaWi: 0,         // turno de la búsqueda del formulario (descarta respuestas viejas)
  esperandoSesion: {},   // servicio → true mientras se espera que el usuario inicie sesión
  sesionesRevisadas: 0,  // cuándo se comprobaron por última vez
};

const SERVICIOS = {
  google: { nombre: "Google (Drive y Calendar)", boton: "Iniciar sesión en Google" },
  ripor: { nombre: "RIPOR", boton: "Iniciar sesión en RIPOR" },
};

const SECCIONES = { ayer: "Ayer", hoy: "Hoy" };
const DESTINOS = [["azure", "devops", "Azure"], ["drive", "drive", "Drive"], ["chat", "chat", "Chat"], ["ripor", "ripor", "RIPOR"]];
const ICONOS = { ok: "✓", error: "✗", omitido: "–", simulado: "◌" };
const NOMBRE_ESTADO = { Closed: "cerrado", Active: "activo", New: "nuevo" };

const $ = (selector) => document.querySelector(selector);

/** Solo enlaces http(s): un valor raro (p. ej. "javascript:") no llega a un href. */
const enlaceSeguro = (url) => (/^https?:\/\//i.test(String(url || "")) ? url : null);

/** Un campo numérico con texto inválido entrega "" (y el servidor lo tomaría como «borrar»). */
function numeroValido(campo, nombre) {
  if (campo.validity && campo.validity.badInput) throw new Error(`${nombre}: escribe un número válido.`);
  return campo.value;
}

/** Crea un elemento: el("li", {class: "x", onclick: fn}, "texto", hijo...). */
function el(tag, props = {}, ...hijos) {
  const nodo = document.createElement(tag);
  for (const [clave, valor] of Object.entries(props)) {
    if (valor === undefined || valor === null || valor === false) continue;
    if (clave === "class") nodo.className = valor;
    else if (clave.startsWith("on")) nodo.addEventListener(clave.slice(2), valor);
    else if (typeof valor === "boolean") nodo[clave] = valor;   // checked, indeterminate, hidden…
    else if (clave === "value") nodo.value = valor;
    else nodo.setAttribute(clave, valor);
  }
  for (const hijo of hijos.flat()) {
    if (hijo !== null && hijo !== undefined && hijo !== false) nodo.append(hijo);
  }
  return nodo;
}

async function api(metodo, url, cuerpo) {
  const opciones = { method: metodo, headers: { "X-Reporte": "1" } };
  if (cuerpo !== undefined) {
    opciones.headers["Content-Type"] = "application/json";
    opciones.body = JSON.stringify(cuerpo);
  }
  let respuesta;
  try {
    respuesta = await fetch(url, opciones);
  } catch {
    throw new Error("No hay conexión con el servidor. ¿Sigue abierta la ventana de «python servidor.py»?");
  }
  const datos = await respuesta.json().catch(() => ({}));
  if (!respuesta.ok) {
    const error = new Error(datos.error || `Error HTTP ${respuesta.status}`);
    error.status = respuesta.status;
    error.datos = datos;
    if (datos.sin_llave) avisoFijo(datos.error);
    throw error;
  }
  return datos;
}

/** Aviso que queda fijo arriba (no desaparece solo), p. ej. si falta la llave local. */
function avisoFijo(texto) {
  const caja = $("#aviso-fecha");
  caja.textContent = texto;
  caja.hidden = false;
}

let temporizadorAviso;
function aviso(texto, tipo = "info") {
  const caja = $("#aviso");
  caja.textContent = texto;
  caja.className = `aviso ${tipo}`;
  caja.hidden = false;
  clearTimeout(temporizadorAviso);
  temporizadorAviso = setTimeout(() => { caja.hidden = true; }, tipo === "error" ? 9000 : 5000);
}

function aplicar(datos) {
  estado.reporte = datos.reporte;
  estado.categorias = datos.categorias;
  estado.config = datos.config;
  estado.estadoTk = datos.estado_tk || "Closed";
  estado.transportes = datos.transportes || [];
  estado.puntos = datos.puntos || [];
  estado.riporDoc = datos.ripor_doc || "";
  estado.hoy = datos.hoy || "";
  for (const t of estado.reporte.tareas) {
    for (const [id, titulo] of Object.entries(t.wi_titulos || {})) estado.titulosWi.set(Number(id), titulo);
  }
  const existentes = new Set(estado.reporte.tareas.map((t) => t.id));
  for (const id of [...estado.seleccion]) if (!existentes.has(id)) estado.seleccion.delete(id);
  if (estado.relacionando && !existentes.has(estado.relacionando)) estado.relacionando = null;
  if (estado.editando && !existentes.has(estado.editando)) limpiarFormulario();
  render();
}

const esReunion = (t) => t.origen.startsWith("calendar:") || t.categoria.toLowerCase() === "reuniones";

// ---------------------------------------------------------------------------
// Dibujo
// ---------------------------------------------------------------------------

/** Si el reporte es de un día anterior a hoy, lo dice arriba con un botón para iniciar el día. */
function renderAvisoFecha() {
  const caja = $("#aviso-fecha");
  const r = estado.reporte;
  if (!estado.hoy || r.fecha >= estado.hoy) { caja.hidden = true; return; }
  caja.replaceChildren(
    el("span", {}, `⚠ El reporte es del ${r.fecha} y hoy es ${estado.hoy}. Si empiezas el día, pulsa «Nuevo día»; `
      + "si subes así, todo saldrá con la fecha del reporte."),
    el("button", { type: "button", class: "primario", onclick: () => $("#btn-nuevo-dia").click() }, "Nuevo día"));
  caja.hidden = false;
}

function render() {
  renderCabecera();
  renderAvisoFecha();
  renderCabeceraRipor();
  $("#lista-categorias").replaceChildren(...estado.categorias.map((c) => el("option", { value: c })));
  $("#f-estado-tk").options[0].textContent = `Por defecto (${NOMBRE_ESTADO[estado.estadoTk]})`;
  renderPuntos();
  renderGrupos();
  renderBarra();
}

function renderCabecera() {
  const r = estado.reporte;
  for (const campo of $("#cabecera").querySelectorAll("[name]")) {
    if (document.activeElement === campo) continue; // no pisar lo que se está escribiendo
    campo.value = r[campo.name] ?? "";
  }
}

function agrupar(tareas) {
  const grupos = new Map();
  const orden = (t) => `${t.seccion === "ayer" ? 0 : 1}${t.inicio || "99"}`;
  for (const t of [...tareas].sort((a, b) => orden(a).localeCompare(orden(b)))) {
    const categoria = t.categoria || "Sin categoría";
    if (!grupos.has(categoria)) grupos.set(categoria, []);
    grupos.get(categoria).push(t);
  }
  return [...grupos.entries()].sort((a, b) => a[0].localeCompare(b[0], "es"));
}

function marcar(ids, activo) {
  for (const id of ids) activo ? estado.seleccion.add(id) : estado.seleccion.delete(id);
  render();
}

// Categorías y secciones (Ayer/Hoy) plegables; se recuerdan las abiertas en este navegador.
const claveGrupo = (categoria, seccion) => (seccion ? `${categoria}|${seccion}` : categoria);

function cargarAbiertos() {
  try { estado.abiertos = new Set(JSON.parse(localStorage.getItem("grupos-abiertos") || "[]")); } catch { /* sin almacenamiento */ }
}

function alternarGrupo(clave) {
  estado.abiertos.has(clave) ? estado.abiertos.delete(clave) : estado.abiertos.add(clave);
  try { localStorage.setItem("grupos-abiertos", JSON.stringify([...estado.abiertos])); } catch { /* sin almacenamiento */ }
  renderGrupos();
}

function plegarTodo(abrir) {
  estado.abiertos = new Set();
  if (abrir) for (const t of estado.reporte.tareas) mostrarTarea(t);
  try { localStorage.setItem("grupos-abiertos", JSON.stringify([...estado.abiertos])); } catch { /* sin almacenamiento */ }
  renderGrupos();
}

/** Abre la categoría y la sección de una tarea (al agregarla o editarla, para que se vea). */
function mostrarTarea(t) {
  const categoria = t.categoria || "Sin categoría";
  estado.abiertos.add(claveGrupo(categoria));
  estado.abiertos.add(claveGrupo(categoria, t.seccion));
}

/** Barra de un grupo: casilla para marcar todo, título que pliega/despliega y conteo. */
function barraGrupo(clave, titulo, lista, clase, contexto = "") {
  const ids = lista.map((t) => t.id);
  const marcadas = ids.filter((id) => estado.seleccion.has(id)).length;
  const abierto = estado.abiertos.has(clave);
  return el("div", { class: `grupo-cabecera ${clase}` },
    el("input", {
      type: "checkbox",
      checked: marcadas === ids.length,
      indeterminate: marcadas > 0 && marcadas < ids.length,
      "aria-label": `Seleccionar todas las tareas de ${contexto}${titulo}`,
      onchange: (e) => marcar(ids, e.target.checked),
    }),
    el("button", { type: "button", class: "plegar", "aria-expanded": String(abierto), onclick: () => alternarGrupo(clave) },
      el("span", { class: "flecha", "aria-hidden": "true" }, abierto ? "▾" : "▸"),
      el("strong", {}, titulo),
      el("span", { class: "conteo" }, ` ${lista.length} tarea${lista.length === 1 ? "" : "s"}`)),
    el("span", { class: "conteo" }, `${marcadas}/${ids.length} seleccionadas`));
}

function renderGrupos() {
  const contenedor = $("#grupos");
  const tareas = estado.reporte.tareas;
  if (!tareas.length) {
    contenedor.replaceChildren(el("p", { class: "vacio" },
      "No hay tareas. Crea una con el formulario o importa tus reuniones de Calendar."));
    return;
  }
  contenedor.replaceChildren(...agrupar(tareas).map(([categoria, lista]) => {
    const secciones = Object.keys(SECCIONES).map((s) => [s, lista.filter((t) => t.seccion === s)])
      .filter(([, sub]) => sub.length);
    return el("section", { class: "grupo" },
      barraGrupo(claveGrupo(categoria), categoria, lista, "nivel-categoria"),
      estado.abiertos.has(claveGrupo(categoria)) ? secciones.map(([seccion, sub]) =>
        el("div", { class: "subgrupo" },
          barraGrupo(claveGrupo(categoria, seccion), SECCIONES[seccion], sub, `nivel-seccion ${seccion}`, `${categoria}, `),
          estado.abiertos.has(claveGrupo(categoria, seccion)) ? el("ul", { class: "tareas" }, sub.map(filaTarea)) : null))
        : null);
  }));
  // Al abrir el buscador de una reunión, el cursor queda listo para escribir (solo esa vez).
  if (estado.rapido.enfocar) {
    estado.rapido.enfocar = false;
    contenedor.querySelector(".relacionar-caja input")?.focus();
  }
}

function filaTarea(t) {
  const meta = [];
  if (!t.work_items.length) meta.push("Sin work item: no crea TK");
  else if (!t.crear_tk) meta.push("no crea TK");
  else meta.push(`TK ${NOMBRE_ESTADO[t.estado_tk || estado.estadoTk]}`);
  if (t.tipo) meta.push(t.tipo);
  if (t.inicio) meta.push(t.fin ? `${t.inicio}–${t.fin}` : `desde ${t.inicio}`);
  if (t.horas) meta.push(`${t.horas} h`);
  if (t.puntos) meta.push(`${t.puntos} pts`);

  const enlacesTk = t.tks.flatMap((x, i) => [
    i ? ", " : " · ",
    el("a", { href: enlaceSeguro(x.url), target: "_blank", rel: "noopener" }, `TK ${x.tk}`),
  ]);

  return el("li", { class: `tarea${estado.editando === t.id ? " editando" : ""}` },
    el("input", {
      type: "checkbox",
      checked: estado.seleccion.has(t.id),
      "aria-label": `Seleccionar: ${t.descripcion}`,
      onchange: (e) => marcar([t.id], e.target.checked),
    }),
    el("div", { class: "tarea-cuerpo" },
      el("div", { class: "tarea-linea" },
        el("span", { class: `seccion ${t.seccion}` }, SECCIONES[t.seccion]),
        el("span", { class: "descripcion" }, t.descripcion)),
      fichasWiTarea(t),
      el("div", { class: "meta" }, meta.join(" · "), enlacesTk),
      esReunion(t) ? relacionarRapido(t) : null,
      resultadoTarea(t)),
    el("div", { class: "tarea-acciones" },
      el("button", { type: "button", class: "enlace", "aria-label": `Editar: ${t.descripcion}`, onclick: () => editar(t.id) }, "Editar"),
      el("button", { type: "button", class: "enlace peligro", "aria-label": `Eliminar: ${t.descripcion}`, onclick: () => eliminar(t) }, "Eliminar")));
}

/** Work items de la tarea como fichas con su título; la × los quita sin abrir el formulario. */
function fichasWiTarea(t) {
  if (!t.work_items.length) return null;
  return el("div", { class: "fichas-wi en-lista" }, t.work_items.map((id) => {
    const titulo = estado.titulosWi.get(id);
    return el("span", { class: "ficha-wi", title: titulo || `Work item ${id}` },
      el("span", {}, titulo ? `${id} · ${titulo}` : `WI ${id}`),
      el("button", {
        type: "button",
        "aria-label": `Quitar work item ${id}`,
        onclick: () => cambiarWis(t, t.work_items.filter((x) => x !== id), `WI ${id} quitado de la tarea.`),
      }, "×"));
  }));
}

/** Debajo de cada reunión: buscar y relacionar un work item sin pasar por «Editar». */
function relacionarRapido(t) {
  if (estado.relacionando !== t.id) {
    return el("button", { type: "button", class: "enlace relacionar", onclick: () => abrirRelacionar(t.id) },
      t.work_items.length ? "+ Relacionar otro work item" : "+ Relacionar work item (para crear el TK)");
  }
  const r = estado.rapido;
  let lista = null;
  if (r.buscando) lista = el("p", { class: "ayuda" }, "Buscando en Azure DevOps…");
  else if (r.error) lista = el("p", { class: "error" }, r.error);
  else if (r.resultados && !r.resultados.length) lista = el("p", { class: "ayuda" }, `Sin resultados para «${r.texto}».`);
  else if (r.resultados) {
    lista = el("ul", { class: "resultados-wi" }, r.resultados.map((w) =>
      el("li", {}, botonResultado(w, () => relacionarWi(t, w)))));
  }
  return el("div", { class: "relacionar-caja" },
    el("div", { class: "buscador" },
      el("input", {
        type: "search",
        value: r.texto,
        placeholder: "HU533 o número de work item",
        "aria-label": `Buscar work item para: ${t.descripcion}`,
        oninput: (e) => { r.texto = e.target.value; },
        onkeydown: (e) => {
          if (e.key === "Enter") { e.preventDefault(); buscarRapido(); }
          if (e.key === "Escape") cerrarRelacionar();
        },
      }),
      el("button", { type: "button", class: "secundario", onclick: buscarRapido }, "Buscar"),
      el("button", { type: "button", class: "enlace", onclick: cerrarRelacionar }, "Cerrar")),
    lista);
}

function abrirRelacionar(id) {
  estado.relacionando = id;
  estado.rapido = { texto: "", resultados: null, buscando: false, error: "", enfocar: true };
  renderGrupos();
}

function cerrarRelacionar() {
  estado.relacionando = null;
  renderGrupos();
}

async function buscarRapido() {
  const r = estado.rapido;
  if (!r.texto.trim()) return;
  Object.assign(r, { buscando: true, error: "", resultados: null });
  renderGrupos();
  try {
    r.resultados = (await api("GET", `/api/buscar?q=${encodeURIComponent(r.texto.trim())}`)).resultados;
  } catch (error) {
    r.error = error.message;
  }
  r.buscando = false;
  r.enfocar = true;  // el buscador sigue listo para otra búsqueda
  renderGrupos();
}

async function relacionarWi(t, w) {
  estado.titulosWi.set(w.id, w.titulo);
  estado.relacionando = null;
  await cambiarWis(t, [...new Set([...t.work_items, w.id])], `WI ${w.id} relacionado con la tarea.`);
}

/** Guarda la lista de work items de una tarea directamente desde la lista. */
async function cambiarWis(t, workItems, mensaje) {
  // Un cambio a la vez por tarea: dos clics seguidos en × no se pisan entre sí.
  if (estado.cambiandoWis.has(t.id)) return aviso("Espera a que termine el cambio anterior de esa tarea.", "error");
  estado.cambiandoWis.add(t.id);
  const titulos = Object.fromEntries(workItems.filter((id) => estado.titulosWi.has(id))
    .map((id) => [id, estado.titulosWi.get(id)]));
  try {
    aplicar(await api("PUT", `/api/tareas/${t.id}`, { ...t, work_items: workItems, wi_titulos: titulos }));
    if (estado.editando === t.id) {
      // Se aplica el mismo cambio al formulario sin perder lo que el usuario agregó allí.
      const quitados = t.work_items.filter((id) => !workItems.includes(id));
      const agregados = workItems.filter((id) => !t.work_items.includes(id));
      estado.workItems = [...estado.workItems.filter((id) => !quitados.includes(id)),
        ...agregados.filter((id) => !estado.workItems.includes(id))];
      renderWis();
    }
    aviso(mensaje);
  } catch (error) {
    aviso(error.message, "error");
  } finally {
    estado.cambiandoWis.delete(t.id);
  }
}

/** Fichas ✓/✗ por destino con el motivo; usa la última subida de esta sesión o la guardada. */
function resultadoTarea(t) {
  const simulado = estado.subida?.simulada ? estado.subida.tareas?.[t.id] : null;
  const bloques = [fichasResultado(t.resultado, false), fichasResultado(simulado, true)].filter(Boolean);
  return bloques.length ? el("div", { class: "resultado" }, bloques) : null;
}

function fichasResultado(r, esSimulacion) {
  if (!r || !DESTINOS.some(([clave]) => r[clave])) return null;
  const fichas = esSimulacion ? [el("span", { class: "cuando" }, "Simulación:")] : [];
  const detalles = [];
  for (const [clave, , nombre] of DESTINOS) {
    const x = r[clave];
    if (!x) continue;
    fichas.push(el("span", { class: `ficha ${x.estado}`, title: x.detalle }, `${ICONOS[x.estado] || "?"} ${nombre}`));
    // Azure y RIPOR siempre explican qué pasó; Drive y Chat solo si fallaron.
    if (clave === "azure" || clave === "ripor" || x.estado === "error") {
      detalles.push(el("li", { class: x.estado }, `${nombre}: ${x.detalle}`));
    }
  }
  const cuando = !esSimulacion && r.fecha ? el("span", { class: "cuando" }, `Última subida: ${r.fecha.replace("T", " ")}`) : null;
  return el("div", {},
    el("div", { class: "fichas" }, fichas, cuando),
    detalles.length ? el("ul", { class: "detalles" }, detalles) : null);
}

function renderBarra() {
  const total = estado.reporte.tareas.length;
  const marcadas = estado.seleccion.size;
  const todas = $("#sel-todas");
  todas.checked = total > 0 && marcadas === total;
  todas.indeterminate = marcadas > 0 && marcadas < total;
  todas.disabled = total === 0;
  const nuevosTk = estado.reporte.tareas.filter((t) => estado.seleccion.has(t.id) && t.crear_tk)
    .reduce((n, t) => n + t.work_items.filter((wi) => !t.tks.some((x) => x.wi === wi)).length, 0);
  $("#contador").textContent = `${marcadas} de ${total} seleccionadas`
    + (marcadas ? ` · ${nuevosTk} TK nuevo${nuevosTk === 1 ? "" : "s"}` : "");

  const riporBloqueado = estado.sesiones.ripor?.bloqueado;
  for (const [, destino, nombre] of DESTINOS) {
    const casilla = $(`#dest-${destino}`);
    const listo = estado.config[destino] && !(destino === "ripor" && riporBloqueado);
    casilla.disabled = !listo;
    if (!listo) casilla.checked = false;
    let motivo = "";
    if (!listo && destino !== "ripor") motivo = `${nombre} no está configurado en el .env`;
    else if (!listo) motivo = riporBloqueado ? "Node.js está bloqueado en este equipo" : "Falta el conector de RIPOR";
    casilla.parentElement.title = motivo;
  }
  const boton = $("#btn-subir");
  if (!boton.dataset.ocupado) boton.disabled = marcadas === 0;
}

// ---------------------------------------------------------------------------
// RIPOR: un registro por día (proyecto y tarea se eligen solos si no se indican)
// ---------------------------------------------------------------------------

const textoProyecto = (a) => `${a.ceco} · ${a.proyecto}`;
const esPersonalizada = (x) => /personalizad/i.test(x.nombre);

/** Llena un <select> de proyectos de RIPOR conservando el valor guardado aunque la lista no esté cargada. */
function opcionesProyecto(select, valor, nombre) {
  const lista = estado.ripor.asignaciones || [];
  const opciones = [el("option", { value: "" }, "Automático (tu proyecto vigente)"),
    ...lista.map((a) => el("option", { value: String(a.project_id) },
      textoProyecto(a) + (a.cubre_la_fecha ? "" : " (fuera de fechas)")))];
  if (valor && !lista.some((a) => a.project_id === valor)) {
    opciones.push(el("option", { value: String(valor) }, nombre.split(" › ")[0] || `Proyecto ${valor}`));
  }
  select.replaceChildren(...opciones);
  select.value = valor ? String(valor) : "";
}

function opcionesTarea(select, proyecto, valor, nombre) {
  const lista = (proyecto && estado.ripor.tareas.get(proyecto)) || [];
  const opciones = [el("option", { value: "" }, "Automático («Tarea personalizada»)"),
    ...lista.map((x) => el("option", { value: String(x.task_id) }, x.nombre))];
  if (valor && !lista.some((x) => x.task_id === valor)) {
    opciones.push(el("option", { value: String(valor) }, nombre.split(" › ")[1] || `Tarea ${valor}`));
  }
  select.replaceChildren(...opciones);
  select.value = valor ? String(valor) : "";
}

/** "CECO · proyecto › tarea" a partir de lo elegido. */
function nombreRipor(selProyecto, selTarea) {
  if (!selProyecto.value) return "";
  const proyecto = selProyecto.selectedOptions[0].textContent.replace(" (fuera de fechas)", "");
  return selTarea.value ? `${proyecto} › ${selTarea.selectedOptions[0].textContent}` : proyecto;
}

async function cargarTareasRipor(proyecto) {
  if (!proyecto || estado.ripor.tareas.has(proyecto)) return;
  const r = await api("GET", `/api/ripor/tareas?project_id=${proyecto}`);
  estado.ripor.tareas.set(proyecto, r.tareas);
}

function renderCabeceraRipor() {
  const r = estado.reporte;
  const caja = $("#cabecera-ripor");
  caja.hidden = !estado.config.ripor;
  const dia = r.ripor_fecha || r.fecha;
  // Mientras se guarda un cambio, o el usuario está en un campo, no se pisa lo que eligió.
  if (!estado.ripor.guardando) {
    const enUso = (campo) => document.activeElement === campo;
    const selProyecto = caja.querySelector("[name=ripor_proyecto]");
    const selTarea = caja.querySelector("[name=ripor_tarea]");
    if (!enUso(selProyecto) && !enUso(selTarea)) {
      opcionesProyecto(selProyecto, r.ripor_proyecto, r.ripor_nombre);
      opcionesTarea(selTarea, r.ripor_proyecto, r.ripor_tarea, r.ripor_nombre);
    }
    const horas = caja.querySelector("[name=ripor_horas]");
    if (!enUso(horas)) horas.value = r.ripor_horas ?? "";
    const transporte = caja.querySelector("[name=transporte]");
    if (!enUso(transporte)) {
      transporte.replaceChildren(
        ...(r.transporte ? [] : [el("option", { value: "" }, "— Elige —")]),
        ...estado.transportes.map((x) => el("option", { value: x.id }, x.nombre)));
      transporte.value = r.transporte || "";
    }
    // Día a reportar: por defecto el del reporte; se puede elegir uno anterior que haya faltado.
    const campoDia = caja.querySelector("[name=ripor_fecha]");
    if (!enUso(campoDia)) campoDia.value = dia;
    campoDia.max = r.fecha;
    const evidencia = caja.querySelector("[name=ripor_evidencia]");
    if (!enUso(evidencia)) evidencia.value = r.ripor_evidencia || "";
  }
  const evidencia = caja.querySelector("[name=ripor_evidencia]");
  evidencia.placeholder = estado.riporDoc
    ? `Automático: el Doc de Drive del ${dia}`
    : (dia === r.fecha ? "Automático: el Doc de hoy (se crea al subir a Drive)" : `Pega el enlace del Doc de Drive del ${dia}`);

  const registrado = (r.ripor_registros || {})[dia];
  const otroDia = dia !== r.fecha ? " (no es el día del reporte)" : "";
  let texto = registrado
    ? `✓ El daily del ${dia} ya se registró en RIPOR (${registrado.replace("T", " ")}).`
    : `Se registrará el día ${dia}${otroDia}.`;
  if (!registrado && dia !== r.fecha && !estado.riporDoc && !r.ripor_evidencia) {
    texto += " Falta el enlace del Doc de ese día: pégalo en «Evidencia».";
  }
  // Con «Automático», qué se usaría (calculado con las listas cargadas; no se guarda).
  if (!r.ripor_proyecto && estado.ripor.asignaciones) {
    const vigentes = estado.ripor.asignaciones.filter((a) => a.cubre_la_fecha);
    if (vigentes.length === 1) {
      const tareas = (estado.ripor.tareas.get(vigentes[0].project_id) || []).filter(esPersonalizada);
      texto += ` Automático: ${textoProyecto(vigentes[0])}${tareas.length === 1 ? ` › ${tareas[0].nombre}` : ""}.`;
    } else {
      texto += ` Tienes ${vigentes.length} proyectos vigentes: elige uno.`;
    }
  }
  $("#estado-ripor").textContent = texto;
}

async function cambioCabeceraRipor(evento) {
  const caja = evento.currentTarget;
  const proyecto = caja.querySelector("[name=ripor_proyecto]");
  const tarea = caja.querySelector("[name=ripor_tarea]");
  // El proyecto elegido se toma ANTES de esperar: un render en medio no lo cambia.
  const elegido = proyecto.value;
  if (evento.target.name === "ripor_fecha") caja.querySelector("[name=ripor_evidencia]").value = "";
  estado.ripor.guardando = true;
  let respuesta;
  try {
    if (evento.target === proyecto) {
      await cargarTareasRipor(Number(elegido) || null);
      proyecto.value = elegido;
      opcionesTarea(tarea, Number(elegido) || null, null, "");
      const unica = (estado.ripor.tareas.get(Number(elegido)) || []).filter(esPersonalizada);
      if (unica.length === 1) tarea.value = String(unica[0].task_id);
    }
    respuesta = await api("PUT", "/api/reporte", {
      ripor_proyecto: elegido,
      ripor_tarea: tarea.value,
      ripor_nombre: nombreRipor(proyecto, tarea),
      ripor_horas: numeroValido(caja.querySelector("[name=ripor_horas]"), "Horas dedicadas"),
      transporte: caja.querySelector("[name=transporte]").value,
      ripor_fecha: caja.querySelector("[name=ripor_fecha]").value,
      ripor_evidencia: caja.querySelector("[name=ripor_evidencia]").value.trim(),
    });
  } catch (error) {
    aviso(error.message, "error");
    respuesta = await api("GET", "/api/estado").catch(() => null);
  } finally {
    estado.ripor.guardando = false;
  }
  if (respuesta) aplicar(respuesta);
}

/** Trae los proyectos y, si no hay uno elegido, deja el único vigente y su «Tarea personalizada». */
async function botonCargarRipor(evento) {
  const boton = evento.currentTarget;
  boton.disabled = true;
  boton.textContent = "Cargando…";
  try {
    const r = estado.reporte;
    const dia = r.ripor_fecha || r.fecha;
    estado.ripor.asignaciones = (await api("GET", `/api/ripor/asignaciones?fecha=${dia}`)).asignaciones;
    const vigentes = estado.ripor.asignaciones.filter((a) => a.cubre_la_fecha);
    const proyecto = r.ripor_proyecto || (vigentes.length === 1 ? vigentes[0].project_id : null);
    await cargarTareasRipor(proyecto);
    aviso(`RIPOR: ${vigentes.length} proyecto(s) vigente(s) el ${dia}.`);
  } catch (error) {
    aviso(error.message, "error");
  } finally {
    boton.disabled = false;
    boton.textContent = "Cargar proyectos de RIPOR";
    render();
  }
}

// ---------------------------------------------------------------------------
// Formulario de tarea
// ---------------------------------------------------------------------------

const formulario = () => $("#form-tarea").elements;

function renderWis() {
  $("#wi-fichas").replaceChildren(...estado.workItems.map((id) => {
    const titulo = estado.titulosWi.get(id);
    return el("span", { class: "ficha-wi", title: titulo || `Work item ${id}` },
      el("span", {}, titulo ? `${id} · ${titulo}` : `WI ${id}`),
      el("button", {
        type: "button",
        "aria-label": `Quitar work item ${id}`,
        onclick: () => { estado.workItems = estado.workItems.filter((x) => x !== id); renderWis(); },
      }, "×"));
  }));
  // Los datos del TK solo tienen sentido si la tarea va a crear TK.
  const conWi = estado.workItems.length > 0;
  $("#bloque-tk").hidden = !conWi;
  $("#sin-tk").hidden = conWi;
}

/** Opciones de puntos con su significado ("3 · Mediana: 1 a 2 días"). */
function renderPuntos() {
  const select = $("#f-puntos");
  const valor = select.value;
  select.replaceChildren(el("option", { value: "" }, "Los del work item"),
    ...estado.puntos.map((x) => el("option", { value: String(x.puntos) }, `${x.puntos} · ${x.texto}`)));
  select.value = valor;
  explicarPuntos();
}

function explicarPuntos() {
  const valor = Number($("#f-puntos").value);
  const guia = estado.puntos.find((x) => x.puntos === valor);
  $("#guia-puntos").textContent = guia
    ? `${valor} punto${valor === 1 ? "" : "s"}: ${guia.texto}.`
    : "Se usan los puntos que tenga el work item (si no tiene, 1).";
}

function limpiarFormulario() {
  estado.editando = null;
  estado.workItems = [];
  const seccion = formulario().seccion.value;
  $("#form-tarea").reset();
  formulario().seccion.value = seccion;
  $("#titulo-form").textContent = "Nueva tarea";
  $("#btn-guardar").textContent = "Agregar tarea";
  $("#btn-cancelar").hidden = true;
  $("#error-form").textContent = "";
  $("#resultados-wi").replaceChildren();
  estado.busquedaWi++;  // descarta respuestas de búsquedas pendientes
  $("#detalles-tk").open = false;
  estado.lote = null;
  renderWis();
  explicarPuntos();
  renderModo();
}

function editar(id) {
  const t = estado.reporte.tareas.find((x) => x.id === id);
  if (!t) return;
  estado.modo = "una";  // una tarea se edita siempre en el formulario normal
  limpiarFormulario();
  estado.editando = id;
  mostrarTarea(t);
  const f = formulario();
  f.seccion.value = t.seccion;
  f.descripcion.value = t.descripcion;
  f.categoria.value = t.categoria;
  f.tarea.value = t.tarea;
  f.tipo.value = t.tipo;
  f.puntos.value = t.puntos ?? "";
  f.estado_tk.value = t.estado_tk;
  f.inicio.value = t.inicio;
  f.fin.value = t.fin;
  f.horas.value = t.horas ?? "";
  f.crear_tk.checked = t.crear_tk;
  estado.workItems = [...t.work_items];
  $("#detalles-tk").open = Boolean(t.tarea || t.tipo || !t.crear_tk);
  explicarPuntos();
  $("#titulo-form").textContent = "Editar tarea";
  $("#btn-guardar").textContent = "Guardar cambios";
  $("#btn-cancelar").hidden = false;
  renderWis();
  render();
  $("#panel-form").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#f-descripcion").focus({ preventScroll: true });
}

function datosFormulario() {
  const f = formulario();
  return {
    seccion: f.seccion.value,
    descripcion: f.descripcion.value.trim(),
    categoria: f.categoria.value.trim(),
    work_items: estado.workItems,
    wi_titulos: Object.fromEntries(estado.workItems.filter((id) => estado.titulosWi.has(id))
      .map((id) => [id, estado.titulosWi.get(id)])),
    tarea: f.tarea.value.trim(),
    tipo: f.tipo.value,
    estado_tk: f.estado_tk.value,
    puntos: f.puntos.value ? Number(f.puntos.value) : null,
    inicio: f.inicio.value,
    fin: f.fin.value,
    horas: numeroValido(f.horas, "Horas") ? Number(f.horas.value) : null,
    crear_tk: f.crear_tk.checked,
  };
}

async function guardarTarea(evento) {
  evento.preventDefault();
  if (estado.modo === "varias") return guardarLote();
  if (estado.guardandoTarea) return;  // doble clic o Ctrl+Enter repetido: no se duplica la tarea
  $("#error-form").textContent = "";
  const editando = estado.editando;  // se toma antes de esperar la respuesta
  const boton = $("#btn-guardar");
  estado.guardandoTarea = true;
  boton.disabled = true;
  try {
    const datos = datosFormulario();
    const respuesta = editando
      ? await api("PUT", `/api/tareas/${editando}`, datos)
      : await api("POST", "/api/tareas", datos);
    if (respuesta.nueva) estado.seleccion.add(respuesta.nueva); // la nueva queda marcada
    mostrarTarea(datos);
    // Si mientras tanto se empezó a editar otra tarea, no se borra ese formulario.
    if (estado.editando === editando) limpiarFormulario();
    aplicar(respuesta);
    aviso(editando ? "Tarea actualizada." : "Tarea agregada.");
  } catch (error) {
    $("#error-form").textContent = error.message;
  } finally {
    estado.guardandoTarea = false;
    boton.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Varias tareas a la vez: «-Tarea/WI/puntos» por línea
// ---------------------------------------------------------------------------

function renderModo() {
  const varias = estado.modo === "varias";
  for (const radio of document.querySelectorAll("#modo-form input")) radio.checked = radio.value === estado.modo;
  $("#solo-una").hidden = varias;
  $("#solo-una-arriba").hidden = varias;
  $("#bloque-lote").hidden = !varias;
  $("#f-descripcion").required = !varias;  // un campo oculto obligatorio bloquearía el envío
  if (estado.editando) return;  // el título y el botón ya dicen «Editar tarea»
  $("#titulo-form").textContent = varias ? "Nuevas tareas" : "Nueva tarea";
  renderLote();
}

/** ¿La revisión mostrada corresponde al texto actual y se puede agregar? */
const loteListo = () => Boolean(estado.lote && estado.lote.texto === $("#f-lote").value && !estado.lote.faltan.length);

function renderLote() {
  if (estado.modo !== "varias") {
    $("#btn-guardar").textContent = estado.editando ? "Guardar cambios" : "Agregar tarea";
    return;
  }
  const r = estado.lote;
  const vigente = r && r.texto === $("#f-lote").value;
  $("#btn-guardar").textContent = loteListo()
    ? `Agregar ${r.tareas.length} tarea${r.tareas.length === 1 ? "" : "s"}`
    : "Revisar";
  const caja = $("#vista-lote");
  if (!r) { caja.replaceChildren(); return; }
  const conTk = r.tareas.filter((x) => x.work_items.length).length;
  caja.replaceChildren(
    el("p", { class: "resumen-lote" },
      `${r.tareas.length} tarea(s): ${conTk} con work item (crean TK) y ${r.tareas.length - conTk} sin work item.`,
      vigente ? null : el("strong", {}, " El texto cambió: vuelve a revisar.")),
    r.aviso ? el("p", { class: "ayuda" }, r.aviso) : null,
    r.faltan.length ? el("p", { class: "error" },
      `No se encontraron en Azure DevOps (no existen o tu PAT no tiene acceso): ${r.faltan.join(", ")}. Corrige el número del work item.`) : null,
    el("ol", { class: "lista-lote" }, r.tareas.map((x) => el("li", {},
      el("span", { class: "desc-lote" }, x.descripcion),
      el("span", { class: "meta-lote" },
        x.work_items.length
          ? [...x.work_items.map((w) => {
              const info = r.wis[String(w)];
              const falta = r.faltan.includes(w);
              return el("span", { class: `ficha-wi${falta ? " falta" : ""}`, title: info ? `${info.tipo} · ${info.proyecto} · ${info.estado}` : "" },
                info ? `${w} · ${info.titulo}` : (falta ? `${w} · no encontrado` : `WI ${w}`));
            }),
            el("span", { class: "puntos-lote" }, `${x.puntos} pt${x.puntos === 1 ? "" : "s"}${x.puntos_defecto ? " (por defecto)" : ""}`)]
          : el("span", { class: "ayuda" }, "Sin work item: no crea TK"))))));
}

async function guardarLote() {
  if (estado.guardandoTarea) return;
  $("#error-form").textContent = "";
  const texto = $("#f-lote").value;
  const boton = $("#btn-guardar");
  estado.guardandoTarea = true;
  boton.disabled = true;
  try {
    if (!loteListo()) {
      // Primero se revisa: se ve cómo quedó cada tarea y si los work items existen.
      boton.textContent = "Revisando…";
      estado.lote = { texto, ...(await api("POST", "/api/tareas/lote/revisar", { texto })) };
      for (const [id, w] of Object.entries(estado.lote.wis)) estado.titulosWi.set(Number(id), w.titulo);
      return;
    }
    const f = formulario();
    const ids = estado.lote.tareas.flatMap((x) => x.work_items);
    const respuesta = await api("POST", "/api/tareas/lote", {
      texto,
      seccion: f.seccion.value,
      categoria: f.categoria.value.trim(),
      wi_titulos: Object.fromEntries(ids.filter((id) => estado.titulosWi.has(id)).map((id) => [id, estado.titulosWi.get(id)])),
    });
    for (const id of respuesta.nuevas || []) estado.seleccion.add(id);  // quedan marcadas, como una tarea nueva
    for (const id of respuesta.nuevas || []) {
      const t = respuesta.reporte.tareas.find((x) => x.id === id);
      if (t) mostrarTarea(t);
    }
    limpiarFormulario();
    aplicar(respuesta);
    aviso(respuesta.aviso || "Tareas agregadas.");
  } catch (error) {
    $("#error-form").textContent = error.message;
  } finally {
    estado.guardandoTarea = false;
    boton.disabled = false;
    renderLote();
  }
}

/** Lee un .txt o .docx (el servidor lo convierte en texto) y lo agrega al cuadro. */
async function adjuntarLote(archivo) {
  if (!archivo) return;
  $("#error-form").textContent = "";
  if (!/\.(txt|docx)$/i.test(archivo.name)) {
    $("#error-form").textContent = `«${archivo.name}» no es .txt ni .docx (Word). Si es .doc, guárdalo como .docx.`;
    return;
  }
  if (archivo.size > 1_000_000) {
    $("#error-form").textContent = `«${archivo.name}» pesa más de 1 MB.`;
    return;
  }
  const boton = $("#btn-archivo-lote");
  boton.disabled = true;
  try {
    const bytes = new Uint8Array(await archivo.arrayBuffer());
    let binario = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binario += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const { texto } = await api("POST", "/api/tareas/lote/archivo", { nombre: archivo.name, contenido: btoa(binario) });
    const cuadro = $("#f-lote");
    cuadro.value = cuadro.value.trim() ? `${cuadro.value.replace(/\s+$/, "")}\n${texto}` : texto;
    renderLote();
    aviso(`Se leyó «${archivo.name}». Pulsa Revisar.`);
    cuadro.focus();
  } catch (error) {
    $("#error-form").textContent = `${archivo.name}: ${error.message}`;
  } finally {
    boton.disabled = false;
    $("#f-archivo-lote").value = "";  // permite volver a adjuntar el mismo archivo
  }
}

async function eliminar(t) {
  if (!confirm(`¿Eliminar la tarea?\n\n${t.descripcion}`)) return;
  try {
    aplicar(await api("DELETE", `/api/tareas/${t.id}`));
    if (estado.editando === t.id) limpiarFormulario();
    aviso("Tarea eliminada.");
  } catch (error) {
    aviso(error.message, "error");
  }
}

function botonResultado(w, onclick) {
  return el("button", { type: "button", class: "resultado-wi", onclick },
    el("strong", {}, String(w.id)), ` ${w.titulo}`,
    el("small", {}, `${w.tipo} · ${w.proyecto} · ${w.estado}`));
}

async function buscarWi() {
  const texto = $("#wi-buscar").value.trim();
  if (!texto) return;
  const turno = ++estado.busquedaWi;
  const lista = $("#resultados-wi");
  lista.replaceChildren(el("li", { class: "ayuda" }, "Buscando en Azure DevOps…"));
  try {
    const { resultados } = await api("GET", `/api/buscar?q=${encodeURIComponent(texto)}`);
    if (turno !== estado.busquedaWi) return;  // llegó tarde: ya hay otra búsqueda (o se limpió el formulario)
    if (!resultados.length) {
      lista.replaceChildren(el("li", { class: "ayuda" }, `Sin resultados para «${texto}».`));
      return;
    }
    lista.replaceChildren(...resultados.map((w) => el("li", {}, botonResultado(w, () => agregarWi(w)))));
  } catch (error) {
    if (turno === estado.busquedaWi) lista.replaceChildren(el("li", { class: "error" }, error.message));
  }
}

function agregarWi(w) {
  estado.titulosWi.set(w.id, w.titulo);
  if (!estado.workItems.includes(w.id)) estado.workItems.push(w.id);
  $("#resultados-wi").replaceChildren();
  $("#wi-buscar").value = "";
  renderWis();
}

// ---------------------------------------------------------------------------
// Sesiones de Google y RIPOR (la de Google vence cada día en Linktic)
// ---------------------------------------------------------------------------

async function comprobarSesion(servicio) {
  try {
    estado.sesiones[servicio] = await api("GET", `/api/sesion/${servicio}`);
  } catch (error) {
    estado.sesiones[servicio] = { configurado: true, ok: false, detalle: error.message };
  }
  estado.sesionesRevisadas = Date.now();
  renderSesiones();
  if (estado.reporte) renderBarra();
}

function comprobarSesiones() {
  return Promise.all(Object.keys(SERVICIOS).map(comprobarSesion));
}

function renderSesiones() {
  const filas = Object.entries(SERVICIOS).map(([clave, s]) => {
    const x = estado.sesiones[clave];
    if (x && !x.configurado) return null;   // destino no configurado: nada que mostrar
    if (!x) return el("div", { class: "sesion comprobando" }, `${s.nombre}: comprobando la sesión…`);
    if (x.ok) return el("div", { class: "sesion ok" }, `✓ ${s.nombre}: sesión activa${x.cuenta ? ` (${x.cuenta})` : ""}`);
    const esperando = estado.esperandoSesion[clave];
    return el("div", { class: `sesion ${x.bloqueado ? "bloqueada" : "pendiente"}` },
      el("span", {}, `${x.bloqueado ? "–" : "⚠"} ${s.nombre}: ${x.detalle}`),
      x.bloqueado ? null : el("button", {
        type: "button",
        class: "primario",
        disabled: Boolean(esperando),
        onclick: () => iniciarSesion(clave),
      }, esperando ? "Esperando a que inicies sesión en el navegador…" : s.boton));
  }).filter(Boolean);
  const firma = filas.map((f) => f.textContent).join("|");
  if (firma === estado.firmaSesiones) return;
  estado.firmaSesiones = firma;
  $("#sesiones").replaceChildren(...filas);
}

/** Abre el inicio de sesión en el navegador y espera (hasta 5 min) a que quede lista. */
async function iniciarSesion(clave) {
  if (estado.esperandoSesion[clave]) return;
  estado.esperandoSesion[clave] = true;
  renderSesiones();
  try {
    aviso((await api("POST", `/api/sesion/${clave}`)).aviso);
  } catch (error) {
    estado.esperandoSesion[clave] = false;
    renderSesiones();
    return aviso(error.message, "error");
  }
  const limite = Date.now() + 5 * 60 * 1000;
  while (Date.now() < limite) {
    await new Promise((listo) => setTimeout(listo, 4000));
    await comprobarSesion(clave);
    if (estado.sesiones[clave]?.ok) {
      aviso(`Sesión de ${SERVICIOS[clave].nombre} lista.`);
      break;
    }
  }
  estado.esperandoSesion[clave] = false;
  renderSesiones();
}

/** Antes de usar Google o RIPOR: si ya se sabe que la sesión no sirve, avisa en vez de fallar. */
function sesionLista(clave, accion) {
  const x = estado.sesiones[clave];
  if (!x || !x.configurado || x.ok) return true;
  aviso(x.bloqueado
    ? `${SERVICIOS[clave].nombre} no está disponible en este equipo: ${accion}.`
    : `Inicia sesión en ${SERVICIOS[clave].nombre} con el botón de arriba: ${accion}.`, "error");
  return false;
}

// ---------------------------------------------------------------------------
// Subida y acciones generales
// ---------------------------------------------------------------------------

async function subir() {
  const ids = [...estado.seleccion];
  const destinos = DESTINOS.map(([, d]) => d).filter((d) => {
    const casilla = $(`#dest-${d}`);
    return casilla.checked && !casilla.disabled;
  });
  if (!ids.length) return aviso("Selecciona al menos una tarea.", "error");
  if (!destinos.length) return aviso("Elige al menos un destino.", "error");
  if (destinos.includes("drive") && !sesionLista("google", "o desmarca Drive")) return;
  if (destinos.includes("ripor") && !sesionLista("ripor", "desmarca RIPOR")) return;

  const simular = $("#simular").checked;
  if (!simular && estado.hoy && estado.reporte.fecha < estado.hoy
      && !confirm(`El reporte es del ${estado.reporte.fecha} (hoy es ${estado.hoy}): todo se publicará con esa fecha. `
        + "¿Seguro? (Si estás empezando el día, cancela y pulsa «Nuevo día».)")) return;
  if (!simular) {
    const partes = [];
    if (destinos.includes("devops")) partes.push("crear o completar TK en Azure DevOps");
    if (destinos.includes("drive")) partes.push("actualizar el documento de Drive");
    if (destinos.includes("chat")) partes.push("enviar un mensaje a Google Chat");
    if (destinos.includes("ripor")) {
      const dia = estado.reporte.ripor_fecha || estado.reporte.fecha;
      partes.push(`registrar el daily del ${dia} en RIPOR (no se puede editar ni borrar desde aquí)`);
    }
    if (!confirm(`Se va a ${partes.join(", ")} con ${ids.length} tarea(s). ¿Continuar?`)) return;
  }

  const boton = $("#btn-subir");
  boton.dataset.ocupado = "1";
  boton.disabled = true;
  boton.textContent = simular ? "Simulando…" : "Subiendo…";
  try {
    const respuesta = await api("POST", "/api/subir", { ids, destinos, simular });
    estado.subida = { ...respuesta.subida, simulada: simular };
    aplicar(respuesta);
    mostrarResumen(respuesta.subida, simular);
    // Si algo falló, puede ser una sesión que venció en el camino.
    const fallos = Object.values(respuesta.subida.tareas).some((r) => r.drive?.estado === "error" || r.ripor?.estado === "error");
    if (fallos) comprobarSesiones();
  } catch (error) {
    aviso(error.message, "error");
  } finally {
    delete boton.dataset.ocupado;
    boton.textContent = "Subir seleccionadas";
    renderBarra();
  }
}

function mostrarResumen(subida, simular) {
  const resultados = Object.values(subida.tareas);
  const lineas = DESTINOS.filter(([clave]) => resultados.some((r) => r[clave])).map(([clave, , nombre]) => {
    const conteo = {};
    for (const r of resultados) if (r[clave]) conteo[r[clave].estado] = (conteo[r[clave].estado] || 0) + 1;
    const texto = Object.entries(conteo).map(([e, n]) => `${ICONOS[e] || "?"} ${n} ${e}`).join(" · ");
    return el("p", {}, el("strong", {}, `${nombre}: `), texto);
  });
  if (subida.drive_url) {
    lineas.push(el("p", {}, el("strong", {}, "Documento: "),
      el("a", { href: enlaceSeguro(subida.drive_url), target: "_blank", rel: "noopener" }, "abrir en Google Drive")));
  }
  if (simular) lineas.unshift(el("p", {}, el("em", {}, "Simulación: no se publicó nada.")));
  $("#resumen").replaceChildren(...lineas);

  const detalle = $("#detalle-chat");
  detalle.hidden = !subida.mensaje_chat;
  $("#texto-chat").textContent = subida.mensaje_chat || "";
  detalle.open = simular;
  $("#panel-resultado").hidden = false;
  $("#panel-resultado").scrollIntoView({ behavior: "smooth", block: "nearest" });

  const errores = resultados.filter((r) => DESTINOS.some(([c]) => r[c]?.estado === "error")).length;
  aviso(errores ? `${errores} tarea(s) con errores; revisa el motivo en cada una.` : (simular ? "Simulación lista." : "Subida completada."),
    errores ? "error" : "info");
}

async function guardarCabecera(evento) {
  const campo = evento.target;
  if (!campo.name) return;
  let respuesta;
  try {
    const valor = campo.type === "number" ? numeroValido(campo, campo.parentElement.firstChild.textContent.trim())
      : campo.value;
    respuesta = await api("PUT", "/api/reporte", { [campo.name]: valor });
  } catch (error) {
    aviso(error.message, "error");
    respuesta = await api("GET", "/api/estado").catch(() => null);
  }
  if (respuesta) aplicar(respuesta);
}

async function accionGeneral(boton, url, mensajeConfirmacion, soltarEdicion = false) {
  if (mensajeConfirmacion && !confirm(mensajeConfirmacion)) return;
  const texto = boton.textContent;
  boton.disabled = true;
  boton.textContent = "Procesando…";
  try {
    const respuesta = await api("POST", url);
    estado.subida = null;
    if (soltarEdicion && estado.editando) limpiarFormulario();
    aplicar(respuesta);
    if (respuesta.aviso) aviso(respuesta.aviso);
  } catch (error) {
    aviso(error.message, "error");
  } finally {
    boton.disabled = false;
    boton.textContent = texto;
  }
}

/** «Nuevo día» con avisos: lo que se archiva sin publicar y los días no hábiles. */
async function nuevoDia(boton) {
  const ayer = estado.reporte.tareas.filter((t) => t.seccion === "ayer");
  const sinTk = ayer.filter((t) => t.crear_tk && t.work_items.some((wi) => !t.tks.some((x) => x.wi === wi))).length;
  const sinSubir = ayer.filter((t) => !t.resultado?.drive && !t.resultado?.chat).length;
  let mensaje = "Las tareas de «hoy» pasarán a «ayer» y las de «ayer» se archivarán en historial/.";
  if (sinTk || sinSubir) {
    mensaje += `\n\nOjo: de las de «ayer», ${sinTk} no tienen su TK creado y ${sinSubir} nunca se subieron a Drive ni a Chat.`;
  }
  if (!confirm(`${mensaje}\n\n¿Continuar?`)) return;
  const texto = boton.textContent;
  boton.disabled = true;
  boton.textContent = "Procesando…";
  try {
    let respuesta;
    try {
      respuesta = await api("POST", "/api/nuevo-dia", {});
    } catch (error) {
      // Sábado, domingo o festivo: el servidor pide confirmación explícita.
      if (!error.datos?.confirmar || !confirm(error.message)) throw error;
      respuesta = await api("POST", "/api/nuevo-dia", { forzar: true });
    }
    estado.subida = null;
    if (estado.editando) limpiarFormulario();
    aplicar(respuesta);
    if (respuesta.aviso) aviso(respuesta.aviso);
  } catch (error) {
    if (!error.datos?.confirmar) aviso(error.message, "error");
  } finally {
    boton.disabled = false;
    boton.textContent = texto;
  }
}

// La casilla de RIPOR recuerda la última elección en este navegador.
function recordarDestinoRipor() {
  const casilla = $("#dest-ripor");
  try { casilla.checked = localStorage.getItem("destino-ripor") === "1"; } catch { /* sin almacenamiento */ }
  casilla.addEventListener("change", () => {
    try { localStorage.setItem("destino-ripor", casilla.checked ? "1" : "0"); } catch { /* sin almacenamiento */ }
  });
}

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

function conectarEventos() {
  $("#form-tarea").addEventListener("submit", guardarTarea);
  $("#btn-cancelar").addEventListener("click", () => { limpiarFormulario(); render(); });
  $("#btn-buscar-wi").addEventListener("click", buscarWi);
  $("#wi-buscar").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) { e.preventDefault(); e.stopPropagation(); buscarWi(); }
  });
  $("#cabecera").addEventListener("change", guardarCabecera);
  $("#f-puntos").addEventListener("change", explicarPuntos);
  $("#modo-form").addEventListener("change", (e) => {
    if (estado.editando) limpiarFormulario();  // cambiar de modo cancela la edición
    estado.modo = e.target.value;
    renderModo();
    (estado.modo === "varias" ? $("#f-lote") : $("#f-descripcion")).focus();
  });
  $("#f-lote").addEventListener("input", renderLote);
  $("#btn-archivo-lote").addEventListener("click", () => $("#f-archivo-lote").click());
  $("#f-archivo-lote").addEventListener("change", (e) => adjuntarLote(e.target.files[0]));
  $("#f-lote").addEventListener("dragover", (e) => {
    if ([...e.dataTransfer.types].includes("Files")) { e.preventDefault(); e.currentTarget.classList.add("soltar"); }
  });
  $("#f-lote").addEventListener("dragleave", (e) => e.currentTarget.classList.remove("soltar"));
  $("#f-lote").addEventListener("drop", (e) => {
    e.currentTarget.classList.remove("soltar");
    if (!e.dataTransfer.files.length) return;  // texto arrastrado: lo maneja el navegador
    e.preventDefault();
    adjuntarLote(e.dataTransfer.files[0]);
  });
  // Ctrl+Enter (o Cmd+Enter en Mac) guarda la tarea desde cualquier campo del formulario.
  $("#form-tarea").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("#form-tarea").requestSubmit(); }
  });
  $("#btn-desplegar").addEventListener("click", () => plegarTodo(true));
  $("#btn-plegar").addEventListener("click", () => plegarTodo(false));
  $("#cabecera-ripor").addEventListener("change", cambioCabeceraRipor);
  $("#btn-ripor").addEventListener("click", botonCargarRipor);
  $("#sel-todas").addEventListener("change", (e) => marcar(estado.reporte.tareas.map((t) => t.id), e.target.checked));
  $("#btn-subir").addEventListener("click", subir);
  $("#btn-calendar").addEventListener("click", (e) => {
    if (!sesionLista("google", "Calendar la necesita")) return;
    accionGeneral(e.currentTarget, "/api/calendar").then(() => {
      if (!estado.sesiones.google?.ok) comprobarSesion("google");
    });
  });
  // La sesión de Google vence sin avisar: se revisa cada 15 min y al volver a la pestaña.
  setInterval(comprobarSesiones, 15 * 60 * 1000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    api("GET", "/api/estado").then(aplicar).catch(() => {});
    if (Date.now() - estado.sesionesRevisadas > 5 * 60 * 1000) comprobarSesiones();
  });
  $("#btn-nuevo-dia").addEventListener("click", (e) => nuevoDia(e.currentTarget));
  $("#btn-limpiar").addEventListener("click", (e) => accionGeneral(e.currentTarget, "/api/limpiar",
    "Se quitarán todas las tareas de la lista (queda una copia en historial/). Lo ya publicado en Azure, " +
    "Drive, Chat y RIPOR no cambia. ¿Continuar?", true));
  $("#btn-cerrar-app").addEventListener("click", async () => {
    if (!confirm("¿Cerrar el programa? Se vuelve a abrir con el acceso directo «Reporte diario».")) return;
    try { await api("POST", "/api/apagar"); } catch { /* ya estaba cerrado */ }
    document.body.replaceChildren(el("main", { class: "cerrado" },
      el("h1", {}, "Reporte diario cerrado"), el("p", {}, "Ya puedes cerrar esta ventana.")));
    window.close();
  });
  recordarDestinoRipor();
}

cargarAbiertos();
conectarEventos();
renderModo();
renderSesiones();
api("GET", "/api/estado").then(aplicar).catch((error) => aviso(error.message, "error"));
comprobarSesiones();
