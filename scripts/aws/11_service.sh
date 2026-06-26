#!/usr/bin/env bash
# Create (or update) the ECS service: desired=1, in the public subnets with a
# public IP, behind both ALB target groups (api:2020 + web:3000), ECS Exec on.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var CLUSTER; require_var SERVICE; require_var TASK_FAMILY
require_var PUBLIC_SUBNET_IDS; require_var AUTOAGENT_SG_ID; require_var TG_API_ARN; require_var TG_WEB_ARN
R="$AWS_REGION"
NET="awsvpcConfiguration={subnets=[${PUBLIC_SUBNET_IDS}],securityGroups=[${AUTOAGENT_SG_ID}],assignPublicIp=ENABLED}"

EXISTS=$(aws ecs describe-services --region "$R" --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[?status==`ACTIVE`].serviceName | [0]' --output text 2>/dev/null | grep -v None || true)

if [ -n "$EXISTS" ]; then
  log "service exists -> force new deployment on latest task def"
  aws ecs update-service --region "$R" --cluster "$CLUSTER" --service "$SERVICE" \
    --task-definition "$TASK_FAMILY" --force-new-deployment --query 'service.serviceName' --output text
else
  log "creating service $SERVICE"
  aws ecs create-service --region "$R" --cluster "$CLUSTER" --service-name "$SERVICE" \
    --task-definition "$TASK_FAMILY" --desired-count 1 --launch-type FARGATE \
    --enable-execute-command \
    --health-check-grace-period-seconds 180 \
    --network-configuration "$NET" \
    --load-balancers \
      "targetGroupArn=${TG_API_ARN},containerName=auto-agent,containerPort=2020" \
      "targetGroupArn=${TG_WEB_ARN},containerName=web-next,containerPort=3000" \
    --query 'service.serviceName' --output text
fi
log "waiting for service to stabilize (first boot can take a few min)..."
aws ecs wait services-stable --region "$R" --cluster "$CLUSTER" --services "$SERVICE" || warn "not stable yet — check ECS console / logs"
log "11_service done."
