#!/usr/bin/env bash
# Ejemplo: 3 bots en paralelo, cada uno con su Chrome (puerto + perfil).
#
# Uso:
#   chmod +x multi-session.example.sh
#   ./multi-session.example.sh          # lanza los 3 en background
#   ./multi-session.example.sh --fg 1   # solo imprime los comandos (no ejecuta)
#
# Requisitos: .env con TU_PORTAL_USER / TU_PORTAL_PASS (y Telegram opcional).
# Cada sesión debe usar un CDP_PORT distinto y un CDP_PROFILE distinto.
# Con --session, el perfil se deriva solo; el puerto es mejor fijarlo a mano
# para evitar colisiones de hash.

set -euo pipefail
cd "$(dirname "$0")"

# Activar venv si existe
if [[ -f venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
elif [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

DRY_RUN=0
if [[ "${1:-}" == "--fg" ]] || [[ "${1:-}" == "--print" ]]; then
  DRY_RUN=1
fi

# Preferir el python del venv / python3
if command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  PYTHON=python3
fi

run_bot() {
  local session="$1"
  local port="$2"
  shift 2
  local cmd=(
    "$PYTHON" bot.py
    --session "$session"
    --cdp-port "$port"
    "$@"
  )
  echo "── Sesión '$session'  (CDP_PORT=$port) ──"
  printf '  %q' "${cmd[@]}"
  echo
  if [[ "$DRY_RUN" -eq 1 ]]; then
    return 0
  fi
  # Log por sesión + background
  mkdir -p logs
  nohup "${cmd[@]}" >"logs/bot-${session}.log" 2>&1 &
  echo "  PID $!  → logs/bot-${session}.log"
}

echo "=== Multi-sesión Tu Portal Bot ==="
echo "Cada proceso abre su propio Chrome (no compartir puerto/perfil)."
echo

# ── Terminal 1 / proceso A: Traumatología ──────────────────────────────────
run_bot traumato 9223 \
  --especialidad "TRAUMATOLOGIA TOBILLO/PIE" \
  --profesional "EQUIPO DE TRAUMATOLOGIA DR ROFRANO" \
  --mes-desde Junio --dia-desde 30 \
  --mes-hasta Julio --dia-hasta 16 \
  --anio 2026 \
  --intervalo 10

# ── Terminal 2 / proceso B: Clínica médica ────────────────────────────────
run_bot clinica 9224 \
  --especialidad "CLINICA MEDICA" \
  --profesional "Garcia" \
  --mes-desde Julio --dia-desde 1 \
  --mes-hasta Julio --dia-hasta 31 \
  --anio 2026 \
  --intervalo 15

# ── Terminal 3 / proceso C: Dermatología ──────────────────────────────────
run_bot dermato 9225 \
  --especialidad "DERMATOLOGIA" \
  --profesional "Rusiñol" \
  --mes-desde Julio --dia-desde 1 \
  --mes-hasta Agosto --dia-hasta 15 \
  --anio 2026 \
  --intervalo 15

echo
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "(dry-run: no se lanzó nada)"
  echo "Copiá cada bloque a su propia terminal, o corré sin --print para background."
else
  echo "Bots en background. Para seguir logs:"
  echo "  tail -f logs/bot-traumato.log logs/bot-clinica.log logs/bot-dermato.log"
  echo "Para detener:"
  echo "  pkill -f 'user-data-dir=/tmp/tu-portal-cdp-'"
  echo "  # o por sesión: pkill -f 'user-data-dir=/tmp/tu-portal-cdp-traumato'"
fi
