#!/usr/bin/env bash
# Two IAM roles (idempotent):
#   autoagent-exec : ECS execution role — pull ECR, write logs, read the S3 env
#                    file (environmentFiles), read the DB secrets (bootstrap).
#   autoagent-task : task role — just ECS Exec (SSM) for debugging/bootstrap.
# AWS injects secrets into containers itself; their values never touch this shell.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var AWS_ACCOUNT_ID; require_var ENV_BUCKET
require_var HARPOON_MASTER_SECRET_ARN

TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

ensure_role() {  # ensure_role NAME
  if aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    log "role $1 exists"
  else
    log "creating role $1"
    aws iam create-role --role-name "$1" --assume-role-policy-document "$TRUST" \
      --query 'Role.Arn' --output text
  fi
}

ensure_role autoagent-exec
aws iam attach-role-policy --role-name autoagent-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

EXEC_INLINE=$(cat <<JSON
{"Version":"2012-10-17","Statement":[
  {"Sid":"EnvFile","Effect":"Allow","Action":["s3:GetObject"],"Resource":"arn:aws:s3:::${ENV_BUCKET}/autoagent.env"},
  {"Sid":"EnvBucket","Effect":"Allow","Action":["s3:GetBucketLocation"],"Resource":"arn:aws:s3:::${ENV_BUCKET}"},
  {"Sid":"DbSecrets","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":[
    "${HARPOON_MASTER_SECRET_ARN}",
    "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:autoagent/db-password*"]}
]}
JSON
)
aws iam put-role-policy --role-name autoagent-exec --policy-name autoagent-exec-inline \
  --policy-document "$EXEC_INLINE"

ensure_role autoagent-task
TASK_INLINE='{"Version":"2012-10-17","Statement":[{"Sid":"ECSExec","Effect":"Allow","Action":["ssmmessages:CreateControlChannel","ssmmessages:CreateDataChannel","ssmmessages:OpenControlChannel","ssmmessages:OpenDataChannel"],"Resource":"*"}]}'
aws iam put-role-policy --role-name autoagent-task --policy-name autoagent-task-inline \
  --policy-document "$TASK_INLINE"

EXEC_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/autoagent-exec"
TASK_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/autoagent-task"
cat <<EOF

  paste into config.env:
  export EXECUTION_ROLE_ARN="$EXEC_ARN"
  export TASK_ROLE_ARN="$TASK_ARN"
EOF
log "09_iam done."
