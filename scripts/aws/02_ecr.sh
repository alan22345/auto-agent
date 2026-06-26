#!/usr/bin/env bash
# Create the two ECR repos, the ECS cluster, and the CloudWatch log group.
# Idempotent: re-running is safe (already-exists errors are swallowed).
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var AWS_ACCOUNT_ID

create_repo() {
  local name="$1"
  if aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$name" >/dev/null 2>&1; then
    log "ECR repo $name already exists"
  else
    log "Creating ECR repo $name"
    aws ecr create-repository --region "$AWS_REGION" --repository-name "$name" \
      --image-scanning-configuration scanOnPush=true \
      --query 'repository.repositoryUri' --output text
  fi
}
create_repo "$ECR_AUTOAGENT_REPO"
create_repo "$ECR_WEBNEXT_REPO"

if aws ecs describe-clusters --region "$AWS_REGION" --clusters "$CLUSTER" \
     --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  log "ECS cluster $CLUSTER already active"
else
  log "Creating ECS cluster $CLUSTER"
  aws ecs create-cluster --region "$AWS_REGION" --cluster-name "$CLUSTER" \
    --capacity-providers FARGATE --query 'cluster.clusterName' --output text
fi

if aws logs describe-log-groups --region "$AWS_REGION" --log-group-name-prefix "$LOG_GROUP" \
     --query 'logGroups[?logGroupName==`'"$LOG_GROUP"'`]' --output text | grep -q .; then
  log "Log group $LOG_GROUP already exists"
else
  log "Creating log group $LOG_GROUP"
  aws logs create-log-group --region "$AWS_REGION" --log-group-name "$LOG_GROUP"
  aws logs put-retention-policy --region "$AWS_REGION" --log-group-name "$LOG_GROUP" --retention-in-days 30
fi
log "02_ecr done."
