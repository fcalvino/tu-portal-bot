"""Control de la app 'Tu Portal' (Chrome PWA) via CDP."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
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
            "--window-size=1280,800",
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
MESES_ABREV = {
    "Enero": "ENE", "Febrero": "FEB", "Marzo": "MAR", "Abril": "ABR",
    "Mayo": "MAY", "Junio": "JUN", "Julio": "JUL", "Agosto": "AGO",
    "Septiembre": "SEP", "Octubre": "OCT", "Noviembre": "NOV", "Diciembre": "DIC",
}
ABREV_TO_NUM = {abbr: MESES[name] for name, abbr in MESES_ABREV.items()}


def _mes_a_num(mes_nombre: str) -> int:
    """Convierte nombre de mes (Junio, JULIO, etc) a número 1-12."""
    if not mes_nombre:
        return 0
    # direct
    if mes_nombre in MESES:
        return MESES[mes_nombre]
    # case insensitive + partial
    n = mes_nombre.strip().lower()
    for nombre, num in MESES.items():
        if nombre.lower() == n or n in nombre.lower():
            return num
    return 0


def _parse_dia_from_fecha(fecha: str | None) -> int | None:
    """Extrae día desde '30-JUN-26' o similar."""
    if not fecha:
        return None
    m = re.match(r'^(\d{2})-', str(fecha).strip())
    return int(m.group(1)) if m else None


def _parse_mes_from_fecha(fecha: str | None) -> int | None:
    """Extrae número de mes desde la abreviatura en la fecha del portal."""
    if not fecha:
        return None
    m = re.search(r'-([A-Z]{3})-', str(fecha).upper())
    if m:
        return ABREV_TO_NUM.get(m.group(1))
    return None


def _dia_desde_fecha(fecha: str | None) -> int | None:
    """Extrae el número de día (1-31) desde el string de fecha que devuelve el portal (ej: '15-JUN-26')."""
    if not fecha:
        return None
    m = re.match(r'^(\d{2})-', str(fecha).strip())
    return int(m.group(1)) if m else None


async def _dismiss_blocking_dialogs(ws_url: str) -> bool:
    """Cierra dialogs bloqueantes con botón 'Aceptar' (sesión expirada, info anteojos/oftalmología,
    warnings, u otros avisos modales). Retorna True si se cerró alguno.
    """
    js = """
