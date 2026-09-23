# backend

Loopd'n backend (Django)

## What this is

The API behind Loopd'n, a realtime messaging app. It handles everything the
clients need and nothing they render:

- **Accounts** — register, log in with a username *or* email, JWT sessions with
  refresh-token rotation and blacklisting. Email verification is **on** by
  default (`REQUIRE_EMAIL_VERIFICATION`): registering emails a six-digit code
  and login is refused until `verify-email` confirms it, which also returns the
  first token pair. With `RESEND_API_KEY` unset the message is rendered and
  logged instead of sent, so a laptop needs no account.
- **Profiles** — display name, avatar upload, bio, date of birth, E.164 phone
  number, and presence (`is_online` / `last_seen_at`).
- **People search** — find users by username prefix or exact phone number.
- **Conversations** — direct (exactly two people, deduplicated so a pair can
  never end up with two parallel threads) and groups (with admins).
- **Messages** — text, image and file attachments, paginated history.
- **Realtime** — one websocket per user at `/ws/chat/`, carrying new messages,
  typing indicators and new-conversation events for every thread you belong to.
- **Calls** — voice and video calls in any conversation, on a self-hosted
  [LiveKit](https://livekit.io). Django decides who may join, enforces the plan's
  participant limit and signs a short-lived token; the browser then talks to
  LiveKit directly, so no audio or video ever passes through this API. Call
  events ring people over the same websocket. Recording writes to S3 by way of
  LiveKit's egress service, and Django keeps only the metadata.

Two conventions worth knowing before reading the code:

- Every REST response leaves through a single envelope — `{code, message, data}`,
  where `code` is `1` on success and `-1` on failure. HTTP status codes are left
  meaningful. See [loopd/renderers.py](loopd/renderers.py).
- Access control is the queryset, not a check bolted on afterwards. Chat lookups
  start from the conversations the caller participates in, so a non-member gets
  a 404 from every route and cannot learn that a conversation exists.

| App | What lives there |
| --- | --- |
| [loopd/](loopd/) | Settings, ASGI/WSGI entrypoints, Celery app, response envelope, exception handler |
| [user/](user/) | Accounts, JWT auth, profiles, search, websocket auth middleware |
| [otp/](otp/) | One-time passcodes: issue, verify, rate-limit, expire |
| [chat/](chat/) | Conversations, participants, messages, attachments, websocket consumer |
| [calls/](calls/) | Calls, participants, recordings, the LiveKit wrapper and webhook; plan limits are read from [billing/](billing/) |

## Tech stack

| | |
| --- | --- |
| Language | Python 3.13 |
| Framework | Django 6.0, Django REST Framework 3.18 |
| Realtime | Channels 4.3 over Daphne (ASGI), `channels_redis` layer |
| Auth | `djangorestframework-simplejwt` — 30 min access, 30 day refresh, rotated and blacklisted |
| Database | PostgreSQL 16 in Docker; SQLite automatically when running off a laptop |
| Cache / broker | Redis 7 — db `0` for the Channels layer, db `1` for Celery, so a `FLUSHDB` on one never drops the other |
| Background work | Celery 5.6 with `django-celery-beat` (schedules stored in Postgres, editable from the admin) |
| Media | Pillow, written to `var/media/` (gitignored) |
| Deployment | Docker Compose — `web`, `db`, `redis`, `celery`, `celery_beat` |

Daphne rather than gunicorn: this app serves websockets, and a WSGI server
cannot. `daphne` is also first in `INSTALLED_APPS`, which replaces `runserver`
with an ASGI one — that is what makes websockets work in development.

## How to run it

### Locally

Needs nothing but Python 3.13. With no `POSTGRES_DB` in the environment Django
falls back to SQLite, and with no `REDIS_URL` the channel layer falls back to an
in-memory one — enough for `runserver` and the test suite, though it drops
events between processes, so it is single-process only.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python manage.py migrate
python manage.py createsuperuser   # optional, for /admin/
python manage.py runserver
```

- API — http://127.0.0.1:8000/api/v1/
- Admin — http://127.0.0.1:8000/admin/
- Websocket — ws://127.0.0.1:8000/ws/chat/?token=&lt;access token&gt;

`DEBUG` defaults to on, which turns on DRF's browsable API, serves uploaded
media through Django, and returns the OTP code in the verification response so
you can sign up without a mail server.

Run the tests with:

```bash
python manage.py test
```

Celery is optional locally — nothing schedules work yet. To exercise it, point
`REDIS_URL` at a running Redis and start a worker:

```bash
celery -A loopd worker --loglevel=info
```

### Testing calls locally

Calls run on [LiveKit](https://livekit.io), a separate media server. Django only
authorises a call and signs a token; the browser then connects to LiveKit
directly, so **a call cannot connect unless a LiveKit server is reachable**. The
tests (`python manage.py test calls`) need none of this — they never contact
LiveKit. This is for trying the feature by hand.

You will have three things running at once, each in its own terminal:

| What | Where |
| --- | --- |
| Django (`runserver`) | port 8000 |
| The web frontend | port 3000 |
| A LiveKit server, in Docker | port 7880 (signalling), 7881/7882 (media) |

SQLite and the in-memory channel layer are fine; no Postgres or Redis needed.

**1. Give Django a LiveKit key and secret.** Any pair will do, as long as the
LiveKit server is started with the *same* one. Generate one, and keep the secret
at 32 characters or more:

```bash
python -c "import secrets; print('LIVEKIT_API_KEY=API' + secrets.token_hex(6)); print('LIVEKIT_API_SECRET=' + secrets.token_urlsafe(32))"
```

Put both lines in `.env`, with the address of the server you are about to start:

```
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_URL=
```

Leave `LIVEKIT_API_URL` blank: it defaults to `LIVEKIT_URL`. All three of key,
secret and URL must be set, or none — a partly configured setup fails
`manage.py check` (`calls.E001`), and with none set every call endpoint answers
`503`.

**2. Start LiveKit.** Needs Docker Desktop running. On Windows, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\livekit-local.ps1
```

It reads the key and secret from `.env` and runs the same image version the
staging service uses. Leave it running; Ctrl+C stops it. `curl http://localhost:7880`
should answer `OK`. On macOS or Linux, run the equivalent by hand:

```bash
docker run --rm --name livekit -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
  -e "LIVEKIT_KEYS=<your key>: <your secret>" \
  -e $'LIVEKIT_CONFIG=port: 7880\nrtc:\n  tcp_port: 7881\n  udp_port: 7882\n  use_external_ip: false\n' \
  livekit/livekit-server:v1.13.7 --node-ip 127.0.0.1
```

**3. Restart `runserver`.** Django reads `.env` only when it starts, so a running
server still has the old values. Then check that it can reach LiveKit:

```bash
python manage.py shell -c "from calls import livekit; livekit.delete_room('x'); print('reachable')"
```

`reachable` means it can. `Cannot reach LiveKit at ...` means the container is not
running or `LIVEKIT_URL` is wrong.

**4. Make a call.** A call needs two people, so sign in as two different
accounts at once — a normal window and a private one, say — and put both in the
same group or direct conversation. One presses the call button, the other gets the
incoming call and accepts. `localhost` counts as a secure context, so the browser
will allow the microphone and camera. Try audio and video, mute and unmute, one
person leaving, and the last person leaving (which ends the call).

**What does not work locally**

- **A closed tab is not noticed straight away.** LiveKit reports disconnects to
  Django by webhook, and it cannot reach your machine. The person stays "in" the
  call until the background sweep gives their place back (`calls.sweep_stale_calls`,
  which needs Celery beat, so in practice: until they leave properly).
- **Recording.** It needs LiveKit's separate egress service and an S3 bucket, and
  neither exists in this setup. `POST .../recording/start/` answers `503` here.
  Leave `LIVEKIT_RECORDING_BUCKET` blank and it says so plainly.
- **Plan limits are still enforced.** Every account is on Free unless you change
  it, which allows 4 people on a call and has no screen sharing or recording.
  Change a plan in `/admin/` (Billing → Subscriptions) to try the others.

**When something goes wrong**

| You see | It means |
| --- | --- |
| `503` from `POST /api/v1/calls/` | `LIVEKIT_URL`, `LIVEKIT_API_KEY` or `LIVEKIT_API_SECRET` is missing, or `runserver` was not restarted after setting them |
| `Cannot reach LiveKit at ...` in the Django log | The LiveKit container is not running, or `LIVEKIT_URL` points somewhere that does not exist |
| The call starts, then hangs up about a second later, and a following `join` gets `409` | The browser could not connect to LiveKit, so the app left the call. Check the container and the browser console |
| `403` "This call is full" | The plan's participant limit — see above |
| `404` on a call you can see in the UI | You are not an active member of that conversation |
| `database is locked` | Fixed in `loopd/settings.py` for SQLite (`transaction_mode: IMMEDIATE`); restart `runserver` if you still see it |

### The full stack, in Docker

Brings up Postgres, Redis, the ASGI server, a Celery worker and beat.

```bash
cp .env.staging.example .env.staging
# fill in SECRET_KEY, JWT_SIGNING_KEY, POSTGRES_PASSWORD, ALLOWED_HOSTS

docker compose -f docker-compose-staging.yml up --build
```

The `web` service migrates and runs `collectstatic` before serving, so a fresh
box comes up ready. Source is bind-mounted, so a staging box can be updated with
a restart rather than a rebuild.

Host ports are configurable, because 8000 is a popular port and colliding with
another project's `runserver` produces a confusing half-working stack. These are
`${VAR}` substitutions, so they come from your shell or a plain `.env` beside the
compose file — **not** from `.env.staging`:

```bash
WEB_PORT=8010 docker compose -f docker-compose-staging.yml up -d
```

`ALLOWED_HOSTS` is not optional once `DEBUG=False`: it also gates websockets, and
`AllowedHostsOriginValidator` refuses every handshake when the list is empty.
