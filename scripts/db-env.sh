# Point this shell's Django at the tunnelled database. SOURCE it, do not run it:
#
#   ./scripts/db-tunnel.sh          # terminal 1, leave running
#   source scripts/db-env.sh        # terminal 2
#   python manage.py runserver      # now talking to staging
#
# loopd/settings.py keys the whole database choice on POSTGRES_DB being set
# (settings.py:217) - unset, it falls back to local SQLite - so exporting these
# five variables is the entire switch. There is no DATABASE_URL support to use.
#
# The password is read from SSM every time and only ever lives in this shell's
# environment. Deliberately NOT written to backend/.env: that file is loaded by
# every runserver and docker compose run in this directory, so a staging
# password left there silently becomes the default for local work too, and one
# `manage.py flush` on what you thought was SQLite is unrecoverable.
#
#   ENV=prod source scripts/db-env.sh     # if you really mean prod
#   LOCAL_PORT=15433 source scripts/db-env.sh
#
# Sourced, so it must never call exit or set -e - that would kill your shell.

_dbenv_main() {
  local env="${ENV:-staging}"
  local port="${LOCAL_PORT:-15432}"
  local profile="${AWS_PROFILE:-loopd}"
  local region="${AWS_REGION:-us-west-2}"

  # Nested definitions are global in bash, so this is unset at the end rather
  # than left behind as a stray `p` in the caller's shell.
  _dbenv_param() { aws ssm get-parameter --profile "$profile" --region "$region" \
          --name "$1" --with-decryption --query Parameter.Value --output text 2>/dev/null; }

  local db user pass
  db=$(_dbenv_param "/loopd/${env}/rds_db_name")
  user=$(_dbenv_param "/loopd/${env}/rds_username")
  pass=$(_dbenv_param "/loopd/${env}/rds_password")

  if [ -z "$db" ] || [ -z "$user" ] || [ -z "$pass" ]; then
    echo "ERROR: could not read /loopd/${env}/rds_* from SSM (profile ${profile})" >&2
    return 1
  fi

  # HOST/PORT point at the tunnel, not at RDS - the tunnel is the only route.
  export POSTGRES_DB="$db"
  export POSTGRES_USER="$user"
  export POSTGRES_PASSWORD="$pass"
  export POSTGRES_HOST=127.0.0.1
  export POSTGRES_PORT="$port"

  echo ">> Django in this shell now points at ${env} via 127.0.0.1:${port}"
  echo ">> db=${db} user=${user} (password exported, not printed)"
  if ! lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">> WARNING: nothing is listening on ${port} - start ./scripts/db-tunnel.sh first" >&2
  fi
}
_dbenv_main
unset -f _dbenv_main _dbenv_param 2>/dev/null
