#!/usr/bin/env bash
# Create the autoagent role + database on harpoon RDS, via a one-off ECS task
# (the RDS is private, so we must run inside the VPC). Both the harpoon master
# password and the new autoagent password are injected by AWS from Secrets
# Manager into the throwaway container — neither is ever printed here.
#
# SHARED-PROD: reads the harpoon master secret + writes to harpoon RDS. Run this
# yourself (`! bash scripts/aws/04_db.sh`) so it executes under your permissions.
# Prereq: 07b_rds_ingress.sh has allowed the task SG into the RDS SG.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var HARPOON_RDS_ENDPOINT; require_var HARPOON_MASTER_USER
require_var HARPOON_MASTER_SECRET_ARN; require_var AUTOAGENT_SG_ID; require_var PUBLIC_SUBNET_IDS
require_var EXECUTION_ROLE_ARN; require_var LOG_GROUP; require_var CLUSTER
R="$AWS_REGION"

# 1. autoagent DB password -> Secrets Manager (generate once; reuse on re-run)
SECRET_NAME="autoagent/db-password"
AA_SECRET_ARN=$(aws secretsmanager describe-secret --region "$R" --secret-id "$SECRET_NAME" --query ARN --output text 2>/dev/null || true)
if [ -z "$AA_SECRET_ARN" ] || [ "$AA_SECRET_ARN" = None ]; then
  PW=$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)
  AA_SECRET_ARN=$(aws secretsmanager create-secret --region "$R" --name "$SECRET_NAME" \
    --secret-string "$PW" --query ARN --output text)
  log "created secret $SECRET_NAME"
else
  log "secret $SECRET_NAME exists — reusing"
fi

# 2. bootstrap SQL — single-quoted so $AUTOAGENT_PW expands at RUNTIME in-container
BOOT='set -e
psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='\''autoagent'\''" | grep -q 1 || psql -c "CREATE ROLE autoagent LOGIN PASSWORD '\''$AUTOAGENT_PW'\''"
psql -tAc "SELECT 1 FROM pg_database WHERE datname='\''autoagent'\''" | grep -q 1 || psql -c "CREATE DATABASE autoagent OWNER autoagent"
echo BOOTSTRAP_OK'
B64=$(printf '%s' "$BOOT" | base64 | tr -d '\n')

# 3. register the throwaway task definition
TD=$(cat <<JSON
{
  "family": "autoagent-dbinit",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "256", "memory": "512",
  "executionRoleArn": "${EXECUTION_ROLE_ARN}",
  "containerDefinitions": [{
    "name": "dbinit",
    "image": "public.ecr.aws/docker/library/postgres:16-alpine",
    "essential": true,
    "command": ["sh","-c","echo ${B64} | base64 -d | sh"],
    "environment": [
      {"name":"PGHOST","value":"${HARPOON_RDS_ENDPOINT}"},
      {"name":"PGPORT","value":"5432"},
      {"name":"PGUSER","value":"${HARPOON_MASTER_USER}"},
      {"name":"PGDATABASE","value":"postgres"}
    ],
    "secrets": [
      {"name":"PGPASSWORD","valueFrom":"${HARPOON_MASTER_SECRET_ARN}"},
      {"name":"AUTOAGENT_PW","valueFrom":"${AA_SECRET_ARN}"}
    ],
    "logConfiguration": {"logDriver":"awslogs","options":{
      "awslogs-group":"${LOG_GROUP}","awslogs-region":"${R}","awslogs-stream-prefix":"dbinit"}}
  }]
}
JSON
)
aws ecs register-task-definition --region "$R" --cli-input-json "$TD" >/dev/null
log "registered autoagent-dbinit task def"

# 4. run it, wait, report
SUBNET1=$(echo "$PUBLIC_SUBNET_IDS" | cut -d, -f1)
TASK_ARN=$(aws ecs run-task --region "$R" --cluster "$CLUSTER" \
  --launch-type FARGATE --task-definition autoagent-dbinit \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET1],securityGroups=[$AUTOAGENT_SG_ID],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)
log "running dbinit task: $TASK_ARN — waiting..."
aws ecs wait tasks-stopped --region "$R" --cluster "$CLUSTER" --tasks "$TASK_ARN"
CODE=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
log "dbinit exit code: $CODE"
TID=$(basename "$TASK_ARN")
aws logs get-log-events --region "$R" --log-group-name "$LOG_GROUP" \
  --log-stream-name "dbinit/dbinit/$TID" --query 'events[].message' --output text 2>/dev/null || true
[ "$CODE" = 0 ] || die "dbinit failed — see logs above"

cat <<EOF

  autoagent DB + role created on harpoon RDS.
  paste into config.env:
  export AUTOAGENT_DB_SECRET_ARN="$AA_SECRET_ARN"
EOF
log "04_db done."
