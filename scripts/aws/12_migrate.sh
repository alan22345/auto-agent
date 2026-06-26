#!/usr/bin/env bash
# Migrate live data from the Azure VM into AWS. Two independent phases:
#   ./12_migrate.sh db        pg_dump the VM DB -> restore into harpoon autoagent
#   ./12_migrate.sh userdata  tar the VM Claude-auth volume -> seed EFS
#   ./12_migrate.sh both      (default) run both
# Staging goes through S3 with short-lived presigned URLs, so the throwaway
# restore/seed tasks need no extra tooling (busybox wget + psql/tar only).
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var ENV_BUCKET; require_var CLUSTER; require_var EXECUTION_ROLE_ARN
require_var HARPOON_RDS_ENDPOINT; require_var AUTOAGENT_SG_ID; require_var PUBLIC_SUBNET_IDS
require_var EFS_ID; require_var EFS_ACCESS_POINT_ID; require_var LOG_GROUP
R="$AWS_REGION"; VM="azureuser@172.190.26.82"
SUBNET1=$(echo "$PUBLIC_SUBNET_IDS" | cut -d, -f1)
NET="awsvpcConfiguration={subnets=[$SUBNET1],securityGroups=[$AUTOAGENT_SG_ID],assignPublicIp=ENABLED}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

run_oneoff() {  # run_oneoff TASKDEF_FAMILY STREAM_PREFIX
  local fam="$1" pfx="$2"
  local arn; arn=$(aws ecs run-task --region "$R" --cluster "$CLUSTER" --launch-type FARGATE \
    --task-definition "$fam" --network-configuration "$NET" --query 'tasks[0].taskArn' --output text)
  log "task $arn — waiting..."
  aws ecs wait tasks-stopped --region "$R" --cluster "$CLUSTER" --tasks "$arn"
  local code; code=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$arn" \
    --query 'tasks[0].containers[0].exitCode' --output text)
  aws logs get-log-events --region "$R" --log-group-name "$LOG_GROUP" \
    --log-stream-name "$pfx/$(basename "$arn")" --query 'events[].message' --output text 2>/dev/null || true
  log "exit code: $code"; [ "$code" = 0 ] || die "$fam failed"
}

migrate_db() {
  log "pg_dump VM autoagent DB"
  ssh -o ConnectTimeout=15 -o BatchMode=yes "$VM" \
    'cd ~/auto-agent && docker compose exec -T postgres pg_dump -U autoagent --no-owner --no-privileges autoagent' > "$TMP/db.sql"
  [ -s "$TMP/db.sql" ] || die "dump empty"
  log "dump size: $(du -h "$TMP/db.sql" | cut -f1)"
  aws s3 cp "$TMP/db.sql" "s3://$ENV_BUCKET/migrate/db.sql" --sse AES256 >/dev/null
  local url; url=$(aws s3 presign "s3://$ENV_BUCKET/migrate/db.sql" --region "$R" --expires-in 3600)

  local cmd='wget -qO /tmp/db.sql "$DUMP_URL" && psql -v ON_ERROR_STOP=1 -f /tmp/db.sql && echo RESTORE_OK'
  local b64; b64=$(printf '%s' "$cmd" | base64 | tr -d '\n')
  local td; td=$(cat <<JSON
{"family":"autoagent-dbrestore","networkMode":"awsvpc","requiresCompatibilities":["FARGATE"],
 "cpu":"512","memory":"1024","executionRoleArn":"${EXECUTION_ROLE_ARN}",
 "containerDefinitions":[{"name":"restore","image":"public.ecr.aws/docker/library/postgres:16-alpine","essential":true,
   "command":["sh","-c","echo ${b64} | base64 -d | sh"],
   "environment":[{"name":"PGHOST","value":"${HARPOON_RDS_ENDPOINT}"},{"name":"PGPORT","value":"5432"},
     {"name":"PGUSER","value":"autoagent"},{"name":"PGDATABASE","value":"autoagent"},
     {"name":"DUMP_URL","value":"${url}"}],
   "secrets":[{"name":"PGPASSWORD","valueFrom":"$(aws secretsmanager describe-secret --region "$R" --secret-id autoagent/db-password --query ARN --output text)"}],
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"${LOG_GROUP}","awslogs-region":"${R}","awslogs-stream-prefix":"dbrestore"}}}]}
JSON
)
  aws ecs register-task-definition --region "$R" --cli-input-json "$td" >/dev/null
  run_oneoff autoagent-dbrestore dbrestore
  aws s3 rm "s3://$ENV_BUCKET/migrate/db.sql" >/dev/null 2>&1 || true
  log "DB migrated."
}

