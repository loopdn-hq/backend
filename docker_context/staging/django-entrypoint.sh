#!/usr/bin/env sh
#
# Container entrypoint.
#
# The ordering this replaces lives in docker-compose-staging.yml, which AWS
# never reads - on ECS the container would otherwise start against a database
# that has no tables.
#
# Migrations are NOT run here. CI runs them once, as a separate one-off task,
# before updating the service; docker-compose-staging.yml notes the reason:
# concurrent `migrate` calls race each other once there is more than one
# replica. Set RUN_MIGRATIONS=true to migrate on start anyway, which is useful
# when running the image by hand.
#
# collectstatic is not run here either - it happens at build time, so start-up
# stays fast and the filesystem stays immutable.
set -eu

# Without this, SIGTERM from ECS goes to the shell and the container is killed
# after the 30s stop timeout instead of shutting down cleanly. `exec` at the end
# hands PID 1 to daphne so it receives the signal directly.
trap 'echo ">> Entrypoint received shutdown signal, exiting"; exit 0' TERM INT

# Any arguments are treated as the command to run instead of the server. This is
# what lets a Celery worker, or the CI migration task, reuse this image and
# entrypoint unchanged.
if [ "$#" -gt 0 ]; then
  echo ">> ENTRYPOINT: executing overridden command: $*"
  exec "$@"
fi

if [ ! -f manage.py ]; then
  echo ">> ERROR: manage.py not found in $(pwd)" >&2
  exit 1
fi

# A task can start before RDS finishes failing over or waking up. Without this
# the container exits, ECS restarts it, and the service flaps until the database
# happens to be ready at the right moment.
wait_for_db() {
  retries=${DB_WAIT_RETRIES:-30}
  sleep_seconds=${DB_WAIT_SECONDS:-2}

  echo ">> Waiting for database to become available..."
  until python -c "
import django, sys
django.setup()
from django.db import connections
try:
    connections['default'].cursor()
except Exception as exc:
    print('   ' + type(exc).__name__ + ': ' + str(exc)[:120], file=sys.stderr)
    sys.exit(1)
sys.exit(0)
"
  do
    retries=$((retries - 1))
    if [ "$retries" -le 0 ]; then
      echo ">> ERROR: database did not become available in time" >&2
      exit 1
    fi
    echo ">> DB unavailable, sleeping ${sleep_seconds}s... (${retries} attempts left)"
    sleep "${sleep_seconds}"
  done

  echo ">> Database is available"
}

wait_for_db

# Off by default - see the header. Note there is no `makemigrations`: generating
# migrations at deploy time means the schema depends on whichever container
# happened to boot first.
if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
  echo ">> Migrating database"
  python manage.py migrate --noinput
fi

echo ">> Starting Daphne ASGI server on 0.0.0.0:8000"
# Daphne rather than gunicorn because this app serves websockets, which WSGI
# cannot. exec replaces this shell as PID 1 so signals reach daphne directly.
exec daphne -b 0.0.0.0 -p 8000 loopd.asgi:application -v2