(() => {
    const bodyText = (document.body.innerText || '').toLowerCase();
    const isKnownBlocking =
        bodyText.includes('tu sesión ha expirado') ||
        bodyText.includes('anteojos') ||
        bodyText.includes('3 meses') ||
        bodyText.includes('oftalmolog') ||
        bodyText.includes('receta de anteojos') ||
        bodyText.includes('pérdida o rotura');

    // Buscar contenedores de dialog típicos (Angular Material + genéricos)
    let containers = [
        ...document.querySelectorAll(
            'mat-dialog-container, .cdk-overlay-pane, [role="dialog"], .mat-mdc-dialog-container'
        )
    ].filter(el => {
        try {
            const r = el.getBoundingClientRect();
            return r.width > 140 && r.height > 70 && el.offsetParent !== null;
        } catch { return false; }
    });

    if (containers.length === 0 && isKnownBlocking) {
        containers = [document.body];
    }

    const buttons = [...document.querySelectorAll('button')];
    let clicked = false;

    const isTargetBtn = (b) => {
        const t = (b.textContent || '').trim();
        return t === 'Aceptar' || t === 'Entendido' || t === 'OK' || t.toLowerCase() === 'aceptar';
    };

    for (const c of containers) {
        const btn = buttons.find(b => {
            if (!isTargetBtn(b)) return false;
            if (b.offsetParent === null) return false;
            if (c.contains(b)) return true;
            if (b.closest('.cdk-overlay-pane, mat-dialog-container, [role="dialog"], .mat-mdc-dialog-container')) return true;
            return containers.length <= 1;
        });
        if (btn) {
            btn.click();
            clicked = true;
            break;
        }
    }

    if (!clicked && isKnownBlocking) {
        // Fallback: cualquier Aceptar visible cuando detectamos texto conocido
        const a = buttons.find(b => (b.textContent || '').trim() === 'Aceptar' && b.offsetParent !== null);
        if (a) {
            a.click();
            clicked = true;
        }
    }

    return clicked;
})()
"""
    try:
        result = await _cdp_eval(ws_url, js, timeout=6.0)
        return result.get("value") is True
    except Exception:
        return False


# Alias de compatibilidad (por si se referencia en otros lados)
_dismiss_session_dialog = _dismiss_blocking_dialogs


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

    # Detectar y cerrar cualquier dialog bloqueante (sesión expirada, info de anteojos, warnings, etc.)
    dismissed = await _dismiss_blocking_dialogs(target["webSocketDebuggerUrl"])
    if dismissed:
        log.info("Dialog bloqueante detectado y cerrado (sesión expirada o aviso informativo como anteojos), esperando...")
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

    # Un último intento de cerrar cualquier aviso residual después de estabilizar
    await _dismiss_blocking_dialogs(target["webSocketDebuggerUrl"])

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

    # Cerrar cualquier modal informativo o aviso que aparezca al cargar /reservarTurno
    if await _dismiss_blocking_dialogs(ws_url):
        log.info("Dialog bloqueante cerrado después de cargar reservarTurno (posible aviso de anteojos / oftalmología u otro)")
        await asyncio.sleep(1.2)

    # Paso 3: esperar que la página cargue y llenar formulario
    js = f"""
