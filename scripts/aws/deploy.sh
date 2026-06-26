#!/usr/bin/env bash
# One-command AWS deploy (no GitHub Actions). Pure aws-cli + docker.
#
#   ./deploy.sh              full deploy: build+push images, migrate DB, roll service
#   ./deploy.sh --no-build   skip the image build (just migrate + roll the current :latest)
#   ./deploy.sh --no-migrate skip migrations (build + roll only)
#
# Order matters: migrations run BEFORE the service roll, so the new code never
# starts against an un-migrated schema (the drift that left prod at alembic 047
# while the code expected 057, breaking code-graph + auto-heal).
#
# Prereqs: docker running, `aws sso login` done, `gh auth token` available
# (used by 03_build_push.sh for the private team-memory build-arg). All infra
# coordinates come from config.env.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var CLUSTER; require_var SERVICE
DIR="$(cd "$(dirname "$0")" && pwd)"

DO_BUILD=1; DO_MIGRATE=1
for arg in "$@"; do
  case "$arg" in
    --no-build) DO_BUILD=0 ;;
    --no-migrate) DO_MIGRATE=0 ;;
    *) die "unknown flag: $arg (use --no-build | --no-migrate)" ;;
  esac
done

if [ "$DO_BUILD" = 1 ]; then
  log "STEP 1/3 — build + push images (auto-agent + web-next)"
  "$DIR/03_build_push.sh"
else
  log "STEP 1/3 — skipped (--no-build); rolling whatever :latest is in ECR"
fi

if [ "$DO_MIGRATE" = 1 ]; then
  log "STEP 2/3 — alembic upgrade head (one-off Fargate task on the new image)"
  "$DIR/prod_migrate.sh"
else
  log "STEP 2/3 — skipped (--no-migrate)"
fi

log "STEP 3/3 — roll the service (force-new-deployment)"
aws ecs update-service --region "$AWS_REGION" --cluster "$CLUSTER" --service "$SERVICE" \
  --force-new-deployment >/dev/null
log "waiting for service to stabilize..."
aws ecs wait services-stable --region "$AWS_REGION" --cluster "$CLUSTER" --services "$SERVICE"

# Confirm the running task is on the digest we just pushed.
T=$(aws ecs list-tasks --region "$AWS_REGION" --cluster "$CLUSTER" --service-name "$SERVICE" \
     --query 'taskArns[0]' --output text)
DIGEST=$(aws ecs describe-tasks --region "$AWS_REGION" --cluster "$CLUSTER" --tasks "$T" \
     --query 'tasks[0].containers[?name==`auto-agent`].imageDigest' --output text)
log "deploy complete — service stable. auto-agent digest: ${DIGEST}"
