# backend

Loopd'n backend (Django)

## What this is

The API behind Loopd'n, a realtime messaging app. It handles everything the
clients need and nothing they render:

- **Accounts** — register, log in with a username *or* email, JWT sessions with
  refresh-token rotation and blacklisting. Email verification via one-time code
  is built and wired up, currently switched off (`REQUIRE_EMAIL_VERIFICATION`).
- **Profiles** — display name, avatar upload, bio, date of birth, E.164 phone
  number, and presence (`is_online` / `last_seen_at`).
- **People search** — find users by username prefix or exact phone number.
- **Conversations** — direct (exactly two people, deduplicated so a pair can
  never end up with two parallel threads) and groups (with admins).
- **Messages** — text, image and file attachments, paginated history.
- **Realtime** — one websocket per user at `/ws/chat/`, carrying new messages,
  typing indicators and new-conversation events for every thread you belong to.

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
