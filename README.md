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
- Optional: a Grafana with a Prometheus datasource (node_exporter, plus
  nvidia_gpu_exporter or the DCGM exporter for GPUs)
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
3. Add the bot to your group as a normal member: do **not** make it an admin, and
   leave privacy mode on (the BotFather default; do not run `/setprivacy`). The
   bot only needs to read its own commands and post in the booking topic.
4. Open `/admin/setup/bind`, then within 15 minutes send `/bind@<your_bot>`
   (for example `/bind@lab_booking_bot`) in the machine-booking topic. With
   privacy mode on, Telegram reliably delivers only commands addressed to the bot.
   Confirm the group name shown in the page. You must be a group admin, and your
   admin rights must not have "Remain anonymous" turned on, or the bot cannot
   tell who sent `/bind`.

   `/bind` also registers the topic for the topic dropdown under Admin ->
   Integrations. With privacy mode on the bot does not see ordinary messages,
   so a topic only appears there after `/bind` or another bot command has been
   sent in it.

   The bot deletes its own `/bind` confirmation after 30 seconds. It cannot
   delete your `/bind` message, so delete that by hand if you like. Letting the
   bot delete it would mean making it an admin with "Delete messages", and that
   also lets it read every message in the group, so it is not recommended.
5. Log in with Telegram as a group admin (see "Logging in" below).
6. Grafana: enter the URL and a token (a read-only service-account token is
   preferred; user/password also works), press Test, then choose the datasource.
   The default PromQL query templates target node_exporter (CPU, RAM, host
   discovery) and the NVIDIA DCGM exporter (GPUs), all matched on the `instance`
   label (e.g. `DCGM_FI_DEV_GPU_UTIL{instance=~"(?i){instance_re}(\\.[^:]*)?(:.*)?"}`).
   The instance matchers are case-insensitive and accept a domain suffix and a
   port, so a server stored as `gpu-01` matches `instance="GPU-01.lab:9100"`.
   Adjust them to your labels:
   - `{instance}` is replaced by the server's exact instance/hostname value
     (used with `=`).
   - `{instance_re}` is the regex-escaped value for `=~` matchers (e.g.
     `instance=~"{instance_re}(:.*)?"`, which also matches `host:9100`).

   Using [nvidia_gpu_exporter](https://github.com/utkuozdemir/nvidia_gpu_exporter)
   instead? It also labels series by `instance`; its `*_ratio` metrics are 0-1,
   hence the `100 *`. Use:
   - gpu_util: `100 * nvidia_smi_utilization_gpu_ratio{instance=~"(?i){instance_re}(\\.[^:]*)?(:.*)?"}`
   - gpu_mem: `100 * nvidia_smi_memory_used_bytes{...same matcher} / nvidia_smi_memory_total_bytes{...same matcher}`
   - discover_gpus: `count by (instance) (nvidia_smi_utilization_gpu_ratio)`

   Saved queries that are exactly a default of an earlier version (the old DCGM
   `Hostname="{instance}"` queries, the nvidia_gpu_exporter queries, or the older
   case-sensitive `instance=~"{instance_re}(:.*)?"` forms) are treated as unset
   and replaced by the current defaults. To keep one of them, save a slightly
   different query (e.g. add `,gpu!=""` inside the braces).

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
bookable (and can be shared) but are never reclaimed.

## Sharing and idle reclaim

**Sharing.** Sharing is chosen per booking by the person booking: tick
"Allow sharing" on the booking form (or use "Let others share" under My
bookings, as long as nobody has joined yet). Someone else may
then book an overlapping time — the board shows the machine purple ("In use —
can share") with a **Join** button, and the booking form asks them to confirm
("You'll be sharing gpu-01 with Ana from … to …"). The owner gets a Telegram
message when someone joins. At most two people are on a machine at any instant,
and the one who joins can't open it to a third. Overlapping a booking that is
not shareable is refused, for new bookings and extensions alike. The CPU only /
CPU + GPU choice is shown on the board and in the bot so you can see what the
other person uses; it is not enforced. While two people share a machine, both
get a Telegram alert if its average CPU, RAM or GPU memory reaches 50 % over
15 min (admin-editable).

**Idle reclaim.** If a booked server stays idle for 120 min (CPU < 10 %, GPU
util < 5 % for CPU + GPU bookings, RAM change < 2 percentage points), the user
is warned. After 3 warnings the booking is released. Each booking is judged on
what it booked: a CPU only booking ignores the GPU entirely. On a shared machine
each person is warned separately, with their own "I'm still using it" button,
and only a booking that runs out of warnings is released — the other keeps the
machine. Metrics older than 15 min count as stale and snapshots are
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
| purple | in use, one more person can join |
| grey | unavailable |

In Telegram: 🟢 available, 🟠 booked soon, 🔴 booked, 🔴➜🟢 free soon,
🟣 shareable, ⚪ unavailable.

**Bot commands.** `/status` lists all servers (`/status <server>` shows one in
detail), `/free` lists servers that are free now, and `/notify <server>` asks
the bot to message you when that server becomes free. A machine in use that has been
quiet for at least the idle window shows "💤 Quiet for 2 h 15 min (no activity
since 11:05)", so you can ask the person who booked it.
In the group topic, address commands to the bot: `/status@<your_bot>`,
`/free@<your_bot>`, `/notify@<your_bot> <server>`. With privacy mode on,
Telegram reliably delivers only commands addressed to the bot. In a private chat
with the bot, plain `/status` works.

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
