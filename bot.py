"""
Bot de búsqueda de turnos — Hospital Alemán, Tu Portal.

Busca turnos cada INTERVALO segundos y notifica cuando encuentra uno.

Uso:
    python bot.py
    python bot.py --mes Junio --anio 2026
    python bot.py --especialidad "CLINICA MEDICA" --profesional "Garcia" --intervalo 60
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime

# Asegurar que importamos desde el directorio del script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import urllib.request
import urllib.parse

import app_controller as ac

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


def enviar_telegram(texto: str) -> None:
    """Envía un mensaje por Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": texto, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(url, data, timeout=10)
        log.info("Telegram enviado OK")
    except Exception as e:
        log.error("Error enviando Telegram: %s", e)


def notificar(turno: dict, todos: list[dict]) -> None:
    """Notificación sonora + macOS + Telegram + consola."""
    fecha = turno.get("fecha", "?")
    hora  = turno.get("hora", "?")
    prof  = turno.get("profesional", "?")
    lugar = turno.get("lugar", "?")

    msg = f"TURNO: {fecha} {hora} - {prof} - {lugar}"

    # Notificaciones macOS (solo en Mac)
    if platform.system() == "Darwin":
        escaped = msg.replace('"', '\\"')
        subprocess.run([
            "osascript", "-e",
            f'display notification "{escaped}" with title "Tu Portal Bot" sound name "Glass"'
        ], capture_output=True)
        import time
        for _ in range(3):
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)

    # Telegram (siempre — funciona en Mac y servidor)
    tg_msg = f"🏥 <b>TURNO ENCONTRADO</b>\n\n📅 {fecha} 🕐 {hora}\n👨‍⚕️ {prof}\n📍 {lugar}"
    if len(todos) > 1:
        tg_msg += f"\n\nTotal disponibles: {len(todos)}"
        for t in todos:
            tg_msg += f"\n  • Día {t.get('dia')}: {t.get('hora')} ({t.get('lugar', '')})"
    enviar_telegram(tg_msg)

    # Consola
    print()
    print("=" * 60)
    print(f"  TURNO ENCONTRADO!")
    print(f"  {msg}")
    if len(todos) > 1:
        print(f"\n  Total de turnos disponibles: {len(todos)}")
        for t in todos:
            print(f"    - Día {t.get('dia')}: {t.get('hora')} ({t.get('lugar', '')})")
    print("=" * 60)
    print()


async def main():
    parser = argparse.ArgumentParser(description="Bot de búsqueda de turnos")
    parser.add_argument("--especialidad", default="DERMATOLOGIA")
    parser.add_argument("--profesional", default="Rusiñol")
    parser.add_argument("--mes", default="Mayo")
    parser.add_argument("--anio", type=int, default=2026)
    parser.add_argument("--intervalo", type=int, default=30,
                        help="Segundos entre cada búsqueda")
    parser.add_argument("--no-parar", action="store_true",
                        help="No detenerse al encontrar turno, seguir buscando")
    args = parser.parse_args()

    log.info("Bot iniciado")
    log.info("  Especialidad: %s", args.especialidad)
    log.info("  Profesional:  %s", args.profesional)
    log.info("  Mes objetivo: %s %d", args.mes, args.anio)
    log.info("  Intervalo:    %ds", args.intervalo)
    print()

    ciclo = 0
    errores_seguidos = 0

    while True:
        ciclo += 1
        hora = datetime.now().strftime("%H:%M:%S")
        log.info("--- Ciclo %d [%s] ---", ciclo, hora)

        try:
            resultado = await ac.buscar_turno_mas_cercano(
                especialidad=args.especialidad,
                profesional=args.profesional,
                mes=args.mes,
                anio=args.anio,
            )
            errores_seguidos = 0

            if resultado["encontrado"]:
                notificar(resultado["turno_cercano"], resultado["turnos"])
                if not args.no_parar:
                    log.info("Bot detenido (turno encontrado). "
                             "Usá --no-parar para seguir buscando.")
                    break
            else:
                msg = resultado.get("mensaje") or resultado.get("error") or "Sin resultado"
                log.info("  %s", msg)

        except Exception as e:
            errores_seguidos += 1
            log.error("  Error en ciclo %d: %s", ciclo, e)
            if errores_seguidos >= 3:
                log.warning("  3 errores seguidos, reiniciando Chrome...")
                ac.close_app()
                await asyncio.sleep(2.0)
                errores_seguidos = 0

        log.info("  Esperando %ds...", args.intervalo)
        await asyncio.sleep(args.intervalo)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot detenido por el usuario.")
