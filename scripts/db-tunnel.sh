#!/usr/bin/env bash
#
# Open a local port onto the staging (or prod) RDS instance.
#
# WHY THIS EXISTS RATHER THAN AN IP ALLOWLIST
# -------------------------------------------
# There is nothing to allowlist against. The instance has publicly_accessible
# false and lives in subnets whose route table has no internet gateway, so no
# security-group rule can create a path from a laptop to it - the routing is
# what blocks you, not the firewall. Making it reachable would mean moving the
# database into public subnets and flipping publicly_accessible in a Terraform
# module that is SHARED WITH PROD, then keeping a residential IP allowlisted as
# it changes.
#
# So we borrow a path that already exists. ECS Exec gives an SSM channel into a
# running Fargate task, and SSM can forward a TCP port over that channel. The
# database sees the connection arriving from the task's private address, which
# its security group already trusts. No infrastructure changes, nothing exposed
# to the internet, and the session is logged against your IAM user.
#
#   ./scripts/db-tunnel.sh                    # staging -> localhost:15432
#   ENV=prod ./scripts/db-tunnel.sh           # prod
#   LOCAL_PORT=15433 ./scripts/db-tunnel.sh   # when 15432 is taken
#
# Leave it running; connect from another terminal with scripts/db-env.sh.
set -euo pipefail

ENV="${ENV:-staging}"
LOCAL_PORT="${LOCAL_PORT:-15432}"
AWS_PROFILE="${AWS_PROFILE:-loopd}"
AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_PROFILE AWS_REGION

CLUSTER="ecs-loopd-${ENV}"
SERVICE="svc-ecs-loopd-${ENV}-api"
DB_ID="rds-loopd-${ENV}"

die() { echo "ERROR: $*" >&2; exit 1; }

# Preflight. Each of these fails in a way that is hard to read if left to the
# underlying command, so they are checked by name first.
command -v aws >/dev/null || die "aws CLI not installed"
command -v session-manager-plugin >/dev/null || die \
  "session-manager-plugin not installed - brew install --cask session-manager-plugin"
aws sts get-caller-identity >/dev/null 2>&1 || die \
  "AWS credentials for profile '${AWS_PROFILE}' are not working"

if lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  die "port ${LOCAL_PORT} is already in use - set LOCAL_PORT to something else"
fi

echo ">> Resolving ${DB_ID} endpoint..."
DB_HOST=$(aws rds describe-db-instances --db-instance-identifier "${DB_ID}" \
  --query 'DBInstances[0].Endpoint.Address' --output text) || die "no such database ${DB_ID}"
DB_PORT=$(aws rds describe-db-instances --db-instance-identifier "${DB_ID}" \
  --query 'DBInstances[0].Endpoint.Port' --output text)

# Resolved fresh every run on purpose: the task id is part of the SSM target and
# changes on EVERY deploy. A saved copy of the resulting command works until the
# next deploy and then fails with an unhelpful TargetNotConnected.
echo ">> Finding a running ${SERVICE} task..."
TASK=$(aws ecs list-tasks --cluster "${CLUSTER}" --service-name "${SERVICE}" \
  --desired-status RUNNING --query 'taskArns[0]' --output text)
[ "${TASK}" != "None" ] && [ -n "${TASK}" ] || die \
  "no RUNNING task in ${SERVICE} - the service may be scaled to zero or mid-deploy"

read -r EXEC_ENABLED RUNTIME_ID < <(aws ecs describe-tasks --cluster "${CLUSTER}" \
  --tasks "${TASK}" \
  --query 'tasks[0].[enableExecuteCommand,containers[0].runtimeId]' --output text)

[ "${EXEC_ENABLED}" = "True" ] || die \
  "ECS Exec is disabled on this task. Set enable_execute_command on the service in Terraform."

echo ">> Tunnelling localhost:${LOCAL_PORT} -> ${DB_HOST}:${DB_PORT}"
echo ">> Connect with:  source scripts/db-env.sh${LOCAL_PORT:+ }  (ENV=${ENV} LOCAL_PORT=${LOCAL_PORT})"
echo ">> Ctrl-C to close."

exec aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK##*/}_${RUNTIME_ID}" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"${DB_HOST}\"],\"portNumber\":[\"${DB_PORT}\"],\"localPortNumber\":[\"${LOCAL_PORT}\"]}"