(async () => {{
    function _tryDismissBlocking() {{
        const txt = (document.body.innerText || '').toLowerCase();
        const looksBlocking = txt.includes('tu sesión ha expirado') ||
                              txt.includes('anteojos') || txt.includes('3 meses') ||
                              txt.includes('oftalmolog') || txt.includes('receta');
        const containers = [
            ...document.querySelectorAll('mat-dialog-container, .cdk-overlay-pane, [role="dialog"], .mat-mdc-dialog-container')
        ].filter(el => {{
            try {{
                const r = el.getBoundingClientRect();
                return r.width > 100 && r.height > 60;
            }} catch {{ return false; }}
        }});
        const btns = [...document.querySelectorAll('button')];
        let targetBtn = btns.find(b => {{
            const t = (b.textContent || '').trim();
            if (t !== 'Aceptar' && t !== 'Entendido') return false;
            if (b.offsetParent === null) return false;
            return containers.some(c => c.contains(b)) || b.closest('.cdk-overlay-pane');
        }});
        if (!targetBtn && looksBlocking) {{
            targetBtn = btns.find(b => (b.textContent || '').trim() === 'Aceptar' && b.offsetParent !== null);
        }}
        if (targetBtn) {{
            targetBtn.click();
            return true;
        }}
        return false;
    }}

    // Verificar / cerrar si hay dialog de sesión expirada
    if (document.body.innerText.includes('Tu sesión ha expirado')) return 'SESSION_EXPIRED';

    // Esperar inputs (la página acaba de cargar). Intentar cerrar modales en cada iteración.
    const dl = Date.now() + 10000;
    let inputs;
    while (Date.now() < dl) {{
        if (document.body.innerText.includes('Tu sesión ha expirado')) return 'SESSION_EXPIRED';
        _tryDismissBlocking();
        inputs = document.querySelectorAll('input[id^="mat-input"]');
        if (inputs.length >= 2) break;
        await new Promise(r => setTimeout(r, 300));
    }}
    if (!inputs || inputs.length < 2) return 'ERROR: inputs no encontrados';

    // Asegurar modalidad Presencial (el popup de anteojos suele aparecer en este flujo)
    (() => {{
        const pres = [...document.querySelectorAll('button,mat-button-toggle, .mat-button-toggle')]
            .find(el => (el.textContent || '').toLowerCase().includes('presencial'));
        if (pres) {{
            // Click aunque parezca activo; es idempotente y asegura el estado correcto
            pres.click();
        }}
    }})();
    await new Promise(r => setTimeout(r, 400));
    _tryDismissBlocking();
    await new Promise(r => setTimeout(r, 300));

    // Setter nativo para disparar correctamente Angular change detection en headless
    const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;

    function typeInto(inp, text) {{
        inp.focus();
        inp.click();
        nativeSetter.call(inp, '');
        inp.dispatchEvent(new Event('input', {{bubbles:true}}));
        for (const ch of text) {{
            nativeSetter.call(inp, inp.value + ch);
            inp.dispatchEvent(new KeyboardEvent('keydown', {{bubbles:true, key:ch}}));
            inp.dispatchEvent(new Event('input', {{bubbles:true}}));
            inp.dispatchEvent(new KeyboardEvent('keyup', {{bubbles:true, key:ch}}));
        }}
        inp.dispatchEvent(new Event('change', {{bubbles:true}}));
    }}

    // Especialidad
    typeInto(inputs[0], '{especialidad}');
    // Esperar hasta 4s a que aparezcan las opciones
    const espOpts = await (async () => {{
        for (let i = 0; i < 20; i++) {{
            _tryDismissBlocking();
            const opts = [...document.querySelectorAll('mat-option')];
            if (opts.length > 0) return opts;
            await new Promise(r => setTimeout(r, 200));
        }}
        return [];
    }})();
    const espMatch = espOpts.find(o => o.textContent.toLowerCase().includes('{especialidad.lower()}'));
    if (!espMatch) return 'ERROR: especialidad no encontrada';
    espMatch.click();
    await new Promise(r => setTimeout(r, 800));
    _tryDismissBlocking();
    await new Promise(r => setTimeout(r, 200));

    // Profesional
    typeInto(inputs[1], '{profesional}');
    // Esperar hasta 4s a que aparezcan las opciones
    const proOpts = await (async () => {{
        for (let i = 0; i < 20; i++) {{
            _tryDismissBlocking();
            const opts = [...document.querySelectorAll('mat-option')];
            if (opts.length > 0) return opts;
            await new Promise(r => setTimeout(r, 200));
        }}
        return [];
    }})();
    const proMatch = proOpts.find(o => o.textContent.toLowerCase().includes('{profesional.lower()}'));
    if (!proMatch) return 'ERROR: profesional no encontrado';
    proMatch.click();
    await new Promise(r => setTimeout(r, 400));
    _tryDismissBlocking();
    await new Promise(r => setTimeout(r, 100));

    // Click Buscar (y limpiar cualquier modal que haya aparecido al seleccionar)
    _tryDismissBlocking();
    const buscar = [...document.querySelectorAll('button')].find(b =>
        b.textContent.trim().includes('Buscar'));
    if (!buscar) return 'ERROR: boton Buscar no encontrado';
    buscar.click();
    await new Promise(r => setTimeout(r, 1200));
    _tryDismissBlocking();
    await new Promise(r => setTimeout(r, 1800));
    return 'OK';
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=30.0)
    ret = result.get("value", "ERROR: sin respuesta")

    # Último intento de cerrar modales justo después del submit (el popup puede aparecer post-Buscar)
    if await _dismiss_blocking_dialogs(ws_url):
        log.info("Dialog bloqueante (ej. anteojos/oftalmología) cerrado post-Buscar")
        await asyncio.sleep(1.0)

    return ret, ws_url


async def _navegar_a_mes(ws_url: str, mes_nombre: str, anio: int) -> str:
    """Navega el calendario al mes/año objetivo. Retorna 'OK', 'NO_AGENDA' o 'ERROR:...'.

    Mejoras de robustez:
    - Comparación de mes normalizada (case-insensitive, sin acentos básicos, trim).
    - Manejo más laxo de botones Anterior/Siguiente (includes + lower).
    - Si el cálculo de índice falla por nombre de mes desconocido, intenta seguir
      navegando hasta 12 pasos o hasta que el header normalizado coincida.
    - Verificación final explícita del mes alcanzado.
    """
    # Cerrar modales antes de tocar el calendario (importante para popups post-Buscar)
    if await _dismiss_blocking_dialogs(ws_url):
        await asyncio.sleep(0.5)

    js = f"""
(async () => {{
    const objetivoRaw = '{mes_nombre} {anio}';

    function norm(s) {{
        if (!s) return '';
        let x = String(s).trim().toLowerCase();
        x = x.replace(/[áàäâ]/g, 'a')
             .replace(/[éèëê]/g, 'e')
             .replace(/[íìïî]/g, 'i')
             .replace(/[óòöô]/g, 'o')
             .replace(/[úùüû]/g, 'u')
             .replace(/septiembre|setiembre/g, 'septiembre');
        return x;
    }}

    const objetivoNorm = norm(objetivoRaw);

    // Lista canónica para índice (sin acentos para robustez)
    const mesesCanon = ['enero','febrero','marzo','abril','mayo','junio',
                        'julio','agosto','septiembre','octubre','noviembre','diciembre'];

    function monthToIdx(name, year) {{
        const n = norm(name);
        let idx = mesesCanon.indexOf(n);
        if (idx === -1) {{
            // fallback: intentar match parcial
            for (let i=0; i<mesesCanon.length; i++) if (n.includes(mesesCanon[i]) || mesesCanon[i].includes(n)) return i;
            return -1;
        }}
        return idx;
    }}

    for (let i = 0; i < 12; i++) {{
        // Intentar cerrar cualquier aviso que pudiera aparecer durante la navegación del mes
        (() => {{
            const btn = [...document.querySelectorAll('button')].find(b => (b.textContent||'').trim()==='Aceptar' && b.offsetParent);
            if (btn) btn.click();
        }})();
        const monthMatch = document.body.innerText.match(
            /(Enero|Febrero|Marzo|Abril|Mayo|Junio|Julio|Agosto|Septiembre|Octubre|Noviembre|Diciembre) (\\d{{4}})/i
        );
        if (!monthMatch) return 'ERROR: no se encontró mes en el calendario';

        const mesActualRaw = monthMatch[0];
        const mesActualNorm = norm(mesActualRaw);

        if (mesActualNorm === objetivoNorm) return 'OK';

        // Calcular dirección con tolerancia
        const actualMonthName = monthMatch[1];
        const actualYear = parseInt(monthMatch[2], 10);
        const actualM = monthToIdx(actualMonthName, actualYear);
        const objParts = objetivoRaw.split(' ');
        const objM = monthToIdx(objParts[0], parseInt(objParts[1], 10));
        const actualIdx = (actualM >= 0 ? actualM : 0) + actualYear * 12;
        const objIdx = (objM >= 0 ? objM : 0) + parseInt(objParts[1], 10) * 12;

        let went = false;
        if (actualIdx > objIdx) {{
            const anteriorBtn = [...document.querySelectorAll('button,span')]
                .find(b => norm(b.textContent).includes('anterior'));
            if (!anteriorBtn || anteriorBtn.disabled ||
                anteriorBtn.getAttribute('disabled') !== null ||
                anteriorBtn.closest('button')?.disabled) {{
                return 'NO_AGENDA';
            }}
            (anteriorBtn.closest('button') || anteriorBtn).click();
            went = true;
        }} else {{
            const siguienteBtn = [...document.querySelectorAll('button,span')]
                .find(b => norm(b.textContent).includes('siguiente'));
            if (!siguienteBtn) return 'ERROR: boton Siguiente no encontrado';
            (siguienteBtn.closest('button') || siguienteBtn).click();
            went = true;
        }}
        if (!went) break;
        await new Promise(r => setTimeout(r, 1500));
    }}

    // Verificación final
    const finalMatch = document.body.innerText.match(
        /(Enero|Febrero|Marzo|Abril|Mayo|Junio|Julio|Agosto|Septiembre|Octubre|Noviembre|Diciembre) (\\d{{4}})/i
    );
    if (finalMatch && norm(finalMatch[0]) === objetivoNorm) return 'OK';

    return 'ERROR: no se pudo llegar al mes objetivo en 12 intentos (actual: ' + (finalMatch ? finalMatch[0] : 'desconocido') + ')';
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=30.0)
    return result.get("value", "ERROR: sin respuesta")


async def _obtener_dias_disponibles(ws_url: str) -> list[int]:
    """Retorna los números de día que tienen turno-disponible en el mes actual."""
    await _dismiss_blocking_dialogs(ws_url)
    js = """
    (() => {
        const btns = [...document.querySelectorAll('button.turno-disponible')];
        return btns.map(b => parseInt(b.textContent.trim())).filter(n => !isNaN(n));
    })()
    """
    result = await _cdp_eval(ws_url, js, timeout=5.0)
    return result.get("value", [])


async def _extraer_horarios_dia(ws_url: str, dia_label: int) -> list[dict]:
    """Clickea un día (por label del calendario) y extrae los horarios.
    El 'dia' de cada turno se deriva preferentemente del campo 'fecha' que reporta el portal
    (ej. '15-JUN-26'), no del label del botón clickeado. Esto asegura que el filtro min_dia
    y el turno más cercano usen el día real del turno.
    """
    await _dismiss_blocking_dialogs(ws_url)
    js = f"""
(async () => {{
    const dayBtn = [...document.querySelectorAll('button.turno-disponible')]
        .find(b => b.textContent.trim() === '{dia_label}');
    if (!dayBtn) return JSON.stringify([]);
    dayBtn.click();
    await new Promise(r => setTimeout(r, 2000));

    // Parsear la tabla de turnos
    const section = document.body.innerText.split('Turnos Disponibles')[1];
    if (!section) return JSON.stringify([]);

    const turnos = [];
    const lines = section.split('\\n').map(l => l.trim()).filter(Boolean);
    const esLugar = l => l.includes('POLICLINICA') || l.includes('HOSPITAL') || l.includes('Centro');
    const esDireccion = l => l.includes('Pueyrredón') || l.includes('Dirección') || l.includes('Av.');
    let turno = {{}};
    let esperandoProfesional = false;
    for (const line of lines) {{
        if (line.startsWith('Fecha')) {{
            if (turno.hora) turnos.push(turno);
            turno = {{}};
            esperandoProfesional = false;
        }}
        if (line.match(/^\\d{{2}}-[A-Z]{{3}}-\\d{{2}}$/)) turno.fecha = line;
        if (line.match(/^\\d{{2}}:\\d{{2}}$/)) {{ turno.hora = line; esperandoProfesional = true; }}
        if (esLugar(line))      {{ turno.lugar = line;    esperandoProfesional = false; }}
        if (esDireccion(line))  {{ turno.direccion = line; esperandoProfesional = false; }}
        if (esperandoProfesional && !turno.profesional && !esLugar(line) && !esDireccion(line) && !line.match(/^\\d/))
            turno.profesional = line;
    }}
    if (turno.hora) turnos.push(turno);

    // Derivar 'dia' del dato real del portal (fecha), no del label del botón.
    // Guardamos dia_label solo para diagnóstico / auditoría.
    for (const t of turnos) {{
        if (t.fecha) {{
            const d = parseInt(t.fecha.substring(0, 2), 10);
            if (!isNaN(d)) t.dia = d;
        }}
        if (t.dia == null) t.dia = {dia_label};
        t.dia_label = {dia_label};
    }}
    return JSON.stringify(turnos);
}})()
"""
    result = await _cdp_eval(ws_url, js, timeout=15.0)
    raw = result.get("value", "[]")
    try:
        return json.loads(raw)
    except Exception:
        return []


async def buscar_turnos_en_rango(
    especialidad: str,
    profesional: str,
    mes_desde: str,
    anio_desde: int,
    dia_desde: int,
    mes_hasta: str,
    anio_hasta: int,
    dia_hasta: int,
) -> dict:
    """
    Busca turnos disponibles en un rango de fechas INCLUSIVO que puede cruzar meses.

    - Realiza UN solo _reiniciar_y_buscar (form + submit).
    - Navega a cada mes del rango y recolecta.
    - Filtra con semántica inclusiva:
        * mes_desde: dia >= dia_desde
        * mes_hasta: dia <= dia_hasta
        * meses intermedios: todos
    - Ordena cronológicamente (mes, dia, hora) y retorna el más temprano como turno_cercano.
    - Reutiliza toda la infraestructura existente de navegación y extracción.

    Retorna el mismo contrato que buscar_turno_mas_cercano.
    """
    ws_url = await ensure_session()
    if not ws_url:
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": "No se pudo establecer sesión CDP"}

    search_result, ws_url = await _reiniciar_y_buscar(ws_url, especialidad, profesional)

    if search_result == "SESSION_EXPIRED":
        log.info("Sesión expirada en rango, re-estableciendo...")
        ws_url = await ensure_session()
        if not ws_url:
            return {"encontrado": False, "turnos": [], "turno_cercano": None,
                    "error": "Re-login fallido tras sesión expirada"}
        search_result, ws_url = await _reiniciar_y_buscar(ws_url, especialidad, profesional)

    if search_result.startswith("ERROR"):
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": search_result}

    # Cerrar avisos que puedan haber aparecido después del submit (anteojos etc.)
    if await _dismiss_blocking_dialogs(ws_url):
        log.info("Dialog informativo cerrado antes de navegar meses")
        await asyncio.sleep(0.8)

    # Determinar meses a visitar (simple para rango Jun-Jul u otro par consecutivo)
    m_desde_num = _mes_a_num(mes_desde)
    m_hasta_num = _mes_a_num(mes_hasta)

    meses_a_visitar = []
    if anio_desde == anio_hasta and m_desde_num and m_hasta_num and m_desde_num <= m_hasta_num:
        for num in range(m_desde_num, m_hasta_num + 1):
            nombre = MESES_INV.get(num, mes_desde)
            meses_a_visitar.append((nombre, anio_desde))
    else:
        # fallback: visitar explícitamente los dos extremos (raro)
        meses_a_visitar = [(mes_desde, anio_desde), (mes_hasta, anio_hasta)]

    todos_los_turnos: list[dict] = []

    for mes_n, an in meses_a_visitar:
        nav_result = await _navegar_a_mes(ws_url, mes_n, an)
        if nav_result == "NO_AGENDA":
            continue
        if nav_result.startswith("ERROR"):
            log.warning("Navegación a %s %s falló: %s", mes_n, an, nav_result)
            continue

        dias = await _obtener_dias_disponibles(ws_url)
        for dia_label in sorted(dias):
            horarios = await _extraer_horarios_dia(ws_url, dia_label)
            for h in horarios:
                if h.get("dia") is None:
                    h["dia"] = _parse_dia_from_fecha(h.get("fecha")) or _dia_desde_fecha(h.get("fecha")) or dia_label
                # Enriquecer para filtro y orden multi-mes
                h["mes"] = mes_n
                h["anio"] = an
                h["_mes_num"] = _mes_a_num(mes_n) or _parse_mes_from_fecha(h.get("fecha")) or 0
            todos_los_turnos.extend(horarios)

    # Filtro INCLUSIVO por rango
    filtrados = []
    for t in todos_los_turnos:
        d = int(t.get("dia") or 0)
        mn = int(t.get("_mes_num") or 0)
        if mn == m_desde_num and d < dia_desde:
            continue
        if mn == m_hasta_num and d > dia_hasta:
            continue
        filtrados.append(t)

    if not filtrados:
        return {
            "encontrado": False,
            "turnos": [],
            "turno_cercano": None,
            "error": None,
            "mensaje": f"Sin turnos disponibles entre el {dia_desde} de {mes_desde} y el {dia_hasta} de {mes_hasta} {anio_hasta}",
        }

    # Orden cronológico real
    filtrados.sort(key=lambda t: (t.get("_mes_num", 99), t.get("dia", 99), t.get("hora", "99:99")))
    cercano = filtrados[0]

    return {
        "encontrado": True,
        "turnos": filtrados,
        "turno_cercano": cercano,
        "error": None,
    }


async def buscar_turno_mas_cercano(
    especialidad: str,
    profesional: str,
    mes: str,
    anio: int,
    min_dia: int | None = None,
) -> dict:
    """
    Flujo completo de búsqueda:
      1. Asegurar sesión
      2. Reiniciar búsqueda + llenar formulario + Buscar
      3. Navegar al mes objetivo
      4. Si hay días disponibles, extraer horarios (por label de botón del calendario)
      5. (Opcional) Filtrar turnos con dia > min_dia
      6. Retornar el turno más cercano (entre los válidos)

    IMPORTANTE: El campo 'dia' de cada turno se deriva del string 'fecha' que el portal
    reporta en la sección "Turnos Disponibles" (ej. '15-JUN-26' → dia=15). El label del
    botón del calendario solo se usa para localizar y hacer click. Esto garantiza que
    el filtro min_dia y la selección del turno más cercano respeten el día real del turno
    solicitado por el usuario.

    Retorna: {"encontrado": bool, "turnos": [...], "turno_cercano": {...} | None, "error": str | None, "mensaje": str | None}
    Si min_dia se provee, solo se consideran y notifican turnos con día > min_dia.
    Cada turno incluye además 'dia_label' (el número usado para clickear el botón del calendario)
    para diagnóstico.
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

    # Cerrar avisos informativos que bloquean el calendario (ej. anteojos)
    if await _dismiss_blocking_dialogs(ws_url):
        log.info("Dialog informativo cerrado antes de navegar al mes objetivo")
        await asyncio.sleep(0.8)

    # Paso 3: navegar al mes
    nav_result = await _navegar_a_mes(ws_url, mes, anio)
    if nav_result == "NO_AGENDA":
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": f"No hay agenda en {mes} {anio}"}
    if nav_result.startswith("ERROR"):
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": nav_result}

    # Paso 4: días disponibles (labels de botones del calendario del mes actual)
    dias = await _obtener_dias_disponibles(ws_url)
    if not dias:
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": f"Sin turnos disponibles en {mes} {anio}"}

    # Paso 5: extraer horarios de cada día
    # Clave del fix: _extraer_horarios_dia deriva 'dia' desde la 'fecha' real del portal (no del label).
    # Usamos el label solo para el clic y como fallback / diagnóstico (dia_label).
    todos_los_turnos = []
    for dia_label in sorted(dias):
        horarios = await _extraer_horarios_dia(ws_url, dia_label)
        for h in horarios:
            if h.get("dia") is None:
                h["dia"] = _dia_desde_fecha(h.get("fecha")) or dia_label
        todos_los_turnos.extend(horarios)

    # Diagnóstico de consistencia (útil para analizar si el proceso identificaba bien el día solicitado).
    # Muestra mismatches entre el label del botón del calendario que se clickeó vs. el día contenido en la 'fecha' del turno.
    if min_dia is not None or len(todos_los_turnos) > 0:
        for t in todos_los_turnos:
            real = _dia_desde_fecha(t.get("fecha"))
            label = t.get("dia_label")
            if real is not None and label is not None and real != label:
                log.info("DIA_MISMATCH: label_calendario=%s vs dia_real_fecha=%s (fecha=%s)", label, real, t.get("fecha"))

    # Filtro por día mínimo. Usa el 'dia' real derivado de la 'fecha' que el portal reporta para el turno.
    if min_dia is not None:
        todos_los_turnos = [t for t in todos_los_turnos if t.get("dia", 0) > min_dia]

    if not todos_los_turnos:
        if min_dia is not None:
            return {"encontrado": False, "turnos": [], "turno_cercano": None,
                    "error": None, "mensaje": f"Sin turnos disponibles posteriores al día {min_dia} en {mes} {anio}"}
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": "Días marcados pero sin horarios"}

    # Filtrar solo turnos del mes y año solicitados (safety net si el portal redirige a otro mes)
    mes_abrev = MESES_ABREV.get(mes, "???")
    anio_sufijo = str(anio)[-2:]
    patron_mes = f"-{mes_abrev}-{anio_sufijo}"
    todos_los_turnos = [t for t in todos_los_turnos if patron_mes in t.get("fecha", "")]

    if not todos_los_turnos:
        return {"encontrado": False, "turnos": [], "turno_cercano": None,
                "error": None, "mensaje": f"Sin turnos de {mes} {anio} (el portal mostró otro mes)"}

    # Ordenar por el día real (de la fecha) + hora y tomar el más cercano
    todos_los_turnos.sort(key=lambda t: (t.get("dia", 99), t.get("hora", "99:99")))
    cercano = todos_los_turnos[0]

    return {
        "encontrado": True,
        "turnos": todos_los_turnos,
        "turno_cercano": cercano,
        "error": None,
    }


async def buscar_turnos_multiples_profesionales(
    especialidad: str,
    profesionales: list[str],
    mes: str,
    anio: int,
    fechas_excluidas: list[int] | None = None,
) -> dict:
    """
    Busca turnos para múltiples profesionales y fusiona resultados.
    Excluye fechas especificadas (por defecto, día 10).
    """
    if fechas_excluidas is None:
        fechas_excluidas = [10]

    todos_los_turnos = []
    errores = []

    for profesional in profesionales:
        log.info("Buscando turnos para %s...", profesional)
        resultado = await buscar_turno_mas_cercano(especialidad, profesional, mes, anio)
        if resultado.get("encontrado"):
            todos_los_turnos.extend(resultado.get("turnos", []))
        elif resultado.get("error"):
            errores.append(f"{profesional}: {resultado['error']}")
        else:
            log.info("Sin turnos para %s: %s", profesional, resultado.get("mensaje", "sin datos"))

    if not todos_los_turnos:
        error_msg = "; ".join(errores) if errores else "Sin turnos disponibles"
        return {
            "encontrado": False,
            "turnos": [],
            "turno_cercano": None,
            "error": error_msg if errores else None,
            "mensaje": "No se encontraron turnos disponibles",
        }

    # Eliminar duplicados por fecha+hora+lugar
    vistos = set()
    turnos_unicos = []
    for turno in todos_los_turnos:
        key = (turno.get("fecha"), turno.get("hora"), turno.get("lugar"))
        if key not in vistos:
            vistos.add(key)
            turnos_unicos.append(turno)

    # Filtrar fechas excluidas
    turnos_filtrados = [t for t in turnos_unicos if t.get("dia") not in fechas_excluidas]

    if not turnos_filtrados:
        return {
            "encontrado": False,
            "turnos": [],
            "turno_cercano": None,
            "error": None,
            "mensaje": f"No se encontraron turnos disponibles (excluido día 10 de {mes})",
        }

    # Ordenar y seleccionar el más cercano
    turnos_filtrados.sort(key=lambda t: (t.get("dia", 99), t.get("hora", "99:99")))
    cercano = turnos_filtrados[0]

    return {
        "encontrado": True,
        "turnos": turnos_filtrados,
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
