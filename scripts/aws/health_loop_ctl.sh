#!/usr/bin/env bash
# Control the auto-heal loop for a repo without HTTP auth: a one-off ECS task
# runs the app's own config_service against the autoagent DB (env from the S3
# env file). Graceful — pausing lets the in-flight batch finish, then the loop
# releases the lease and starts no new batch.
#   usage: health_loop_ctl.sh <repo_id> <pause|resume|status>
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var CLUSTER; require_var EXECUTION_ROLE_ARN; require_var TASK_ROLE_ARN
require_var ECR_AUTOAGENT; require_var ENV_BUCKET; require_var LOG_GROUP
require_var AUTOAGENT_SG_ID; require_var PUBLIC_SUBNET_IDS
R="$AWS_REGION"; REPO="${1:?repo_id}"; ACTION="${2:?pause|resume|status}"
SUBNET1=$(echo "$PUBLIC_SUBNET_IDS" | cut -d, -f1)

case "$ACTION" in
  pause)  PY="import asyncio;from agent.health_loop.config_service import set_state,set_enabled;l=asyncio.new_event_loop();l.run_until_complete(set_enabled(${REPO},False));l.run_until_complete(set_state(${REPO},'paused'));print('STOPPED ${REPO} (enabled=False)')";;
  resume) PY="import asyncio;from agent.health_loop.config_service import set_state,set_enabled;l=asyncio.new_event_loop();l.run_until_complete(set_enabled(${REPO},True));l.run_until_complete(set_state(${REPO},'running'));print('RESUMED ${REPO}')";;
  status) PY="import asyncio;from agent.health_loop.config_service import get_config;c=asyncio.run(get_config(${REPO}));print('STATUS',('none' if not c else f'enabled={c.enabled} state={c.state}'))";;
  *) die "action must be pause|resume|status";;
esac
B64=$(printf '%s' "python3 -c \"$PY\"" | base64 | tr -d '\n')

TD=$(cat <<JSON
{"family":"autoagent-healctl","networkMode":"awsvpc","requiresCompatibilities":["FARGATE"],
 "cpu":"512","memory":"1024","executionRoleArn":"${EXECUTION_ROLE_ARN}","taskRoleArn":"${TASK_ROLE_ARN}",
 "containerDefinitions":[{"name":"healctl","image":"${ECR_AUTOAGENT}:${IMAGE_TAG}","essential":true,
   "command":["sh","-c","echo ${B64} | base64 -d | sh"],
   "environmentFiles":[{"value":"arn:aws:s3:::${ENV_BUCKET}/autoagent.env","type":"s3"}],
   "logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":"${LOG_GROUP}","awslogs-region":"${R}","awslogs-stream-prefix":"healctl"}}}]}
JSON
)
aws ecs register-task-definition --region "$R" --cli-input-json "$TD" >/dev/null
ARN=$(aws ecs run-task --region "$R" --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition autoagent-healctl \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET1],securityGroups=[$AUTOAGENT_SG_ID],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)
log "healctl $ACTION repo $REPO — task $(basename "$ARN"), waiting..."
aws ecs wait tasks-stopped --region "$R" --cluster "$CLUSTER" --tasks "$ARN"
CODE=$(aws ecs describe-tasks --region "$R" --cluster "$CLUSTER" --tasks "$ARN" --query 'tasks[0].containers[0].exitCode' --output text)
aws logs get-log-events --region "$R" --log-group-name "$LOG_GROUP" --log-stream-name "healctl/healctl/$(basename "$ARN")" --query 'events[].message' --output text 2>/dev/null || true
log "exit code: $CODE"; [ "$CODE" = 0 ] || die "healctl failed"
