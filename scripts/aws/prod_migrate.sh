#!/usr/bin/env bash
# Apply alembic migrations to PROD (047 -> head). Runs the app image's own
# alembic against the RDS autoagent DB (env from the S3 env file). Prints the
# version before and after. `alembic upgrade head` is a no-op if already current.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var CLUSTER; require_var EXECUTION_ROLE_ARN; require_var TASK_ROLE_ARN
require_var ECR_AUTOAGENT; require_var ENV_BUCKET; require_var LOG_GROUP
require_var AUTOAGENT_SG_ID; require_var PUBLIC_SUBNET_IDS
R="$AWS_REGION"; SUBNET1=$(echo "$PUBLIC_SUBNET_IDS" | cut -d, -f1)

# Shell run inside the app container. PYTHONPATH=/app so alembic env.py imports shared.*
SH='cd /app && export PYTHONPATH=/app && echo "BEFORE:" && alembic current && echo "--- upgrading ---" && alembic upgrade head && echo "AFTER:" && alembic current && echo MIGRATE_OK'
SH_B64=$(printf '%s' "$SH" | base64 | tr -d '\n')

TD=$(cat <<JSON
{"family":"autoagent-migrate","networkMode":"awsvpc","requiresCompatibilities":["FARGATE"],
 "cpu":"512","memory":"1024","executionRoleArn":"${EXECUTION_ROLE_ARN}","taskRoleArn":"${TASK_ROLE_ARN}",
 "containerDefinitions":[{"name":"migrate","image":"${ECR_AUTOAGENT}:${IMAGE_TAG}","essential":true,
   "command":["sh","-c","echo ${SH_B64} | base64 -d | sh"],
   "environmentFiles":[{"value":"arn:aws:s3:::${ENV_BUCKET}/autoagent.env","type":"s3"}],
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"${LOG_GROUP}","awslogs-region":"${R}","awslogs-stream-prefix":"migrate"}}}]}
JSON
)
aws ecs register-task-definition --region "$R" --cli-input-json "$TD" >/dev/null
ARN=$(aws ecs run-task --region "$R" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition autoagent-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET1],securityGroups=[$AUTOAGENT_SG_ID],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)
log "prod migrate — task $(basename "$ARN"), waiting..."
aws ecs wait tasks-stopped --region "$R" --cluster "$CLUSTER" --tasks "$ARN"
CODE=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$ARN" --query 'tasks[0].containers[0].exitCode' --output text)
echo "----- migrate output -----"
aws logs get-log-events --region "$R" --log-group-name "$LOG_GROUP" --log-stream-name "migrate/migrate/$(basename "$ARN")" --query 'events[].message' --output text 2>&1 || true
echo "--------------------------"
log "exit code: $CODE"; [ "$CODE" = 0 ] || die "migration task failed"
