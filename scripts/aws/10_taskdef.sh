#!/usr/bin/env bash
# Render task-def.template.json with config.env values and register it.
source "$(dirname "$0")/lib.sh"
require_var ECR_AUTOAGENT; require_var ECR_WEBNEXT; require_var EXECUTION_ROLE_ARN
require_var TASK_ROLE_ARN; require_var EFS_ID; require_var EFS_ACCESS_POINT_ID
require_var LOG_GROUP; require_var ENV_BUCKET; require_var AWS_REGION
D="$(dirname "$0")"

envsubst < "$D/task-def.template.json" > "$D/task-def.json"
log "rendered $D/task-def.json"
REV=$(aws ecs register-task-definition --region "$AWS_REGION" \
  --cli-input-json "file://$D/task-def.json" \
  --query 'taskDefinition.revision' --output text)
log "registered ${TASK_FAMILY}:${REV}"
log "10_taskdef done."
