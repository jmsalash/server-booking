# Server Booking

A small web portal and Telegram bot for booking lab compute servers. Users book
servers from a calendar-style page, the bot announces bookings in the group's
machine-booking topic, bookings are mirrored to Google Calendar, and Grafana
(Prometheus) metrics are used to allow sharing a busy-but-not-full server and to
reclaim servers that were booked but sit idle.

Components: `web` (FastAPI), `worker` (Telegram bot, scheduler, Grafana/Calendar
sync), `db` (PostgreSQL 16). Secrets entered in the admin UI are encrypted with
`data/secret.key`.

## Requirements

- Docker with the Compose plugin
- A Telegram group with topics enabled, and a bot token from BotFather
- Optional: a Grafana with a Prometheus datasource (node_exporter, DCGM exporter)
- Optional: a Google Cloud service account for Google Calendar

## Install

    docker compose up -d --build

The web UI listens on port 8000. The only configuration is `DATABASE_URL`
(compose sets it) and the `/data` volume.

## First-run setup

1. Get the setup code (printed once per start while setup is incomplete):

       docker compose logs web | grep "SETUP CODE"

   Open `http://<host>:8000/admin` (it redirects to `/admin/setup`) and enter it.
2. Telegram token: create a bot with BotFather and paste the token.
3. Add the bot to your group as an administrator. In BotFather run
   `/setprivacy` -> your bot -> Disable (privacy mode off). The admin who will
   send `/bind` must have "Remain anonymous" disabled in the group.
4. Open `/admin/setup/bind`, then within 15 minutes send `/bind` in the
   machine-booking topic. Confirm the group name shown in the page.
5. Log in with Telegram as a group admin (see "Logging in" below).
6. Grafana: enter the URL and a token (a read-only service-account token is
   preferred; user/password also works), press Test, then choose the datasource.
   Adjust the PromQL query templates to your labels:
   - `{instance}` is replaced by the server's exact instance/hostname value
     (used with `=`, e.g. `DCGM_FI_DEV_GPU_UTIL{Hostname="{instance}"}`).
   - `{instance_re}` is the regex-escaped value for `=~` matchers (e.g.
     `instance=~"{instance_re}(:.*)?"`, which also matches `host:9100`).

   To inspect what your Grafana exposes, run the probe on the host (`scripts/`
   is not in the image) from a dev venv (`python -m venv .venv && .venv/bin/pip
   install httpx`), reading the password without echoing it or storing it in
   shell history:

       read -rs GF_PW
       .venv/bin/python scripts/grafana_probe.py --url https://grafana.lab --user me --password "$GF_PW"
       # or: .venv/bin/python scripts/grafana_probe.py --url https://grafana.lab --token glsa_...
       unset GF_PW

7. Google Calendar: create a service account in Google Cloud, enable the Google
   Calendar API, and download its JSON key. In Google Calendar share the target
   calendar with the service account's email with "Make changes to events".
   In the wizard upload the JSON and enter the calendar ID. Events are titled
   `<server name> | <user display name>`.

## Adding servers

In the admin area add each server with its name and the Grafana instance
identifier(s) used by the query templates. Servers without metrics are still
bookable but are never shareable or reclaimed.

## Sharing and idle reclaim

**Sharing.** A server that is booked but lightly used can be offered as
shareable (yellow). Defaults: CPU, RAM and single-GPU memory each below 50 %,
averaged over 60 min; a short-spike check averages 15 min. At most 2 bookings
may overlap at any instant, and a shared booking must start in the current hour.

**Idle reclaim.** If a booked server stays idle for 120 min (GPU util < 5 %,
CPU < 10 %, RAM change < 2 percentage points; for multi-GPU servers an idle GPU
is util < 5 % and memory < 10 %), the user is warned. After 3 warnings the
booking is released. Metrics older than 15 min count as stale and snapshots are
kept 48 h.

Other defaults (all editable by admins): max booking 14 days, "booked soon"
window 180 min, working hours Mon-Fri 09:00-19:00, timezone `Europe/Rome`.

## Users' guide

**Login.** Open the portal, choose Telegram login, and the bot sends you a
message with a confirm button. Tap it to confirm; the browser then logs in.
Login tokens expire after 5 min; sessions last 30 days.

**Colours.**

| Colour | Meaning |
| --- | --- |
| green | available |
| orange | booked soon |
| red | booked |
| half red / half green | free soon |
| light yellow | shareable |
| grey | unavailable |

In Telegram: 🟢 available, 🟠 booked soon, 🔴 booked, 🔴➜🟢 free soon,
🟡 shareable, ⚪ unavailable.

**Bot commands.** `/status` lists all servers (`/status <server>` shows one in
detail), `/free` lists servers that are free now, and `/notify <server>` asks
the bot to message you when that server becomes free.

## Operations

**Backups.** Back up both volumes: `pgdata` (database) and `data`. The file
`data/secret.key` is required to decrypt stored secrets; without it you must
re-enter the Telegram, Grafana and Google credentials. Example:

    docker compose exec db pg_dump -U booking booking > booking.sql
    docker run --rm -v <project>_data:/data -v "$PWD":/out alpine tar czf /out/data.tgz -C /data .

**Recovery.** If you lose admin access, re-enable the setup code:

    RESET_SETUP=1 docker compose up -d
    docker compose logs web | grep "SETUP CODE"

This issues a new code and re-opens the setup wizard (the existing group binding
is kept; confirm or reset it in the bind step). Unset `RESET_SETUP` afterwards
(plain `docker compose up -d`): every restart with it set issues a new code and
re-opens setup.

**Reverse proxy.** The proxy must preserve the `Host` header (POSTs with an
`Origin` that differs from `Host` are rejected). Uvicorn runs with
`--proxy-headers`, so pass `X-Forwarded-For`/`-Proto`; the setup-code rate limit
is per client IP and relies on it. Serve over HTTPS.

**Logs** never contain secrets (tokens, passwords, setup-code attempts are not
logged; HTTP client logs are silenced). The setup code itself is printed to the
web log by design.

The worker retries on its own; on first start it may log a few "setting ...
unreadable" lines until the web container finishes migrations.

## Development

    python -m venv .venv
    .venv/bin/pip install -e '.[dev]'
    .venv/bin/pytest

The integration tests use testcontainers and need a running Docker daemon.
