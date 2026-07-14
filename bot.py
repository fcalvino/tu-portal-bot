"""
Bot de búsqueda de turnos — Hospital Alemán, Tu Portal.

Busca turnos cada INTERVALO segundos y notifica cuando encuentra uno.

Uso simple:
    python bot.py
    python bot.py --mes-desde Junio --dia-desde 30 --mes-hasta Julio --dia-hasta 16 --anio 2026
    python bot.py --especialidad "TRAUMATOLOGIA TOBILLO/PIE" --profesional "EQUIPO DE TRAUMATOLOGIA DR ROFRANO" --mes-desde Junio --dia-desde 30 --mes-hasta Julio --dia-hasta 16
    python bot.py --especialidad "CLINICA MEDICA" --profesional "Garcia" --intervalo 60

Multi-sesión (varias terminales en paralelo, cada una con su Chrome):
    # Terminal 1
    python bot.py --session traumato --cdp-port 9223 \\
      --especialidad "TRAUMATOLOGIA TOBILLO/PIE" \\
      --profesional "EQUIPO DE TRAUMATOLOGIA DR ROFRANO" \\
      --mes-desde Junio --dia-desde 30 --mes-hasta Julio --dia-hasta 16

    # Terminal 2
    python bot.py --session clinica --cdp-port 9224 \\
      --especialidad "CLINICA MEDICA" --profesional "Garcia" \\
      --mes-desde Julio --dia-desde 1 --mes-hasta Julio --dia-hasta 31

    # Terminal 3 (puerto/perfil derivados de --session si no pasás --cdp-port)
    python bot.py --session dermato \\
      --especialidad "DERMATOLOGIA" --profesional "Rusiñol" \\
      --mes-desde Julio --dia-desde 1 --mes-hasta Agosto --dia-hasta 15

También con env vars:
    SESSION_ID=traumato CDP_PORT=9223 CDP_PROFILE=/tmp/tu-portal-cdp-traumato python bot.py ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

# Asegurar que importamos desde el directorio del script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import urllib.request
import urllib.parse

import app_controller as ac

# Session label se rellena en main() tras parsear --session
_SESSION_LABEL = ""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")


def _session_prefix() -> str:
    if _SESSION_LABEL:
        return f"[{_SESSION_LABEL}] "
    return ""

# ---------------------------------------------------------------------------
# Alerta de crédito Railway
# ---------------------------------------------------------------------------
_BOT_START_TIME = time.time()
_CREDIT_ALERT_SENT = False
_CREDIT_ALERT_DAYS = 18  # ~$3.60 de $5 → avisa cuando queda ~$1.40


def _query_railway_usage(token: str) -> float | None:
    """Consulta el uso actual en Railway via GraphQL. Retorna USD o None."""
    query = json.dumps({"query": "{ me { usage { currentPeriodTotalUsage } } }"})
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=query.encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    raw = urllib.request.urlopen(req, timeout=10).read()
    data = json.loads(raw)
    return data["data"]["me"]["usage"]["currentPeriodTotalUsage"]


def _enviar_alerta_credito(detalle: str) -> None:
    tg = (f"⚠️ <b>Crédito Railway bajo</b>\n\n"
          f"{detalle}\n\n"
          f"Verificá tu saldo en railway.app/account/billing\n"
          f"El bot puede detenerse pronto.")
    enviar_telegram(tg)
    log.warning("Alerta de crédito Railway enviada: %s", detalle)


def _check_railway_credit() -> None:
    """Envía alerta Telegram si el crédito de Railway está por agotarse."""
    global _CREDIT_ALERT_SENT
    if _CREDIT_ALERT_SENT:
        return

    # Nivel 1: consulta real a la API si hay token
    token = os.environ.get("RAILWAY_API_TOKEN", "")
    if token:
        try:
            usage = _query_railway_usage(token)
            if usage is not None and usage >= 4.0:  # queda < $1
                _enviar_alerta_credito(f"${usage:.2f} usados de $5.00")
                _CREDIT_ALERT_SENT = True
                return
            return  # API funcionó y no hay problema todavía
        except Exception:
            pass  # fallback a tiempo

    # Nivel 2 (fallback): alerta por días transcurridos
    dias = (time.time() - _BOT_START_TIME) / 86400
    if dias >= _CREDIT_ALERT_DAYS:
        _enviar_alerta_credito(
            f"El bot lleva {dias:.0f} días corriendo (~${dias * 0.20:.2f} estimados de $5.00)"
        )
        _CREDIT_ALERT_SENT = True


# ---------------------------------------------------------------------------


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
    prefix = _session_prefix()

    msg = f"{prefix}TURNO: {fecha} {hora} - {prof} - {lugar}"
    title = f"Tu Portal Bot{_SESSION_LABEL and f' [{_SESSION_LABEL}]' or ''}"

    # Notificaciones macOS (solo en Mac)
    if platform.system() == "Darwin":
        escaped = msg.replace('"', '\\"')
        title_esc = title.replace('"', '\\"')
        subprocess.run([
            "osascript", "-e",
            f'display notification "{escaped}" with title "{title_esc}" sound name "Glass"'
        ], capture_output=True)
        for _ in range(3):
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Glass.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)

    # Telegram (siempre — funciona en Mac y servidor)
    session_line = f"\n🏷️ Sesión: <code>{_SESSION_LABEL}</code>" if _SESSION_LABEL else ""
    tg_msg = (
        f"🏥 <b>TURNO ENCONTRADO</b>{session_line}\n\n"
        f"📅 {fecha} 🕐 {hora}\n👨‍⚕️ {prof}\n📍 {lugar}"
    )
    if len(todos) > 1:
        tg_msg += f"\n\nTotal disponibles: {len(todos)}"
        for t in todos:
            tg_msg += f"\n  • Día {t.get('dia')}: {t.get('hora')} ({t.get('lugar', '')})"
    enviar_telegram(tg_msg)

    # Consola
    print()
    print("=" * 60)
    print(f"  {prefix}TURNO ENCONTRADO!")
    print(f"  {msg}")
    if len(todos) > 1:
        print(f"\n  Total de turnos disponibles: {len(todos)}")
        for t in todos:
            print(f"    - Día {t.get('dia')}: {t.get('hora')} ({t.get('lugar', '')})")
    print("=" * 60)
    print()


async def main():
    parser = argparse.ArgumentParser(
        description="Bot de búsqueda de turnos (soporta multi-sesión en paralelo)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Multi-sesión: cada terminal necesita su propio Chrome (puerto + perfil distintos).

  Terminal 1:
    python bot.py --session traumato --cdp-port 9223 \\
      --especialidad "TRAUMATOLOGIA TOBILLO/PIE" \\
      --profesional "EQUIPO DE TRAUMATOLOGIA DR ROFRANO"

  Terminal 2:
    python bot.py --session clinica --cdp-port 9224 \\
      --especialidad "CLINICA MEDICA" --profesional "Garcia"

  Terminal 3:
    python bot.py --session dermato --cdp-port 9225 \\
      --especialidad "DERMATOLOGIA" --profesional "Rusiñol"

Sin --cdp-port, el puerto se deriva de --session (rango 9223-9322).
Ver también: multi-session.example.sh
""".strip(),
    )
    parser.add_argument("--especialidad", default="TRAUMATOLOGIA TOBILLO/PIE")
    parser.add_argument("--profesional", default=None, action="append",
                        help="Profesionales a buscar (repetir para múltiples)")

    # Multi-sesión / aislamiento de Chrome
    parser.add_argument(
        "--session", default=None,
        help="ID de sesión (etiqueta logs/Telegram; deriva perfil y puerto si no se pasan)",
    )
    parser.add_argument(
        "--cdp-port", type=int, default=None,
        help="Puerto remote-debugging de Chrome (default: 9223 o derivado de --session)",
    )
    parser.add_argument(
        "--cdp-profile", default=None,
        help="user-data-dir de Chrome (default: /tmp/tu-portal-cdp[-SESSION])",
    )

    # Rango de fechas (preferido, soporta cruce de meses)
    parser.add_argument("--mes-desde", default="Junio")
    parser.add_argument("--dia-desde", type=int, default=30)
    parser.add_argument("--mes-hasta", default="Julio")
    parser.add_argument("--dia-hasta", type=int, default=16)
    parser.add_argument("--anio", type=int, default=2026)

    # Legacy single-month (sigue funcionando)
    parser.add_argument("--mes", default=None, help="Legacy: mes único (usa --mes-desde/--mes-hasta en su lugar)")
    parser.add_argument("--min-dia", type=int, default=None,
                        help="Legacy: día mínimo exclusivo dentro de un solo mes")

    parser.add_argument("--intervalo", type=int, default=5,
                        help="Segundos entre cada búsqueda")
    parser.add_argument("--no-parar", action="store_true",
                        help="No detenerse al encontrar turno, seguir buscando")
    args = parser.parse_args()

    # Configurar aislamiento CDP antes de tocar Chrome
    global _SESSION_LABEL
    ac.configure_cdp(
        port=args.cdp_port,
        profile=args.cdp_profile,
        session_id=args.session or os.environ.get("SESSION_ID") or None,
    )
    _SESSION_LABEL = ac.SESSION_ID or ""
    if _SESSION_LABEL:
        log.name = f"bot.{_SESSION_LABEL}"

    # Determinar si buscar múltiples o un solo profesional
    if args.profesional and len(args.profesional) > 1:
        profesionales = args.profesional
        usar_multiples = True
    elif args.profesional:
        profesionales = args.profesional
        usar_multiples = False
    else:
        profesionales = ["EQUIPO DE TRAUMATOLOGIA DR ROFRANO"]
        usar_multiples = False

    log.info("Bot iniciado%s", f" (sesión={_SESSION_LABEL})" if _SESSION_LABEL else "")
    log.info("  CDP:          port=%s profile=%s", ac.CDP_PORT, ac.CDP_PROFILE)
    log.info("  Especialidad: %s", args.especialidad)
    if usar_multiples:
        log.info("  Profesionales: %s", ", ".join(profesionales))
    else:
        log.info("  Profesional:  %s", profesionales[0])
    if args.mes_desde and args.mes_hasta and not args.mes:
        log.info("  Rango objetivo: %s %d → %s %d (año %d)",
                 args.mes_desde, args.dia_desde, args.mes_hasta, args.dia_hasta, args.anio)
    else:
        log.info("  Mes objetivo: %s %d", args.mes or args.mes_desde or "Junio", args.anio)
    if args.min_dia is not None:
        log.info("  (legacy min_dia: >%d)", args.min_dia)
    log.info("  Intervalo:    %ds", args.intervalo)
    if usar_multiples:
        log.info("  Día 10 excluido: Sí")
    print()

    ciclo = 0
    errores_seguidos = 0

    while True:
        ciclo += 1
        hora = datetime.now().strftime("%H:%M:%S")
        log.info("--- Ciclo %d [%s] ---", ciclo, hora)
        _check_railway_credit()

        try:
            if usar_multiples:
                # Multi-profesional usa mes único (legacy API)
                mes = args.mes or args.mes_desde or "Junio"
                resultado = await ac.buscar_turnos_multiples_profesionales(
                    especialidad=args.especialidad,
                    profesionales=profesionales,
                    mes=mes,
                    anio=args.anio,
                    fechas_excluidas=[10],
                )
            elif args.mes_desde and args.mes_hasta and not args.mes:
                # Buscador por rango (incluye 30 Jun - 16 Jul)
                resultado = await ac.buscar_turnos_en_rango(
                    especialidad=args.especialidad,
                    profesional=profesionales[0],
                    mes_desde=args.mes_desde,
                    anio_desde=args.anio,
                    dia_desde=args.dia_desde,
                    mes_hasta=args.mes_hasta,
                    anio_hasta=args.anio,
                    dia_hasta=args.dia_hasta,
                )
            else:
                # Fallback legacy (mes único)
                resultado = await ac.buscar_turno_mas_cercano(
                    especialidad=args.especialidad,
                    profesional=profesionales[0],
                    mes=args.mes or args.mes_desde or "Junio",
                    anio=args.anio,
                    min_dia=args.min_dia,
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
