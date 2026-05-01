"""Servidor WebSocket local para controlar 'Tu Portal'."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request

from dotenv import load_dotenv
from websockets.asyncio.server import serve

import app_controller as ac

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tu-portal-server")

HOST = "127.0.0.1"
PORT = 8765


async def _stream_logs(websocket, stop_event: asyncio.Event):
    try:
        async for stream, line in ac.tail_logs():
            if stop_event.is_set():
                break
            await websocket.send(json.dumps({"type": "log", "stream": stream, "line": line}))
    except Exception as e:
        await websocket.send(json.dumps({"type": "error", "message": f"log stream: {e}"}))


def _enviar_telegram(texto: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": texto, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data, timeout=10)
    except Exception as e:
        log.error("Error enviando Telegram: %s", e)


# Estado global del bot
_bot_task: asyncio.Task | None = None
_bot_stop = asyncio.Event()
_bot_ciclo = 0
_bot_ultimo: dict = {}


async def _bot_loop(websocket, especialidad, profesional, mes, anio, intervalo):
    global _bot_ciclo, _bot_ultimo
    _bot_ciclo = 0
    while not _bot_stop.is_set():
        _bot_ciclo += 1
        try:
            resultado = await ac.buscar_turno_mas_cercano(especialidad, profesional, mes, anio)
            _bot_ultimo = resultado
            await websocket.send(json.dumps({
                "type": "bot_tick", "ciclo": _bot_ciclo, **resultado
            }))
            if resultado.get("encontrado"):
                # Notificación macOS
                t = resultado.get("turno_cercano", {})
                msg = f"{t.get('fecha','')} {t.get('hora','')} - {t.get('profesional','')}"
                import subprocess
                subprocess.run([
                    "osascript", "-e",
                    f'display notification "{msg}" with title "TURNO ENCONTRADO" sound name "Glass"'
                ], capture_output=True)
                # Telegram
                tg = (f"🏥 <b>TURNO ENCONTRADO</b>\n\n"
                      f"📅 {t.get('fecha','')} 🕐 {t.get('hora','')}\n"
                      f"👨‍⚕️ {t.get('profesional','')}\n"
                      f"📍 {t.get('lugar','')}")
                _enviar_telegram(tg)
        except Exception as e:
            log.error("bot ciclo %d error: %s", _bot_ciclo, e)
            await websocket.send(json.dumps({
                "type": "bot_tick", "ciclo": _bot_ciclo,
                "encontrado": False, "error": str(e)
            }))
        try:
            await asyncio.wait_for(_bot_stop.wait(), timeout=intervalo)
            break  # stop fue señalado
        except asyncio.TimeoutError:
            pass  # timeout normal → siguiente ciclo


async def handle(websocket):
    global _bot_task, _bot_stop

    peer = websocket.remote_address[0] if websocket.remote_address else "?"
    if peer != "127.0.0.1":
        log.warning("rechazando conexion no-local: %s", peer)
        await websocket.close(code=1008, reason="local only")
        return

    log.info("cliente conectado: %s", peer)
    log_task: asyncio.Task | None = None
    log_stop = asyncio.Event()

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send(json.dumps({"type": "error", "message": "json invalido"}))
                continue

            action = msg.get("action")
            try:
                if action == "open":
                    result = await ac.open_and_login()
                    await websocket.send(json.dumps({"type": "ack", "action": "open", "ok": True, **result}))

                elif action == "login":
                    ok = await ac.login()
                    await websocket.send(json.dumps({"type": "ack", "action": "login", "ok": ok}))

                elif action == "close":
                    ok = ac.close_app()
                    await websocket.send(json.dumps({"type": "ack", "action": "close", "ok": ok}))

                elif action == "status":
                    running, pid = ac.is_running()
                    await websocket.send(json.dumps({"type": "status", "running": running, "pid": pid}))

                elif action == "debug_ui":
                    out = await ac.get_ui_fields()
                    await websocket.send(json.dumps({"type": "ack", "action": "debug_ui", "fields": out}))

                elif action == "reservar_turno":
                    especialidad = msg.get("especialidad", "")
                    profesional  = msg.get("profesional", "")
                    if not especialidad or not profesional:
                        await websocket.send(json.dumps({
                            "type": "error",
                            "message": 'Se requieren los campos "especialidad" y "profesional"'
                        }))
                    else:
                        result = await ac.reservar_turno(especialidad, profesional)
                        await websocket.send(json.dumps({
                            "type": "ack", "action": "reservar_turno", **result
                        }))

                elif action == "applescript":
                    script = msg.get("script", "")
                    out = ac.run_applescript_sync(script)
                    await websocket.send(json.dumps({"type": "ack", "action": "applescript", "ok": True, "output": out}))

                elif action == "logs_subscribe":
                    if log_task is None or log_task.done():
                        log_stop = asyncio.Event()
                        log_task = asyncio.create_task(_stream_logs(websocket, log_stop))
                        await websocket.send(json.dumps({"type": "ack", "action": "logs_subscribe", "ok": True}))
                    else:
                        await websocket.send(json.dumps({"type": "ack", "action": "logs_subscribe", "ok": True, "note": "ya suscripto"}))

                elif action == "logs_unsubscribe":
                    if log_task and not log_task.done():
                        log_stop.set()
                        log_task.cancel()
                    await websocket.send(json.dumps({"type": "ack", "action": "logs_unsubscribe", "ok": True}))

                elif action == "bot_start":
                    esp  = msg.get("especialidad", "DERMATOLOGIA")
                    pro  = msg.get("profesional", "Rusiñol")
                    mes  = msg.get("mes", "Mayo")
                    anio = msg.get("anio", 2026)
                    intervalo = msg.get("intervalo", 30)
                    if _bot_task and not _bot_task.done():
                        _bot_stop.set()
                        _bot_task.cancel()
                        await asyncio.sleep(0.5)
                    _bot_stop = asyncio.Event()
                    _bot_task = asyncio.create_task(
                        _bot_loop(websocket, esp, pro, mes, anio, intervalo)
                    )
                    await websocket.send(json.dumps({
                        "type": "ack", "action": "bot_start", "ok": True,
                        "config": {"especialidad": esp, "profesional": pro,
                                   "mes": mes, "anio": anio, "intervalo": intervalo}
                    }))

                elif action == "bot_stop":
                    if _bot_task and not _bot_task.done():
                        _bot_stop.set()
                        _bot_task.cancel()
                    await websocket.send(json.dumps({
                        "type": "ack", "action": "bot_stop", "ok": True,
                        "ciclos": _bot_ciclo
                    }))

                elif action == "bot_status":
                    running = _bot_task is not None and not _bot_task.done()
                    await websocket.send(json.dumps({
                        "type": "ack", "action": "bot_status",
                        "running": running, "ciclo": _bot_ciclo,
                        "ultimo": _bot_ultimo
                    }))

                else:
                    await websocket.send(json.dumps({"type": "error", "message": f"action desconocida: {action}"}))

            except Exception as e:
                log.exception("error procesando %s", action)
                await websocket.send(json.dumps({"type": "error", "message": str(e)}))
    finally:
        if log_task and not log_task.done():
            log_stop.set()
            log_task.cancel()
        log.info("cliente desconectado: %s", peer)


async def main():
    log.info("Listening on ws://%s:%d", HOST, PORT)
    async with serve(handle, HOST, PORT):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
