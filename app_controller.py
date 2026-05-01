"""Control de la app 'Tu Portal' (Chrome PWA) via CDP."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import urllib.request
from typing import AsyncIterator

import websockets
import websockets.sync.client as ws_sync
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

log = logging.getLogger("tu-portal-server")

APP_NAME    = "Tu Portal"
PROCESS_NAME = "app_mode_loader"
PORTAL_URL  = "https://www.hospitalaleman.com/tuportal/"
CDP_PORT    = 9223
CDP_PROFILE = "/tmp/tu-portal-cdp-profile"


def _find_chrome_bin() -> str:
    if platform.system() == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    for path in [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]:
        if os.path.exists(path):
            return path
    raise RuntimeError("No se encontró Chrome/Chromium en el sistema")


CHROME_BIN = _find_chrome_bin()


# ---------------------------------------------------------------------------
# Proceso / ciclo de vida
# ---------------------------------------------------------------------------

def is_running() -> tuple[bool, int | None]:
    p = subprocess.run(
        ["pgrep", "-f", f"user-data-dir={CDP_PROFILE}"],
        capture_output=True, text=True,
    )
    if p.returncode == 0 and p.stdout.strip():
        try:
            return True, int(p.stdout.strip().splitlines()[0])
        except ValueError:
            return True, None
    return False, None


def _ensure_chrome_prefs():
    """Desactiva password manager y popups en el perfil Chrome."""
    prefs_dir = os.path.join(CDP_PROFILE, "Default")
    os.makedirs(prefs_dir, exist_ok=True)
    prefs_file = os.path.join(prefs_dir, "Preferences")
    prefs: dict = {}
    if os.path.exists(prefs_file):
        with open(prefs_file) as f:
            prefs = json.load(f)
    prefs["credentials_enable_service"] = False
    prefs["credentials_enable_autosignin"] = False
    prefs.setdefault("profile", {})["password_manager_enabled"] = False
    with open(prefs_file, "w") as f:
        json.dump(prefs, f)


def open_app() -> int | None:
    """Abre Tu Portal en una instancia Chrome dedicada con CDP habilitado."""
    os.makedirs(CDP_PROFILE, exist_ok=True)
    _ensure_chrome_prefs()
    cmd = [
        CHROME_BIN,
        f"--app={PORTAL_URL}",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={CDP_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        # Desactivar popup de guardar contraseña
        "--password-store=basic",
        "--disable-save-password-bubble",
    ]
    # En Linux (servidor): modo headless sin pantalla física
    if platform.system() != "Darwin":
        cmd += [
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        running, pid = is_running()
        if running:
            return pid
        import time; time.sleep(0.3)
    return None


def close_app() -> bool:
    running, pid = is_running()
    if pid:
        subprocess.run(["kill", str(pid)], capture_output=True)
    subprocess.run(["pkill", "-f", f"user-data-dir={CDP_PROFILE}"],
                   capture_output=True)
    running, _ = is_running()
    return not running


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def _cdp_targets() -> list[dict]:
    try:
        raw = urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json", timeout=2
        ).read()
        return json.loads(raw)
    except Exception:
        return []


async def _wait_for_page_target(timeout: float = 20.0) -> dict | None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for t in _cdp_targets():
            if t.get("type") == "page" and PORTAL_URL in t.get("url", ""):
                return t
            if t.get("type") == "page" and t.get("url", "").startswith("http"):
                return t
        await asyncio.sleep(0.5)
    return None


def _cdp_eval_sync(ws_url: str, expression: str, timeout: float = 15.0) -> dict:
    with ws_sync.connect(ws_url) as conn:
        conn.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
        }))
        resp = json.loads(conn.recv(timeout=timeout))
    return resp.get("result", {}).get("result", {})


async def _cdp_eval(ws_url: str, expression: str, timeout: float = 15.0) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: _cdp_eval_sync(ws_url, expression, timeout)
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def wait_for_login_page(timeout: float = 20.0) -> dict | None:
    return await _wait_for_page_target(timeout)


async def login() -> bool:
    user = os.environ.get("TU_PORTAL_USER")
    pwd  = os.environ.get("TU_PORTAL_PASS")
    if not user or not pwd:
        raise RuntimeError("Faltan TU_PORTAL_USER / TU_PORTAL_PASS")

    target = await _wait_for_page_target(timeout=20.0)
    if not target:
        log.error("login: no se encontró target CDP")
        return False

    ws_url = target["webSocketDebuggerUrl"]
    u = user.replace("\\", "\\\\").replace("'", "\\'")
    p = pwd.replace("\\", "\\\\").replace("'", "\\'")

    js = f"""
