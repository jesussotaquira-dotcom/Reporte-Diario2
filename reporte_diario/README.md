# Reporte diario automático

Página web local para armar el **daily** del equipo y publicarlo, con un clic, en cuatro destinos:

| Destino | Qué hace |
|---|---|
| **Azure DevOps** | Crea un **TK por cada tarea**, como hijo de la HU o Feature con la que se relaciona |
| **Google Drive** | Crea o actualiza el Google Doc `Reporte diario - AAAA-MM-DD`, con enlaces a los TK |
| **Google Chat** | Envía el daily al espacio del equipo, con su formato |
| **RIPOR** | Registra tus horas del día: un registro con el texto del daily y el enlace del Doc de Drive |

Funciona en **Windows y Mac**. El daily sale con el formato que ya usa el equipo:

```
Buen día,
¿Qué hice ayer?
Participación en la sesión de Daily Emisión.
Revisión y respuesta a las inquietudes del equipo de QA relacionadas con la HU309 de Novedades.
¿Qué voy a hacer hoy?
Seguimiento a las dudas escaladas al líder funcional sobre las HU533 y HU528…
¿Tengo algún blocker / stopper? No
```

Cómo encaja todo:

```
 Página web (servidor.py, http://127.0.0.1:8765)
        │  guarda las tareas en reporte_hoy.json
        ▼
 reporte_diario.py ──┬──► Azure DevOps  (REST + PAT) ─────────────► un TK por tarea y work item
                     ├──► Apps Script   (token de gcloud) ─────────► Google Doc en Drive
                     │                                  └──────────► reuniones de Calendar
                     ├──► Google Chat   (webhook) ─────────────────► mensaje en el espacio
                     └──► RIPOR         (conector ripor-mcp.mjs) ──► registro de horas del día
```

**Contenido**

