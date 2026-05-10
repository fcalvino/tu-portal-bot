# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Tu Portal Bot** — An appointment booking automation system for Hospital Alemán's online portal (Tu Portal). The bot monitors available appointment slots for specific specialties and professionals, notifies via Telegram/macOS, and can auto-book appointments.

**Tech Stack:**
- Python 3.12+
- Chrome/Chromium with Chrome DevTools Protocol (CDP) for browser automation
- WebSockets for server-client communication
- Telegram Bot API for notifications
- Deployed on Railway with Docker

## Architecture

### Core Components

**1. app_controller.py** — Business logic layer
- **Chrome automation via CDP**: Opens Tu Portal as a PWA, navigates forms, extracts data
- **Session management**: Login flow using JS injection to bypass field detection
- **Appointment search**: `buscar_turno_mas_cercano(especialidad, profesional, mes, anio)` → full search pipeline
  - Navigates specialty/professional selection
  - Switches to target month/year
  - Extracts available days and time slots
  - Returns sorted list of appointments + earliest one
- **Form interaction**: Fills dropdowns, submaries searches via CDP Runtime.evaluate
- **Process lifecycle**: `open_app()`, `close_app()`, `is_running()`

**2. server.py** — WebSocket server
- **Local-only server**: Binds to 127.0.0.1:8765, rejects remote connections
- **Actions** (JSON-RPC style):
  - `open` — Launch Tu Portal Chrome instance + perform initial login
  - `login` — Retry login after session loss
  - `close` — Kill Chrome process
  - `status` — Check if Tu Portal is running
  - `debug_ui` — Extract form field names for debugging
  - `reservar_turno` — Book an appointment (especialidad + profesional)
  - `bot_start` — Launch background appointment search loop (accepts especialidad, profesional, mes, anio, intervalo)
  - `bot_stop` — Stop background search
  - `bot_status` — Poll search loop state
  - `logs_subscribe` / `logs_unsubscribe` — Stream app output
- **Notifications**: When appointment found → macOS notification + Telegram alert (with date/time/professional/location)

**3. bot.py** — Standalone CLI bot
- Runs `buscar_turno_mas_cercano()` in a loop at fixed interval
- **Args**: `--mes`, `--anio`, `--especialidad`, `--profesional`, `--intervalo`
- **Railway credit monitoring**: Queries Railway API, sends Telegram alert when credit ≤ $1.40
- Entry point for Docker container (see Dockerfile)

### Data Flow

```
Tu Portal (website) 
    ↓ (Chrome + CDP)
app_controller ← JS injection, form filling, DOM parsing
    ↓
Server WebSocket ← Client sends "bot_start", receives "bot_tick" events
    ↓
Notifications ← Telegram API + macOS notifications
```

### Key JS Patterns in CDP

**Native value setter** (input fields):
```python
const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value'
).set;
setter.call(el, val);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
```
Used because form detection sometimes misses input elements; native setter + event dispatching ensures Angular/React forms register changes.

**Dynamic wait for elements** (e.g., login form, dropdown options):
```javascript
const deadline = Date.now() + 8000;
let element = null;
while (Date.now() < deadline) {
    element = document.querySelector('...');
    if (element) break;
    await new Promise(r => setTimeout(r, 200));
}
```
Hospital portal is a PWA with dynamic DOM changes; polling with 200ms intervals is safer than fixed sleeps.

## Development Commands

### Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your Tu Portal credentials and (optional) Telegram bot token
```

### Run Modes

**Standalone CLI bot** (searches appointments, handles Railway credit alerts, sends Telegram):
```bash
python bot.py
python bot.py --mes Junio --anio 2026 --especialidad "DERMATOLOGIA" --profesional "Rusiñol"
python bot.py --intervalo 60  # Search every 60 seconds
```

**WebSocket server** (exposes full control to clients):
```bash
python server.py
# Listens on ws://127.0.0.1:8765
```

**Development** (test Chrome automation):
```bash
# Open Tu Portal and debug UI fields
python -c "import asyncio, app_controller as ac; print(asyncio.run(ac.get_ui_fields()))"

# Check if Tu Portal is running
python -c "import app_controller as ac; print(ac.is_running())"
```

### Docker

```bash
# Build
docker build -t tu-portal-bot .

# Run (sets RAILWAY_API_TOKEN, TELEGRAM_BOT_TOKEN, TU_PORTAL_USER, TU_PORTAL_PASS via env)
docker run --env-file .env tu-portal-bot
```

## Configuration

**Environment variables** (.env):
- `TU_PORTAL_USER` — Hospital Alemán portal username (required)
- `TU_PORTAL_PASS` — Hospital Alemán portal password (required)
- `TU_PORTAL_APP_NAME` — App name for PWA window (default: "Tu Portal")
- `TELEGRAM_BOT_TOKEN` — Telegram bot token for notifications (optional)
- `TELEGRAM_CHAT_ID` — Telegram chat ID to notify (optional)
- `RAILWAY_API_TOKEN` — Railway API token for credit monitoring (optional)

## Common Tasks

### Add a New Appointment Search Feature

The search flow is in `buscar_turno_mas_cercano()`:
1. Ensure CDP session with `ensure_session()`
2. Fill specialty/professional dropdowns with `_reiniciar_y_buscar()`
3. Navigate month/year with `_navegar_a_mes()`
4. Extract days and times with `_obtener_dias_disponibles()` + `_extraer_horarios_dia()`

To add new search criteria (e.g., location, time range):
- Modify the JS injection code in `_reiniciar_y_buscar()` or add new helpers
- Update `buscar_turno_mas_cercano()` signature if exposing new params
- Update server.py `bot_start` action to pass new params

### Debug Appointment Search

1. Open Tu Portal manually: `python app_controller.py open_app()`
2. Extract form field names: `python -c "import asyncio, app_controller as ac; print(asyncio.run(ac.get_ui_fields()))"`
3. Inspect with CDP inspector:
   - In Chrome DevTools, open `chrome://inspect` → target the Tu Portal process
   - Run JS directly in Console to test DOM queries
4. Add logging: Search functions already log extensively; check output from server.py or bot.py

### Extend Notifications

Notifications are triggered in `server.py` `_bot_loop()`:
- **macOS**: Uses `osascript` for system notifications (Darwin-only)
- **Telegram**: Uses `_enviar_telegram()` helper

To add Slack/email/SMS:
1. Create a `_enviar_slack()` or similar helper
2. Call it in `_bot_loop()` alongside the existing Telegram alert
3. Add new env vars for credentials

## Deployment Notes

- Bot runs in Docker on Railway (see Dockerfile)
- Chrome is installed in the Debian container
- Profile dir at `/tmp/tu-portal-cdp-profile` stores Chrome prefs + login state
- Railway credit monitoring sends alerts every ~18 days (when ~$3.60 of $5 spent)
- Bot restarts on Railway auto-redeploy; appointment state is not persisted

## Troubleshooting

**"No se encontró Chrome/Chromium"** — Check CHROME_BIN path in app_controller.py matches system (Darwin, Linux paths differ)

**"Campo usuario no encontrado"** — Hospital portal DOM changed; update JS in `login()` to query correct input selector

**"SESSION_EXPIRED"** — Tu Portal session timed out; `buscar_turno_mas_cercano()` auto-relogins once, then fails if retry fails

**Telegram not sending** — Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set; verify network access to api.telegram.org

**macOS notifications not appearing** — User may need to allow notifications for Terminal.app (System Preferences > Notifications)