(async () => {{
    function setVal(el, val) {{
        const setter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        setter.call(el, val);
        el.dispatchEvent(new Event('input',  {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
    }}
    // Esperar campo usuario
    const deadline = Date.now() + 8000;
    let userField = null;
    while (Date.now() < deadline) {{
        userField = document.querySelector(
            'input[type="text"], input:not([type="password"])'
        );
        if (userField) break;
        await new Promise(r => setTimeout(r, 200));
    }}
    if (!userField) return 'ERROR: campo usuario no encontrado';
    userField.focus();
    setVal(userField, '{u}');
    await new Promise(r => setTimeout(r, 300));

    const passField = document.querySelector('input[type="password"]');
    if (!passField) return 'ERROR: campo password no encontrado';
    passField.focus();
    setVal(passField, '{p}');
    await new Promise(r => setTimeout(r, 300));

    let btn = null;
    for (const b of document.querySelectorAll('button')) {{
        if ((b.textContent || '').toLowerCase().includes('iniciar')) {{
            btn = b; break;
        }}
    }}
    if (!btn) return 'ERROR: boton Iniciar sesion no encontrado';
    btn.click();
    return 'OK';
}})()
"""
    try:
        result = await _cdp_eval(ws_url, js, timeout=20.0)
        value = result.get("value", "")
        if value == "OK":
            log.info("login CDP exitoso")
            return True
        log.error("login JS result: %s", value)
        return False
    except Exception as e:
        log.error("login error: %s", e)
        return False


async def open_and_login() -> dict:
    pid = open_app()
    await asyncio.sleep(2.0)
    target = await wait_for_login_page(timeout=20.0)
    logged = False
    if target:
        logged = await login()
    return {"pid": pid, "page_ready": target is not None, "logged_in": logged}


# ---------------------------------------------------------------------------
# Reserva de turno
# ---------------------------------------------------------------------------

async def _wait_for_url(url_fragment: str, timeout: float = 15.0) -> dict | None:
    """Espera a que la página actual contenga `url_fragment`."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = await _wait_for_page_target(timeout=3.0)
        if t and url_fragment in t.get("url", ""):
            return t
        await asyncio.sleep(0.5)
    return None


async def ir_a_reservar_turno() -> bool:
    """Navega al formulario de Reservar turno desde cualquier pantalla post-login."""
    target = await _wait_for_page_target(timeout=10.0)
    if not target:
        return False
    js = """
    (() => {
        const link = document.querySelector('a[href*="reservarTurno"]');
        if (link) { link.click(); return 'clicked'; }
        window.location.href = '/tuportal/app/reservarTurno';
        return 'navigated';
    })()
    """
    await _cdp_eval(target["webSocketDebuggerUrl"], js, timeout=5.0)
    t = await _wait_for_url("reservarTurno", timeout=10.0)
    return t is not None


async def _fill_autocomplete(ws_url: str, field_id: str, texto: str) -> str:
    """Escribe `texto` en un campo mat-autocomplete y selecciona la primera opción que coincida."""
    js = f"""
(async () => {{
    const inp = document.getElementById('{field_id}');
    if (!inp) return 'ERROR: campo {field_id} no encontrado';
    inp.focus();
    inp.click();
    inp.value = '';

    // Tipear caracter a caracter para disparar el autocomplete de Angular
    for (const ch of '{texto}') {{
        inp.value += ch;
        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
        inp.dispatchEvent(new KeyboardEvent('keydown', {{bubbles: true, key: ch}}));
        inp.dispatchEvent(new KeyboardEvent('keyup',   {{bubbles: true, key: ch}}));
        await new Promise(r => setTimeout(r, 80));
    }}
    await new Promise(r => setTimeout(r, 1200));

    // Buscar opciones del panel
    const options = [...document.querySelectorAll('mat-option')];
    if (options.length === 0) return 'ERROR: sin opciones para "{texto}"';

    // Seleccionar la primera opción que contenga el texto (case-insensitive)
    const searchLower = '{texto}'.toLowerCase();
    const match = options.find(o => o.textContent.toLowerCase().includes(searchLower)) || options[0];
    const selected = match.textContent.trim().slice(0, 80);
    match.click();
    await new Promise(r => setTimeout(r, 500));
    return 'OK:' + selected;
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=20.0)
    return result.get("value", "ERROR: sin respuesta")


async def reservar_turno(especialidad: str, profesional: str) -> dict:
    """
    Flujo completo:
      1. Navega a Reservar turno
      2. Llena Especialidad con `especialidad` y selecciona la primera opción
      3. Llena Profesional con `profesional` y selecciona la primera opción

    Retorna dict con los campos seleccionados o errores.
    """
    # 1. Navegar a la página
    ok = await ir_a_reservar_turno()
    if not ok:
        return {"ok": False, "error": "No se pudo navegar a Reservar turno"}

    target = await _wait_for_page_target(timeout=10.0)
    if not target:
        return {"ok": False, "error": "Sin target CDP"}

    ws_url = target["webSocketDebuggerUrl"]

    # Esperar que los campos mat-input estén en el DOM
    await asyncio.sleep(1.5)
    js_wait = """
    (async () => {
        const dl = Date.now() + 8000;
        while (Date.now() < dl) {
            const inputs = document.querySelectorAll('input[id^="mat-input"]');
            if (inputs.length >= 2) return inputs.length;
            await new Promise(r => setTimeout(r, 300));
        }
        return 0;
    })()
    """
    r = await _cdp_eval(ws_url, js_wait, timeout=12.0)
    n_inputs = r.get("value", 0)
    if n_inputs < 2:
        return {"ok": False, "error": f"Solo se encontraron {n_inputs} inputs en la página"}

    # Obtener los IDs reales de los campos (pueden variar entre sesiones)
    js_ids = """
    (() => {
        const inputs = [...document.querySelectorAll('input[id^="mat-input"]')];
        return inputs.map(i => i.id);
    })()
    """
    ids_result = await _cdp_eval(ws_url, js_ids, timeout=5.0)
    ids = ids_result.get("value", [])
    if len(ids) < 2:
        return {"ok": False, "error": "No se pudieron obtener los IDs de los campos"}

    field_esp = ids[0]
    field_pro = ids[1]
    log.info("Campos: especialidad=%s profesional=%s", field_esp, field_pro)

    # 2. Especialidad
    esp_result = await _fill_autocomplete(ws_url, field_esp, especialidad)
    log.info("Especialidad: %s", esp_result)
    if esp_result.startswith("ERROR"):
        return {"ok": False, "error": esp_result, "step": "especialidad"}

    await asyncio.sleep(0.8)

    # 3. Profesional
    pro_result = await _fill_autocomplete(ws_url, field_pro, profesional)
    log.info("Profesional: %s", pro_result)
    if pro_result.startswith("ERROR"):
        return {"ok": False, "error": pro_result, "step": "profesional"}

    return {
        "ok": True,
        "especialidad_seleccionada": esp_result.removeprefix("OK:"),
        "profesional_seleccionado": pro_result.removeprefix("OK:"),
    }


# ---------------------------------------------------------------------------
# Bot de búsqueda de turnos
# ---------------------------------------------------------------------------

MESES = {
    "Enero": 1, "Febrero": 2, "Marzo": 3, "Abril": 4,
    "Mayo": 5, "Junio": 6, "Julio": 7, "Agosto": 8,
    "Septiembre": 9, "Octubre": 10, "Noviembre": 11, "Diciembre": 12,
}
MESES_INV = {v: k for k, v in MESES.items()}


async def _dismiss_session_dialog(ws_url: str) -> bool:
    """Cierra el dialog 'Tu sesión ha expirado' si está presente. Retorna True si lo encontró."""
    js = """
    (() => {
        const expired = document.body.innerText.includes('Tu sesión ha expirado');
        if (!expired) return false;
        const btns = [...document.querySelectorAll('button')];
        const aceptar = btns.find(b => b.textContent.trim() === 'Aceptar');
        if (aceptar) { aceptar.click(); return true; }
        return false;
    })()
    """
    try:
        result = await _cdp_eval(ws_url, js, timeout=5.0)
        return result.get("value") is True
    except Exception:
        return False


async def ensure_session() -> str | None:
    """Asegura Chrome abierto + logueado. Retorna ws_url o None."""
    running, _ = is_running()
    if not running:
        log.info("Chrome no está corriendo, abriendo...")
        open_app()
        await asyncio.sleep(3.0)

    target = await _wait_for_page_target(timeout=15.0)
    if not target:
        log.error("ensure_session: sin target CDP, reiniciando Chrome")
        close_app()
        await asyncio.sleep(1.0)
        open_app()
        await asyncio.sleep(4.0)
        target = await _wait_for_page_target(timeout=15.0)
        if not target:
            return None

    # Detectar dialog de sesión expirada (aparece sobre la página sin cambiar URL)
    dismissed = await _dismiss_session_dialog(target["webSocketDebuggerUrl"])
    if dismissed:
        log.info("Dialog 'Tu sesión ha expirado' detectado y cerrado, esperando redirect...")
        await asyncio.sleep(2.0)
        target = await _wait_for_page_target(timeout=10.0)
        if not target:
            return None

    # Si estamos en /login, re-loguearse
    if "/login" in target.get("url", ""):
        log.info("Sesión expirada, re-logueando...")
        ok = await login()
        if not ok:
            return None
        await asyncio.sleep(2.0)
        target = await _wait_for_page_target(timeout=10.0)
        if not target:
            return None

    return target["webSocketDebuggerUrl"]


async def _reiniciar_y_buscar(ws_url: str, especialidad: str, profesional: str) -> tuple[str, str]:
    """Recarga la página, llena formulario y clickea Buscar. Retorna (resultado, nuevo_ws_url)."""
    # Paso 1: recarga completa para limpiar estado Angular
    reload_js = """
    (() => {
        window.location.href = '/tuportal/app/reservarTurno';
        return 'reloading';
    })()
    """
    try:
        await _cdp_eval(ws_url, reload_js, timeout=5.0)
    except Exception:
        pass  # La navegación puede cortar la conexión WS, es esperado
    await asyncio.sleep(4.0)

    # Paso 2: obtener nuevo ws_url después de la recarga
    target = await _wait_for_page_target(timeout=15.0)
    if not target:
        return "ERROR: no se reconectó CDP tras recarga", ws_url
    ws_url = target["webSocketDebuggerUrl"]

    # Paso 3: esperar que la página cargue y llenar formulario
    js = f"""
(async () => {{
    // Verificar si hay dialog de sesión expirada
    if (document.body.innerText.includes('Tu sesión ha expirado')) return 'SESSION_EXPIRED';

    // Esperar inputs (la página acaba de cargar)
    const dl = Date.now() + 10000;
    let inputs;
    while (Date.now() < dl) {{
        if (document.body.innerText.includes('Tu sesión ha expirado')) return 'SESSION_EXPIRED';
        inputs = document.querySelectorAll('input[id^="mat-input"]');
        if (inputs.length >= 2) break;
        await new Promise(r => setTimeout(r, 300));
    }}
    if (!inputs || inputs.length < 2) return 'ERROR: inputs no encontrados';

    function typeInto(inp, text) {{
        inp.focus(); inp.click(); inp.value = '';
        for (const ch of text) {{
            inp.value += ch;
            inp.dispatchEvent(new Event('input', {{bubbles:true}}));
            inp.dispatchEvent(new KeyboardEvent('keydown', {{bubbles:true, key:ch}}));
            inp.dispatchEvent(new KeyboardEvent('keyup',   {{bubbles:true, key:ch}}));
        }}
    }}

    // Especialidad
    typeInto(inputs[0], '{especialidad}');
    await new Promise(r => setTimeout(r, 1500));
    const espOpts = [...document.querySelectorAll('mat-option')];
    const espMatch = espOpts.find(o => o.textContent.toLowerCase().includes('{especialidad.lower()}'));
    if (!espMatch) return 'ERROR: especialidad no encontrada';
    espMatch.click();
    await new Promise(r => setTimeout(r, 1000));

    // Profesional
    typeInto(inputs[1], '{profesional}');
    await new Promise(r => setTimeout(r, 1500));
    const proOpts = [...document.querySelectorAll('mat-option')];
    const proMatch = proOpts.find(o => o.textContent.toLowerCase().includes('{profesional.lower()}'));
    if (!proMatch) return 'ERROR: profesional no encontrado';
    proMatch.click();
    await new Promise(r => setTimeout(r, 500));

    // Click Buscar
    const buscar = [...document.querySelectorAll('button')].find(b =>
        b.textContent.trim().includes('Buscar'));
    if (!buscar) return 'ERROR: boton Buscar no encontrado';
    buscar.click();
    await new Promise(r => setTimeout(r, 3000));
    return 'OK';
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=30.0)
    return result.get("value", "ERROR: sin respuesta"), ws_url


async def _navegar_a_mes(ws_url: str, mes_nombre: str, anio: int) -> str:
    """Navega el calendario al mes/año objetivo. Retorna 'OK', 'NO_AGENDA' o 'ERROR:...'."""
    js = f"""
(async () => {{
    const objetivo = '{mes_nombre} {anio}';

    for (let i = 0; i < 12; i++) {{
        const monthMatch = document.body.innerText.match(
            /(Enero|Febrero|Marzo|Abril|Mayo|Junio|Julio|Agosto|Septiembre|Octubre|Noviembre|Diciembre) (\\d{{4}})/
        );
        if (!monthMatch) return 'ERROR: no se encontró mes en el calendario';
        const mesActual = monthMatch[0];

        if (mesActual === objetivo) return 'OK';

        // Determinar dirección
        const meses = ['Enero','Febrero','Marzo','Abril','Mayo','Junio',
                       'Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
        const actualIdx = meses.indexOf(monthMatch[1]) + parseInt(monthMatch[2]) * 12;
        const objParts = objetivo.split(' ');
        const objIdx = meses.indexOf(objParts[0]) + parseInt(objParts[1]) * 12;

        if (actualIdx > objIdx) {{
            // Ir hacia atrás
            const anteriorBtn = [...document.querySelectorAll('button,span')]
                .find(b => b.textContent.trim() === 'Anterior');
            if (!anteriorBtn || anteriorBtn.disabled ||
                anteriorBtn.getAttribute('disabled') !== null ||
                anteriorBtn.closest('button')?.disabled) {{
                return 'NO_AGENDA';
            }}
            (anteriorBtn.closest('button') || anteriorBtn).click();
        }} else {{
            // Ir hacia adelante
            const siguienteBtn = [...document.querySelectorAll('button,span')]
                .find(b => b.textContent.trim() === 'Siguiente');
            if (!siguienteBtn) return 'ERROR: boton Siguiente no encontrado';
            (siguienteBtn.closest('button') || siguienteBtn).click();
        }}
        await new Promise(r => setTimeout(r, 1500));
    }}
    return 'ERROR: no se pudo llegar al mes objetivo en 12 intentos';
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=30.0)
    return result.get("value", "ERROR: sin respuesta")


async def _obtener_dias_disponibles(ws_url: str) -> list[int]:
    """Retorna los números de día que tienen turno-disponible en el mes actual."""
    js = """
    (() => {
        const btns = [...document.querySelectorAll('button.turno-disponible')];
        return btns.map(b => parseInt(b.textContent.trim())).filter(n => !isNaN(n));
    })()
    """
    result = await _cdp_eval(ws_url, js, timeout=5.0)
    return result.get("value", [])


async def _extraer_horarios_dia(ws_url: str, dia: int) -> list[dict]:
    """Clickea un día con turno-disponible y extrae los horarios."""
    js = f"""
(async () => {{
    const dayBtn = [...document.querySelectorAll('button.turno-disponible')]
        .find(b => b.textContent.trim() === '{dia}');
    if (!dayBtn) return JSON.stringify([]);
    dayBtn.click();
    await new Promise(r => setTimeout(r, 2000));

    // Parsear la tabla de turnos
    const section = document.body.innerText.split('Turnos Disponibles')[1];
    if (!section) return JSON.stringify([]);

    const turnos = [];
    const lines = section.split('\\n').map(l => l.trim()).filter(Boolean);
    let turno = {{}};
    for (const line of lines) {{
        if (line.startsWith('Fecha'))   {{ if (turno.hora) turnos.push(turno); turno = {{}}; }}
        if (line.match(/^\\d{{2}}-[A-Z]{{3}}-\\d{{2}}$/))  turno.fecha = line;
        if (line.match(/^\\d{{2}}:\\d{{2}}$/))              turno.hora = line;
        if (line.includes('RUSI'))                         turno.profesional = line;
        if (line.includes('POLICLINICA') || line.includes('HOSPITAL') || line.includes('Centro'))
            turno.lugar = line;
        if (line.includes('Pueyrredón') || line.includes('Dirección'))
            turno.direccion = line;
    }}
    if (turno.hora) turnos.push(turno);
    return JSON.stringify(turnos);
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=15.0)
    raw = result.get("value", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


async def buscar_turno_mas_cercano(
    especialidad: str,
    profesional: str,
    mes: str,
    anio: int,
) -> dict:
    """
    Flujo completo de búsqueda:
      1. Asegurar sesión
      2. Reiniciar búsqueda + llenar formulario + Buscar
      3. Navegar al mes objetivo
      4. Si hay días disponibles, extraer horarios
      5. Retornar el turno más cercano

    Retorna: {"encontrado": bool, "turnos": [...], "turno_cercano": {...} | None, "error": str | None}
    """
    ws_url = await ensure_session()
    if not ws_url:
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": "No se pudo establecer sesión CDP"}

    # Paso 2: recargar página + formulario
    # _reiniciar_y_buscar navega a /reservarTurno y refresca el target CDP
    search_result, ws_url = await _reiniciar_y_buscar(ws_url, especialidad, profesional)

    # Si la sesión expiró durante la navegación, re-login y reintentar una vez
    if search_result == "SESSION_EXPIRED":
        log.info("Sesión expirada detectada durante búsqueda, re-estableciendo sesión...")
        ws_url = await ensure_session()
        if not ws_url:
            return {"encontrado": False, "turnos": [], "turno_cercano": None,
                    "error": "Re-login fallido tras sesión expirada"}
        search_result, ws_url = await _reiniciar_y_buscar(ws_url, especialidad, profesional)

    if search_result.startswith("ERROR"):
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": search_result}

    # Paso 3: navegar al mes
    nav_result = await _navegar_a_mes(ws_url, mes, anio)
    if nav_result == "NO_AGENDA":
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": f"No hay agenda en {mes} {anio}"}
    if nav_result.startswith("ERROR"):
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": nav_result}

    # Paso 4: días disponibles
    dias = await _obtener_dias_disponibles(ws_url)
    if not dias:
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": f"Sin turnos disponibles en {mes} {anio}"}

    # Paso 5: extraer horarios de cada día
    todos_los_turnos = []
    for dia in sorted(dias):
        horarios = await _extraer_horarios_dia(ws_url, dia)
        for h in horarios:
            h["dia"] = dia
        todos_los_turnos.extend(horarios)

    if not todos_los_turnos:
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": "Días marcados pero sin horarios"}

    # Ordenar por fecha+hora y tomar el más cercano
    todos_los_turnos.sort(key=lambda t: (t.get("dia", 99), t.get("hora", "99:99")))
    cercano = todos_los_turnos[0]

    return {
        "encontrado": True,
        "turnos": todos_los_turnos,
        "turno_cercano": cercano,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Debug UI via CDP
# ---------------------------------------------------------------------------

async def get_ui_fields() -> str:
    target = await _wait_for_page_target(timeout=5.0)
    if not target:
        return "error: sin target CDP"
    js = """
    Array.from(document.querySelectorAll('input')).map(el =>
        (el.placeholder || el.name || el.type || '?') + ' [' + el.type + ']'
    ).join('; ')
    """
    result = await _cdp_eval(target["webSocketDebuggerUrl"], js, timeout=5.0)
    return result.get("value", "")


# ---------------------------------------------------------------------------
# AppleScript genérico
# ---------------------------------------------------------------------------

def run_applescript_sync(script: str, timeout: float = 10.0) -> str:
    p = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"osascript failed: {p.stderr or p.stdout}")
    return p.stdout.strip()


# ---------------------------------------------------------------------------
# Logs via unified logging
# ---------------------------------------------------------------------------

async def tail_logs() -> AsyncIterator[tuple[str, str]]:
    proc = await asyncio.create_subprocess_exec(
        "log", "stream",
        "--style", "compact",
        "--predicate", f'process == "{PROCESS_NAME}"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield ("stdout", line.decode(errors="replace").rstrip())
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