migrate_userdata() {
  local vol; vol=$(ssh -o ConnectTimeout=15 -o BatchMode=yes "$VM" "docker volume ls --format '{{.Name}}' | grep userdata | head -1")
  [ -n "$vol" ] || die "could not find userdata volume on VM"
  log "tarring VM volume $vol (Claude auth + state; excluding regenerable caches)"
  # Skip the ~7GB of regenerable build caches (.npm/.yarn/.cache/.local). Only
  # the per-user .claude state (credentials + session history) and small config
  # need to survive; the caches rebuild themselves on the new deployment.
  ssh -o BatchMode=yes "$VM" "docker run --rm -v ${vol}:/data alpine tar czf - -C /data \
    --exclude='users/*/.npm' --exclude='users/*/.yarn' --exclude='users/*/.cache' \
    --exclude='users/*/.local' ." > "$TMP/userdata.tgz"
  [ -s "$TMP/userdata.tgz" ] || die "userdata tar empty"
  log "userdata size: $(du -h "$TMP/userdata.tgz" | cut -f1)"
  aws s3 cp "$TMP/userdata.tgz" "s3://$ENV_BUCKET/migrate/userdata.tgz" --sse AES256 >/dev/null
  local url; url=$(aws s3 presign "s3://$ENV_BUCKET/migrate/userdata.tgz" --region "$R" --expires-in 3600)

  local cmd='wget -qO /tmp/u.tgz "$TGZ_URL" && tar xzf /tmp/u.tgz -C /data && echo SEED_OK && ls /data'
  local b64; b64=$(printf '%s' "$cmd" | base64 | tr -d '\n')
  local td; td=$(cat <<JSON
{"family":"autoagent-efsseed","networkMode":"awsvpc","requiresCompatibilities":["FARGATE"],
 "cpu":"256","memory":"512","executionRoleArn":"${EXECUTION_ROLE_ARN}",
 "volumes":[{"name":"userdata","efsVolumeConfiguration":{"fileSystemId":"${EFS_ID}","transitEncryption":"ENABLED","authorizationConfig":{"accessPointId":"${EFS_ACCESS_POINT_ID}","iam":"DISABLED"}}}],
 "containerDefinitions":[{"name":"seed","image":"public.ecr.aws/docker/library/alpine:3.20","essential":true,
   "command":["sh","-c","echo ${b64} | base64 -d | sh"],
   "environment":[{"name":"TGZ_URL","value":"${url}"}],
   "mountPoints":[{"sourceVolume":"userdata","containerPath":"/data","readOnly":false}],
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"${LOG_GROUP}","awslogs-region":"${R}","awslogs-stream-prefix":"efsseed"}}}]}
JSON
)
  aws ecs register-task-definition --region "$R" --cli-input-json "$td" >/dev/null
  run_oneoff autoagent-efsseed efsseed
  aws s3 rm "s3://$ENV_BUCKET/migrate/userdata.tgz" >/dev/null 2>&1 || true
  log "Claude auth seeded onto EFS."
}

case "${1:-both}" in
  db) migrate_db ;;
  userdata) migrate_userdata ;;
  both) migrate_db; migrate_userdata ;;
  *) die "usage: $0 [db|userdata|both]" ;;
esac
log "12_migrate done."