1. [Antes de empezar: la terminal en Windows y en Mac](#antes-de-empezar-la-terminal-en-windows-y-en-mac)
2. [Instalación paso a paso](#instalación-paso-a-paso-una-sola-vez)
3. [Uso diario](#uso-diario)
4. [Recorrido por la página](#recorrido-por-la-página)
5. [Qué se publica en cada destino](#qué-se-publica-en-cada-destino)
6. [Uso desde la consola](#uso-desde-la-consola-opcional)
7. [Archivos del proyecto](#archivos-del-proyecto)
8. [Problemas frecuentes](#problemas-frecuentes)

---

## Antes de empezar: la terminal en Windows y en Mac

Varios pasos se hacen en una terminal. Cuando un comando cambia según el sistema, aparece en dos versiones.

| | Windows | Mac |
|---|---|---|
| Abrir la terminal | Tecla Windows → escribe `cmd` (o `PowerShell`) → Enter | `Cmd + Espacio` → escribe `Terminal` → Enter |
| Ejecutar Python | `python` | `python3` |
| Carpeta personal | `%USERPROFILE%` en cmd, `$HOME` en PowerShell (p. ej. `C:\Users\tu.usuario`) | `~` (p. ej. `/Users/tu.usuario`) |
| Separador de rutas | `\` | `/` |
| Copiar un archivo | `copy origen destino` | `cp origen destino` |
| Editar un archivo de texto | `notepad archivo` | `open -e archivo` |

En este documento, `<carpeta del proyecto>` es donde quedó el proyecto al descargarlo (la que tiene este README), por ejemplo `C:\Users\tu.usuario\reporte_diario` o `~/reporte_diario`.

---

## Instalación paso a paso (una sola vez)

Cada persona hace la instalación en su propio computador, con **sus** credenciales: su PAT de Azure, su Apps Script, su sesión de Google y su sesión de RIPOR. Nada de eso se comparte ni va en el repositorio.

| Paso | Qué haces | ¿Obligatorio? |
|---|---|---|
| 0. Obtener el proyecto | Clonar el repositorio o descargarlo | Sí |
| 1. Python | Instalar Python 3.10+ | Sí |
| 2. `.env` | Crear tu archivo de configuración | Sí |
| 3. Azure DevOps | Tu PAT con escritura | Para crear TK |
| 4. Apps Script | Tu receptor de Drive y Calendar | Para Drive y Calendar |
| 5. Google Cloud CLI | Tu sesión de Google | Para Drive y Calendar |
| 6. Google Chat | El webhook del espacio del equipo | Para Chat |
| 7. RIPOR | El conector oficial y tu sesión | Para RIPOR |

Puedes hacer solo los pasos de los destinos que vayas a usar: los demás aparecen desactivados en la página.

### Paso 0 — Obtener el proyecto

**Con git** (recomendado, así luego actualizas con `git pull`):

```
cd <la carpeta donde lo quieras, p. ej. tu carpeta personal>
git clone https://github.com/jesussotaquira-dotcom/reporte-diario.git
cd reporte-diario
```

**Sin git:** en la página del repositorio, **Code → Download ZIP**, descomprímelo donde quieras y entra a la carpeta.

Para **actualizar** a una versión nueva: `git pull` dentro de la carpeta (tus archivos `.env`, `.llave_local`, `reporte_hoy.json`, `tks_creados.json` e `historial/` no se tocan, porque no están en el repositorio). Si lo bajaste en ZIP, descarga el nuevo y copia esos archivos a la carpeta nueva.

### Paso 1 — Python y librerías

1. Necesitas **Python 3.10 o superior**. Compruébalo:

   | Windows | Mac |
   |---|---|
   | `python --version` | `python3 --version` |

   Si no está instalado:
   - **Windows:** descárgalo de <https://www.python.org/downloads/> y, en el instalador, marca **Add python.exe to PATH**.
   - **Mac:** descárgalo de <https://www.python.org/downloads/>, o con Homebrew: `brew install python`.

2. **Abre el lanzador** de la carpeta del proyecto con doble clic:
   - **Windows:** `iniciar.bat`
   - **Mac:** `iniciar.command`. La primera vez, macOS puede bloquearlo por venir de internet: haz clic derecho → **Abrir** → **Abrir**. Si dice que no tiene permiso, en la terminal ejecuta una vez `chmod +x iniciar.command`.

   La primera vez, el lanzador hace lo siguiente:
   - prepara un entorno de Python propio del proyecto (carpeta `.venv`, para no mezclarse con otros programas);
   - instala las dos librerías que usa (`requests` y `python-dotenv`);
   - crea tu `.env` a partir de la plantilla y lo abre para que lo completes (paso 2).

   La página usa solo la biblioteca estándar de Python y HTML/CSS/JS sin librerías.

   *Sin lanzador* (si prefieres la terminal): `python -m pip install -r requirements.txt` en Windows o `python3 -m pip install -r requirements.txt` en Mac, dentro de la carpeta del proyecto.

### Paso 2 — Archivo `.env`

El `.env` guarda tu configuración y tus credenciales. El lanzador lo crea solo la primera vez y lo abre. Si prefieres hacerlo a mano:

| Windows | Mac |
|---|---|
| `copy .env.example .env`<br>`notepad .env` | `cp .env.example .env`<br>`open -e .env` |

Lo que dejes vacío queda desactivado en la página. Puedes empezar solo con Azure, por ejemplo, y agregar los demás destinos después. Pon tu nombre en `REPORTE_AUTOR` y tu correo en `ADO_ASSIGNED_TO`.

Los pasos siguientes dicen qué poner en cada variable. **No compartas el `.env` ni lo subas a git** (el `.gitignore` ya lo excluye). En Mac, déjalo legible solo para ti: `chmod 600 .env`.

### Paso 3 — Azure DevOps: PAT con escritura

1. En <https://dev.azure.com/Linktic>, abre el ícono de usuario (arriba a la derecha) → **Personal access tokens** → **+ New Token**.
2. Ponle un nombre y una fecha de vencimiento. En **Scopes** elige *Custom defined* → **Work Items: Read & write**.
3. Pulsa **Create** y copia el token (Azure lo muestra una sola vez).
4. En el `.env`:

   ```ini
   ADO_ORG_URL=https://dev.azure.com/Linktic
   ADO_PAT=<el token>
   ADO_ASSIGNED_TO=tu.correo@linktic.com
   # Opcionales (estos son los valores por defecto):
   ADO_WORK_ITEM_TYPE=TK
   ADO_TK_STATE=Closed            # Closed | Active | New
   ADO_TK_TIPO=Gestión
   ADO_TK_HORA_INICIO=07:30
   ADO_TK_HORA_FIN=17:00
   ADO_TK_PUNTOS_DEFECTO=1
   # Guía de puntos que se muestra en la página (ver «Puntos de historia»):
   # ADO_PUNTOS_GUIA=1=Muy pequeña: unas horas;2=Pequeña: hasta 1 día;3=Mediana: 1 a 2 días
   ```

Cuando el PAT venza, crea otro y reemplázalo en el `.env`.

### Paso 4 — Google Drive y Calendar: Apps Script

Con la cuenta de Linktic no se pueden crear proyectos de Google Cloud ni aplicaciones web públicas. Por eso Drive y Calendar pasan por un Apps Script de tu cuenta, al que Python llama con el token de `gcloud` (paso 5). Todo esto se hace en el navegador, igual en Windows y en Mac.

**4.1 Crear el script.** En <https://script.google.com> → **Nuevo proyecto** → nómbralo `Reporte diario - receptor` → borra lo que trae, pega el contenido de `apps_script/Code.gs` → guarda (💾).

**4.2 Servicio Drive API.** En el panel izquierdo, **Servicios (+)** → **Drive API** (v3) → **Agregar**.

**4.3 Propiedades.** Genera una clave secreta en la terminal:

| Windows | Mac |
|---|---|
| `python -c "import secrets; print(secrets.token_urlsafe(32))"` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |

En **⚙ Configuración del proyecto → Propiedades del script** crea:

| Propiedad | Valor |
|---|---|
| `SECRETO` | La clave generada |
| `CARPETA_ID` | El ID de la carpeta de Drive donde irán los Docs (el final de `https://drive.google.com/drive/folders/<ID>`) |

**4.4 Autorizar.** En el editor, elige la función **`probarConfiguracion`** → **▶ Ejecutar** → **Revisar permisos** → tu cuenta → **Permitir** (si dice "Google no verificó esta app": *Configuración avanzada → Ir a…*). El registro de ejecución debe mostrar `OK. Carpeta destino…` y `OK. Calendario…`.

**4.5 Publicar.** **Implementar → Nueva implementación** → tipo **Aplicación web**:

- *Ejecutar como:* **Yo**
- *Quién tiene acceso:* **Cualquier usuario de Linktic**

Pulsa **Implementar** y copia la URL (forma `https://script.google.com/a/macros/linktic.com/s/.../exec`).

**4.6 En el `.env`:**

```ini
GDRIVE_APPS_SCRIPT_URL=https://script.google.com/a/macros/linktic.com/s/.../exec
GDRIVE_APPS_SCRIPT_SECRET=<la misma clave de SECRETO>
GDRIVE_FILE_PREFIX=Reporte diario
```

**4.7 Cuando cambie `Code.gs`** (por ejemplo, para activar Calendar):

1. Pega el contenido nuevo de `apps_script/Code.gs` y guarda.
2. Ejecuta otra vez **`probarConfiguracion`** y acepta el permiso nuevo de **Calendar**.
3. **Implementar → Gestionar implementaciones** → ✏ → **Versión: Nueva versión** → **Implementar**. La URL no cambia.

### Paso 5 — Google Cloud CLI (la sesión de Google)

1. **Instalar Google Cloud CLI:**
   - **Windows:** instalador de <https://cloud.google.com/sdk/docs/install>. Si al final ofrece `gcloud init` y crear un proyecto, responde **n**: no hace falta proyecto.
   - **Mac:** con Homebrew, `brew install --cask google-cloud-sdk`. También puedes usar el paquete de <https://cloud.google.com/sdk/docs/install> y ejecutar `./google-cloud-sdk/install.sh`. Tampoco hace falta `gcloud init`.
2. **Iniciar sesión por primera vez.** Lo más fácil es abrir la página (ver [Uso diario](#uso-diario)) y pulsar **Iniciar sesión en Google** en la barra de sesiones. También puedes hacerlo en una terminal nueva, en **una sola línea** (igual en Windows y en Mac):

   ```
   gcloud auth login --enable-gdrive-access
   ```

3. En el navegador, entra con la **misma cuenta dueña del Apps Script** y acepta el permiso *"Ver, modificar, crear y eliminar archivos de Google Drive"*. Sin ese permiso, Drive responde 401.

Linktic cierra esta sesión cada ~16 horas: cuando la barra de sesiones lo indique, vuelve a pulsar el botón. La página busca `gcloud` en el PATH y en las rutas donde lo dejan los instaladores. Si está en otra parte, define `GCLOUD_PATH` en el `.env`. Para retirar el permiso: `gcloud auth revoke`.

### Paso 6 — Google Chat: webhook

1. En el espacio de Chat del equipo: nombre del espacio → **Apps e integraciones** → **Webhooks** → **Agregar webhook**.
2. Ponle un nombre (por ejemplo, *Reporte diario*) y copia la URL. PONER NOMBRE DE LA PERSONA A REPORTAR
3. En el `.env`:

   ```ini
   GCHAT_WEBHOOK_URL=https://chat.googleapis.com/v1/spaces/AAAA.../messages?key=...&token=...
   ```

### Paso 7 — RIPOR (reporte de horas)

> En algunos equipos de Linktic **la política de la empresa bloquea Node.js** ("Acceso denegado"). Si te pasa, pide a TI que lo habilite. Mientras tanto, la casilla RIPOR aparece desactivada y lo demás funciona normal.

RIPOR se conecta con su conector oficial, `ripor-mcp.mjs`, que aplica las mismas reglas que la web de RIPOR. Necesita **Node.js 22 o superior**:
- **Windows:** <https://nodejs.org>.
- **Mac:** <https://nodejs.org> o `brew install node`.

1. **Descargar el conector a tu carpeta personal:**

   | Windows (PowerShell) | Mac |
   |---|---|
   | `Invoke-WebRequest https://ripor.co/descargas/ripor-mcp.mjs -UseBasicParsing -OutFile "$HOME\ripor-mcp.mjs"` | `curl -fsSL https://ripor.co/descargas/ripor-mcp.mjs -o ~/ripor-mcp.mjs` |

2. **Verificar la descarga.** Para la versión 0.4.0 debe dar `7430f3dc5ea1e7881240fe1c9b87145d74889000145e7d15135c60f72a499bed` (Windows lo muestra en mayúsculas; es el mismo valor):

   | Windows (PowerShell) | Mac |
   |---|---|
   | `Get-FileHash "$HOME\ripor-mcp.mjs" -Algorithm SHA256` | `shasum -a 256 ~/ripor-mcp.mjs` |

3. **Iniciar sesión.** En la página, pulsa **Iniciar sesión en RIPOR** en la barra de sesiones. También puedes hacerlo en la terminal:

   | Windows (cmd) | Windows (PowerShell) | Mac |
   |---|---|---|
   | `node "%USERPROFILE%\ripor-mcp.mjs" login` | `node "$HOME\ripor-mcp.mjs" login` | `node ~/ripor-mcp.mjs login` |

   Se abre el navegador: entra con tu cuenta de Google de Linktic. Para comprobarlo, cambia `login` por `estado`: debe decir *"RIPOR te reconoce como …"*.

4. **Configurar en la página:** en *Datos del reporte → RIPOR*, pulsa **Cargar proyectos de RIPOR**. Quedan elegidos solos tu proyecto vigente y su «Tarea personalizada». Revisa el medio de transporte.

5. **Primera prueba:** con el Doc del día ya subido a Drive, marca las tareas del daily, deja solo el destino RIPOR y activa **Simular**. Si sale ◌, desmarca Simular y sube.

En el `.env`:

```ini
RIPOR_TRANSPORTE=moto_particular   # medio predeterminado (opciones en .env.example)
# Solo si el conector o Node no están en la ruta por defecto:
# RIPOR_MCP_PATH=C:\Users\tu.usuario\ripor-mcp.mjs        (Mac: /Users/tu.usuario/ripor-mcp.mjs)
# RIPOR_NODE_PATH=C:\Program Files\nodejs\node.exe         (Mac: /opt/homebrew/bin/node)
```

(La guía de RIPOR también ofrece `claude mcp add ripor …`. Eso solo sirve para que Claude use RIPOR directamente; este proyecto no lo necesita.)

---

## Uso diario

Una vez instalado, cada día:

1. **Abre la página** con doble clic en `iniciar.bat` (Windows) o `iniciar.command` (Mac). También puedes usar la terminal:

   | Windows | Mac |
   |---|---|
   | `cd <carpeta del proyecto>`<br>`python servidor.py` | `cd <carpeta del proyecto>`<br>`python3 servidor.py` |

   Se abre el navegador con un enlace del tipo `http://127.0.0.1:8765/?k=…`, que la terminal también muestra. Ese enlace lleva tu **llave local**: la página la guarda en el navegador y desde entonces basta con <http://127.0.0.1:8765>. Si la página dice *«Abre la página desde el enlace que muestra la consola»*, copia el enlace de la terminal. **Deja abierta esa terminal** mientras uses la página; `Ctrl+C` la cierra (igual en Mac).

2. **Revisa la barra de sesiones** (debajo del título). Si dice *⚠ Google…*, pulsa **Iniciar sesión en Google**, entra con tu cuenta de Linktic en el navegador y espera a que la barra pase a *✓ sesión activa*. Linktic pide esto más o menos una vez al día.
3. **Nuevo día** (arriba a la derecha): lo que estaba en «¿Qué voy a hacer hoy?» pasa a «¿Qué hice ayer?», lo anterior se archiva en `historial/` y el blocker vuelve a «No».
4. **Importar reuniones de Calendar** (opcional): trae las reuniones de ayer y de hoy como tareas.
5. **Agrega o ajusta tareas** con el formulario. Debajo de cada reunión puedes pulsar **+ Relacionar work item** para asociarla a una HU o Feature sin abrir el formulario.
6. **Marca las tareas**, elige los destinos y pulsa **Subir seleccionadas**. Con **Simular** marcado ves todo lo que haría, sin publicar nada; úsalo si tienes dudas.
7. Cada tarea muestra el resultado por destino: ✓ subida, ✗ error (con el motivo), – no aplica, ◌ simulación.

---

## Recorrido por la página

### Barra superior

| Botón | Qué hace |
|---|---|
| **Importar reuniones de Calendar** | Trae tus reuniones de «ayer» y de «hoy» del calendario principal (ver [Reuniones de Calendar](#reuniones-de-calendar)) |
| **Nuevo día** | Empieza el día: «hoy» pasa a «ayer» y lo de ayer se archiva en `historial/reporte_<fecha>.json` (si ya existe, se guarda como `_2`, `_3`…; nunca se pisa). Solo funciona si el reporte es de un día anterior. Antes de confirmar avisa cuántas tareas de «ayer» se archivarían sin su TK o sin haberse subido. En **sábado, domingo o festivo** pide confirmación aparte |
| **Limpiar tareas** | Deja la lista vacía (pide confirmación). Antes guarda una copia en `historial/limpieza_<fecha>_<hora>.json`. No toca nada de lo ya publicado |

### Aviso de «Nuevo día»

Si el reporte es de un día anterior a hoy (por ejemplo, abriste la página y olvidaste pulsar *Nuevo día*), aparece arriba un aviso amarillo con el botón **Nuevo día**. Si de todas formas subes, la página pide confirmar, porque todo saldría con la fecha vieja.

### Barra de sesiones

Muestra si las sesiones que necesitan los destinos están listas:

| Mensaje | Qué significa | Qué hacer |
|---|---|---|
| ✓ Google (Drive y Calendar): sesión activa (tu correo) | Todo bien | Nada |
| ⚠ Google…: Tu sesión de Google venció | Linktic cierra la sesión de gcloud cada ~16 h | **Iniciar sesión en Google** → entra en el navegador. La página detecta sola cuando terminas |
| ⚠ RIPOR: No hay sesión de RIPOR | El conector no ha iniciado sesión | **Iniciar sesión en RIPOR** |
| – RIPOR: No se pudo ejecutar Node.js… | Node.js no está instalado o la empresa lo bloquea | Instalarlo o pedir a TI que lo habilite |

Las sesiones se revisan al abrir la página, cada 15 minutos, al volver a la pestaña y cuando una subida falla. Si la sesión de Google no está lista, la página avisa antes de subir a Drive o de importar de Calendar, en vez de dejar que falle.

### Datos del reporte

| Campo | Para qué sirve |
|---|---|
| Fecha (hoy) | Día del reporte. Los TK de las tareas de «hoy» llevan esta fecha |
| Fecha de «ayer» | Día de las tareas de «ayer» (por defecto el día hábil anterior, sin sábados, domingos ni **festivos de Colombia**; con *Nuevo día*, el último día reportado). Debe ser anterior a la fecha del reporte |
| Sprint | `16` → etiqueta `S16` e iteración `Sprint 16` en los TK |
| Tipo de TK por defecto | `Gestión` o `Producto`, para las tareas que no digan otro |
| Autor | Aparece en el Doc de Drive |
| Saludo / Blocker | Primera y última línea del daily |

**Bloque RIPOR** (solo aparece si el conector está instalado). Equivale a llenar a mano el formulario *Registrar* de RIPOR:

| Campo | Por defecto | Para qué sirve |
|---|---|---|
| Día a reportar | **Hoy** (la fecha del reporte) | Elige otro día si te faltó reportarlo, por ejemplo el de ayer. Después de registrarlo vuelve solo a «hoy» |
| Proyecto | **Automático**: tu único proyecto vigente en la fecha (se vuelve a calcular cada vez, así que sigue funcionando si cambias de proyecto) | Si tienes varios, elige uno. **Cargar proyectos de RIPOR** llena las listas y muestra cuál se usaría en automático |
| Tarea | **Automático**: «Tarea personalizada» | Otra tarea del proyecto, si hace falta |
| Horas dedicadas | **Automático**: 8,5 (lun–jue) / 8 (vie), menos lo ya reportado ese día | Escribe otro número para forzarlo **solo en el siguiente registro**; después de registrar (o con *Nuevo día*) vuelve a automático |
| Medio de transporte | El de `RIPOR_TRANSPORTE` en el `.env` | Queda guardado hasta que lo cambies |
| Evidencia | **Automático**: el Doc de Drive de ese día | Si la página no conoce el Doc de ese día, pega aquí su enlace (ábrelo en Drive y copia la dirección) |

Encima de la ayuda aparece qué día se registrará, si falta el enlace del Doc o, si ya se hizo, *✓ El daily del … ya se registró en RIPOR*.

Todo se guarda solo al cambiarlo.

### Formulario de tarea

| Campo | Obligatorio | Uso |
|---|---|---|
| Sección | Sí | Pestañas «¿Qué hice ayer?» / «¿Qué voy a hacer hoy?». Se mantiene al agregar varias tareas seguidas |
| Descripción | Sí | El texto exacto que va en el daily |
| Categoría | No | Agrupa las tareas en la lista (Reuniones, Refinamiento, QA… o la que escribas) |
| Work items relacionados | No | Busca `HU533` o el número del work item y elige el correcto. **Cada tarea crea su propio TK en cada work item que relaciones.** Sin work item, la tarea no crea TK (va solo a Drive, Chat y RIPOR) |
| Inicio / Fin / horas | No | Fecha de Inicio y Fecha Fin del TK. «Horas» es la alternativa a «fin». Por defecto 07:30–17:00 |

Cuando la tarea tiene al menos un work item aparece el bloque **Datos del TK**:

| Campo | Uso |
|---|---|
| Puntos de historia | Cada opción dice qué significa (ver la guía abajo). Por defecto, los puntos del work item; si no tiene o tiene unos que el TK no acepta, 1 |
| Estado | Por defecto **Cerrado**. Elige *Activo (abierto)* si la tarea sigue en curso, o *Nuevo* |
| Más opciones → Nombre corto | Por defecto, la descripción. El título del TK queda `<título del work item>_<nombre corto>` |
| Más opciones → Tipo | Por defecto, el tipo del reporte |
| Más opciones → Crear TK | Desmárcalo para que una tarea con work item no cree TK |

### Varias tareas a la vez

Arriba del formulario, la pestaña **Varias a la vez** cambia la descripción, los work items y las horas por un cuadro de texto. Escribe una tarea por línea con este formato:

```text
-Tarea/WI/puntos
```

```text
-Informe semanal proceso Migración/346364/1
-Revisión de la HU309 con QA/346301
-Participación en la Daily del equipo
```

| Parte | Obligatoria | Uso |
|---|---|---|
| `-Tarea` | Sí | La descripción, tal como va en el daily. Puede llevar `/` (p. ej. `Cotización/Solicitud`): solo se toman como WI y puntos los tramos finales que son números |
| `/WI` | No | Número del work item donde se crea el TK. Varios: `/346364,482680`. **Sin WI, la tarea no crea TK** (va solo a Drive, Chat y RIPOR) |
| `/puntos` | No | 1, 2, 3, 5, 8 o 13. **Si hay WI y no hay puntos, el TK lleva 1** |

- **Desde un archivo:** pulsa **Adjuntar archivo (.txt o Word)** o arrastra el archivo al cuadro. Sirven `.txt` y `.docx` de hasta 1 MB con el mismo formato, una tarea por línea o párrafo. El texto del archivo se agrega al cuadro (no reemplaza lo que ya hay) y después se revisa y agrega igual que si lo hubieras escrito, así que puedes corregirlo antes. En Word sirven también las listas con viñetas: cada viñeta cuenta como una tarea aunque no tenga el guion. Las viñetas `–`, `•` o `*` al inicio se toman como el guion. Un `.doc` antiguo hay que guardarlo antes como `.docx`.
- Se pueden escribir seguidas en una línea, separadas por un espacio y un guion pegado al texto: `-Tarea uno/346364 -Tarea dos`. Un guion entre palabras (`HU-282`) o con espacios a ambos lados (`Cotización - Solicitud`) no parte la tarea.
- Todas toman la **sección** y la **categoría** del formulario; el estado y el tipo del TK son los de por defecto (se pueden cambiar después editando cada tarea).
- El botón primero dice **Revisar**: muestra cómo quedó cada tarea, el título de cada work item según Azure DevOps y los puntos. Si un work item no se encuentra, lo marca en rojo y no deja agregar. Si el texto está bien, el botón pasa a **Agregar N tareas**. Si cambias el texto, hay que volver a revisar.
- Si una línea tiene un error (puntos inválidos, puntos sin WI, falta la descripción), no se agrega ninguna y el mensaje dice qué línea corregir.
- Después cada tarea es una tarea normal de la lista: crea su propio TK al subir y se puede editar o eliminar una por una.

**Guía de puntos de historia** (la que aparece en la página; cada equipo puede ajustarla con `ADO_PUNTOS_GUIA` en el `.env`):

| Puntos | Significado |
|---|---|
| 1 | Muy pequeña: unas horas |
| 2 | Pequeña: hasta 1 día |
| 3 | Mediana: 1 a 2 días |
| 5 | Grande: 2 a 3 días |
| 8 | Muy grande: 3 a 5 días |
| 13 | Enorme: más de una semana (mejor dividirla) |

**Atajo:** `Ctrl + Enter` (en Mac también `Cmd + Enter`) guarda la tarea desde cualquier campo.

> **Ojo con los números de HU:** un mismo número existe en varios productos (HU309 está en SALUD, AP, VI…; HU282 tiene cinco work items). Por eso la búsqueda muestra todos y **tú eliges** el correcto; nunca se vinculan solos.

### Lista de tareas

Las tareas se agrupan en barras desplegables para que la lista no se alargue: primero por **categoría** (Reuniones, QA…) y, dentro de cada una, en **Ayer** y **Hoy**.
- Todas empiezan plegadas. Pulsa el nombre de una barra para abrirla o cerrarla; la página recuerda cuáles dejaste abiertas.
- **Desplegar todo** / **Plegar todo** (junto al título *Tareas*) abren o cierran todas de una vez.
- La casilla de cada barra marca todas sus tareas, y la barra muestra cuántas hay y cuántas están seleccionadas.
- Al agregar o editar una tarea, su grupo se abre solo.

Cada tarea muestra su sección (AYER/HOY), la descripción, sus work items como fichas con el título (la **×** los quita) y un resumen: estado con que quedará el TK, tipo, horario, puntos y enlaces a los TK ya creados.

- **Editar** carga la tarea en el formulario. **Eliminar** la borra de la lista (no borra nada ya publicado).
- **+ Relacionar work item** aparece debajo de las **reuniones**, ya sean importadas de Calendar o de la categoría *Reuniones*. Abre un buscador ahí mismo: escribe `HU533` o el número, pulsa *Buscar* (o Enter) y elige el resultado. Queda guardado al instante; es lo mismo que hacerlo desde *Editar*, pero más rápido.

### Selección y subida

La barra de selección queda **fija arriba** al bajar por la lista, para tenerla siempre a mano.

- **Individual:** la casilla de cada tarea.
- **Por grupo:** la casilla de una barra (categoría, o Ayer/Hoy dentro de ella).
- **Total:** la casilla **Todas**.
- El contador dice cuántas tareas hay seleccionadas y **cuántos TK nuevos** se crearían.
- **Destinos:** Azure, Drive, Chat y RIPOR se activan por separado. Los que no estén disponibles aparecen desactivados; pasa el mouse por encima para ver el motivo. La casilla de RIPOR recuerda tu última elección.
- **Simular:** muestra lo que haría cada destino sin publicar nada (en RIPOR, valida con sus reglas sin guardar).
- **Subir seleccionadas:** pide confirmación y publica. Al terminar, la página baja a **Resultado de la subida**, con el conteo por destino y el enlace al Doc.

---

## Qué se publica en cada destino

### Azure DevOps (TK)

| Situación | Resultado |
|---|---|
| Tarea sin work item | No crea TK |
| Tarea con un work item | Un TK hijo de ese work item |
| Tarea con varios work items | Un TK en cada uno |
| **Varias tareas con el mismo work item** | **Cada tarea tiene su propio TK**, aunque compartan work item y día |
| Vuelves a subir una tarea ya subida, sin cambios | No pasa nada: el TK ya existe |
| **Editas una tarea ya subida** (descripción, nombre corto, puntos, tipo, estado u horario) y la vuelves a subir | Se **actualiza su TK** en Azure con los cambios; no se crea otro |

Campos del TK (siguen la convención del equipo, por ejemplo el TK 559578):

| Campo | Valor |
|---|---|
| Tipo | `TK` |
| Title | `<título del work item>_<nombre corto>` |
| Description | La descripción de la tarea |
| State | **`Closed`** por defecto (`ADO_TK_STATE`); cada tarea puede pedir *Active* o *New*. Si el proceso no permite pasar directo de New a Closed, pasa antes por Active |
| Iteration / Tags | `…\Sprint <n>` del proyecto y `S<n>` |
| Tipo_TK | Gestión o Producto |
| Puntos de Historia | Los de la tarea, los del work item o 1 |
| Fecha de Inicio / Fin | Fecha del TK + horario |
| Assigned To / Asignado | `ADO_ASSIGNED_TO` |
| Area / Padre | Los del work item |

- El TK se crea en el proyecto del work item, así que puedes mezclar `355- Positiva Core` y `416 Positiva CORE 2026`.
- `tks_creados.json` registra cada TK por **fecha + work item + tarea**, y también reconoce los TK creados con versiones anteriores. Si una tarea ya tiene TK en un work item, no se crea otro aunque le cambies la sección o la fecha.
- Si Azure no responde a tiempo al crear un TK, la tarea queda en ✗ con el aviso *«no se sabe si creó el TK»*. Al volver a subirla, primero se busca en Azure si el TK quedó creado, y solo si no está se crea.
- El tipo TK **no tiene estado Removed** y tu usuario **no puede eliminar work items**. Para retirar un TK creado por error, ciérralo con un comentario y borra su línea de `tks_creados.json`.

### Google Drive

El Doc del día es **acumulativo**: incluye lo seleccionado más lo que ya se había subido a Drive ese día, así una subida parcial no lo recorta. Lo publicado se conserva aunque uses **Limpiar tareas**; una tarea quitada con **Eliminar** sí sale del Doc en la siguiente subida. Siempre se guarda además una copia local en `salidas/`.

> La versión actual de `apps_script/Code.gs` evita Docs duplicados si dos subidas coinciden y solo acepta el nombre del Doc del día y rangos de Calendar de hasta 10 días. Para tenerla, publícala con el paso 4.7 (recomendado, no obligatorio).

### Google Chat

Envía **un mensaje nuevo** solo con las tareas seleccionadas. Si subes dos veces, llegan dos mensajes.

### RIPOR

Se registra **un solo reporte por día**, como se hace a mano en *Registrar* de RIPOR:

| Campo de RIPOR | Lo que se envía |
|---|---|
| Día | La fecha del reporte («hoy»), o el día que elijas en «Día a reportar» |
| Proyecto | El elegido o, si no, tu único proyecto vigente |
| Tarea | La elegida o, si no, «Tarea personalizada» |
| Horas dedicadas | Las indicadas o, si no, 8,5 (lun–jue) / 8 (vie), descontando lo ya reportado ese día |
| Descripción | **El daily completo de ese día** (saludo, ¿Qué hice ayer?, ¿Qué voy a hacer hoy?, blocker), con todas las tareas del reporte, no solo las seleccionadas. Para un día anterior se usa el reporte archivado en `historial/`; si no existe, las tareas seleccionadas |
| Evidencia | **El enlace del Doc de ese día en Drive** (el archivo, no la carpeta). Para hoy, hay que subir a Drive antes o en la misma subida. Para otro día, la página usa el Doc que conoce de esa fecha o el enlace que pegues en «Evidencia» |
| Medio de transporte | El guardado en la página |

- **Reportar un día que faltó:** en *Datos del reporte → RIPOR* elige el día en «Día a reportar», deja solo el destino RIPOR y sube. La descripción sale del reporte de ese día guardado en `historial/`; si no existe, de las tareas que marques. La página recuerda el Doc de cada día que subió a Drive; si no conoce el de ese día, pega su enlace en «Evidencia».
- **Nunca registra dos veces el mismo día:** antes de enviar, la página consulta ese día en RIPOR. Si ya hay un registro del mismo proyecto y tarea (hecho a mano, o uno anterior cuya respuesta no llegó), no envía otro y lo muestra como «– omitido» con el motivo. Un registro en otro proyecto no cuenta.
- Si el reporte es de un día anterior y ese día ya está registrado, el aviso te recuerda pulsar *Nuevo día* (en vez de marcar ✓).
- RIPOR no se relaciona con los TK: no usa sus horas ni sus enlaces.
- **Un registro no se puede editar ni borrar desde aquí.** Cada fecha se registra una sola vez; si vuelves a subir, dice *ya se registró* y no repite. Para corregir, entra a <https://ripor.co>.
- RIPOR va al final de la subida, para que ya exista el enlace del Doc.
- Con **Simular** se valida todo con las reglas de RIPOR sin guardar nada.

### Reuniones de Calendar

- Cada reunión entra como `Participación en la sesión de <reunión>.`, categoría *Reuniones*, tipo *Gestión*, con su hora de inicio y fin exactas.
- Omite las de todo el día, las que rechazaste y las que ya estaban importadas. Si una reunión **cambió de hora**, se actualiza la tarea existente en vez de crear otra.
- Solo entran reuniones de «ayer» y de «hoy»; el aviso dice cuántas de otros días se dejaron fuera (por ejemplo, si no abriste la herramienta un día).
- **Aprende:** si a una reunión importada le relacionas work items (por ejemplo, el *Daily Emisión* con la Feature de la célula de Emisión), la próxima vez que la importes ya vendrá relacionada. Se recuerda por el **título de la reunión**, aunque le cambies el nombre corto. Ojo: una reunión de otra persona con exactamente el mismo título también vendría relacionada; revisa antes de subir.

---

## Seguridad

- La página y su API solo escuchan en `127.0.0.1`: nadie más en la red puede entrar.
- La API exige tu **llave local** (archivo `.llave_local`, creado la primera vez y legible solo por tu usuario). Así, otro usuario del mismo computador no puede usar tus credenciales de Azure, Chat o RIPOR. Si lo borras, se crea otro y basta con abrir de nuevo el enlace de la consola.
- Otra página abierta en el navegador no puede usar la API ni incrustar la página.
- Los textos que vienen de fuera (títulos de reuniones de Calendar) se envían a Chat sin `<`, para que no puedan mencionar a todo el espacio (`<users/all>`) ni esconder enlaces.
- **Conector de RIPOR:** si quieres que la página verifique `ripor-mcp.mjs` antes de cada uso, pon su huella en el `.env`: `RIPOR_MCP_SHA256=7430f3dc…` (el valor completo del paso 7). Si el archivo cambia, no se ejecuta. Si actualizas el conector a propósito, actualiza también ese valor.

## Uso desde la consola (opcional)

Sube **todas** las tareas de `reporte_hoy.json` sin abrir la página (en Mac, `python3` en lugar de `python`):

```
python reporte_diario.py --dry-run           # simular
python reporte_diario.py                     # publicar en Azure, Drive y Chat
python reporte_diario.py --solo devops       # solo Azure
python reporte_diario.py --solo drive ripor  # RIPOR solo si se pide explícitamente
```

Desde la consola RIPOR nunca se incluye por defecto, porque un registro no se puede deshacer.

---

## Archivos del proyecto

| Archivo | Qué es |
|---|---|
| `iniciar.bat` / `iniciar.command` | Lanzadores de doble clic (Windows / Mac) |
| `servidor.py` | Servidor de la página (solo escucha en 127.0.0.1) |
| `web/` | La página: `index.html`, `app.js`, `estilos.css` |
| `reporte_diario.py` | Lógica de publicación (Azure, Drive, Chat, RIPOR, Calendar); también se usa desde la consola |
| `apps_script/Code.gs` | El Apps Script de Drive y Calendar |
| `.env` / `.env.example` | Configuración y secretos / plantilla |
| `.llave_local` | Tu llave local para la API (se crea sola; no la compartas) |
| `.venv/` | Entorno de Python que crea el lanzador |
| `reporte_hoy.json` | Tareas y datos del reporte actual (lo maneja la página) |
| `tks_creados.json` | Registro de TK creados; evita duplicarlos |
| `historial/` | Reportes de días anteriores (*Nuevo día*) y copias hechas al *Limpiar tareas* |
| `salidas/` | Copia local en HTML de lo que se sube a Drive |

---

## Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `python` no se reconoce (Windows) / `command not found: python` (Mac) | Python no está instalado o no está en el PATH | Paso 1. En Mac usa `python3` |
| Barra: *⚠ Google…: Tu sesión de Google venció* | Linktic cierra la sesión cada ~16 h | Botón **Iniciar sesión en Google** |
| Barra: *La sesión no tiene permiso de Drive* | Se inició sesión sin `--enable-gdrive-access` | Botón **Iniciar sesión en Google** (ya lo incluye) |
| *No se encontró gcloud* | Google Cloud CLI no instalado o en otra ruta | Paso 5, o `GCLOUD_PATH` en el `.env` |
| *El Apps Script publicado aún no tiene la función de Calendar* | Falta publicar la versión nueva de `Code.gs` | Paso 4.7 |
| *El puerto 8765 ya está en uso* | Ya hay otra terminal con `servidor.py` abierta | Usa esa, o ciérrala. También puedes cambiar `REPORTE_PUERTO` en el `.env` |
| *Abre la página desde el enlace que muestra la consola* | El navegador no tiene tu llave local (otro navegador, cookies borradas o `.llave_local` nuevo) | Abre el enlace `http://127.0.0.1:8765/?k=…` que muestra la terminal |
| *Hoy es sábado/domingo/festivo…* al pulsar *Nuevo día* | Iniciar el día en un día no hábil haría que el siguiente tome ese como «ayer» | Acepta solo si trabajaste ese día |
| *La fecha de «ayer» debe ser anterior…* | Se puso una fecha de «ayer» igual o posterior a la del reporte | Corrige la fecha |
| Página: *No hay conexión con el servidor* | Se cerró la terminal de `servidor.py` | Vuelve a ejecutarlo |
| Un destino aparece desactivado | Falta su configuración o su sesión | Pasa el mouse por encima para ver el motivo; pasos 3 a 7 |
| Azure: `401 — el PAT venció…` | PAT vencido o de solo lectura | Paso 3 |
| Azure: `HTTP 404 … does not exist` | Work item inexistente o sin acceso | Revisa el número en la tarea |
| Azure: *no existe 'Sprint N'* | Ese proyecto no tiene ese sprint | El TK queda en la iteración del work item; revisa el sprint |
| Azure: *el work item tiene 10 puntos, que el TK no acepta* | El TK solo acepta 1, 2, 3, 5, 8, 13 | Elige los puntos en *Datos del TK* |
| Azure: aviso al cambiar estado | El proceso no permitió la transición | El TK sí quedó creado; ajusta el estado en Azure |
| Drive: `Apps Script respondió HTTP 401` | El token no tiene permiso de Drive | Botón **Iniciar sesión en Google** |
| Drive: `Apps Script: Secreto inválido.` | La clave del `.env` no coincide con `SECRETO` | Cópiala de nuevo (paso 4.6) |
| Chat: `HTTP 400/404` | Webhook mal copiado o eliminado | Paso 6 |
| RIPOR: *No se pudo ejecutar Node.js* | Node no instalado, o bloqueado por la empresa ("Acceso denegado") | Instálalo (paso 7) o pide a TI que lo habilite |
| RIPOR: `[SIN_SESION]` o `[SESION_VENCIDA]` | No hay sesión de RIPOR | Botón **Iniciar sesión en RIPOR** |
| RIPOR: *La evidencia de RIPOR es el Doc del día en Drive* | Aún no se ha subido el Doc de hoy | Sube también a Drive (en la misma subida sirve) |
| RIPOR: *No tengo el enlace del Doc de Drive del …* | Elegiste otro día y la página no conoce su Doc | Abre ese Doc en Drive, copia su dirección y pégala en «Evidencia» |
| RIPOR: `[H3] No puedes reportar más de …` / *Ya completaste el límite del día* | Ese día ya tiene horas en RIPOR | Revisa en la web de RIPOR, o pon las horas a mano |
| RIPOR: `[D1] … 50 caracteres` | El daily seleccionado es muy corto | Selecciona más tareas o amplía la descripción |
| RIPOR: *Tienes N proyectos vigentes* / *No se encontró una única «Tarea personalizada»* | No se pudo elegir solo | Elígelos en *Datos del reporte → RIPOR* |
| RIPOR: *ya se registró* | Ese día ya se registró desde aquí | Correcto: no se duplica |
