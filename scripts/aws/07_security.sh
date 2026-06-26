#!/usr/bin/env bash
# Security groups, resolved-or-created by name (idempotent):
#   autoagent-alb-sg   : inbound 80 from CloudFront origin-facing prefix list
#   autoagent-task-sg  : inbound 2020/3000 from the ALB SG
#   autoagent-efs-sg   : inbound 2049 (NFS) from the task SG
# Plus: ingress on harpoon RDS SG (5432) from the task SG — mirrors harpoon-prod.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var VPC_ID; require_var HARPOON_RDS_SG_ID
R="$AWS_REGION"

sg_id() {  # sg_id NAME -> id or empty
  aws ec2 describe-security-groups --region "$R" \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=$1" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null | grep -v None || true
}
ensure_sg() {  # ensure_sg NAME DESC -> id (created if missing)
  local id; id="$(sg_id "$1")"
  if [ -z "$id" ]; then
    id=$(aws ec2 create-security-group --region "$R" --group-name "$1" \
      --description "$2" --vpc-id "$VPC_ID" --query GroupId --output text)
    log "created $1 = $id"
  else
    log "$1 already = $id"
  fi
  echo "$id"
}
# authorize an ingress rule, ignoring the Duplicate error on re-run
allow() { aws ec2 authorize-security-group-ingress --region "$R" "$@" 2>&1 | grep -v 'InvalidPermission.Duplicate' || true; }

ALB_SG_ID="$(ensure_sg autoagent-alb-sg 'autoagent ALB ingress')"
AUTOAGENT_SG_ID="$(ensure_sg autoagent-task-sg 'autoagent Fargate task')"
EFS_SG_ID="$(ensure_sg autoagent-efs-sg 'autoagent EFS mount targets')"

# CloudFront origin-facing managed prefix list (so only CloudFront hits the ALB)
CF_PL=$(aws ec2 describe-managed-prefix-lists --region "$R" \
  --filters "Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing" \
  --query 'PrefixLists[0].PrefixListId' --output text)
log "CloudFront origin-facing prefix list: $CF_PL"

log "ALB SG <- :80 from CloudFront"
allow --group-id "$ALB_SG_ID" --ip-permissions \
  "IpProtocol=tcp,FromPort=80,ToPort=80,PrefixListIds=[{PrefixListId=$CF_PL,Description=cloudfront-origin}]"

log "task SG <- :2020 and :3000 from ALB SG"
allow --group-id "$AUTOAGENT_SG_ID" --protocol tcp --port 2020 --source-group "$ALB_SG_ID"
allow --group-id "$AUTOAGENT_SG_ID" --protocol tcp --port 3000 --source-group "$ALB_SG_ID"

log "EFS SG <- :2049 from task SG"
allow --group-id "$EFS_SG_ID" --protocol tcp --port 2049 --source-group "$AUTOAGENT_SG_ID"

# NOTE: the harpoon-prod RDS SG ingress is a change to a SHARED PROD resource and
# is applied separately, with explicit approval, by 07b_rds_ingress.sh.

cat <<EOF

  paste into config.env:
  export ALB_SG_ID="$ALB_SG_ID"
  export AUTOAGENT_SG_ID="$AUTOAGENT_SG_ID"
  export EFS_SG_ID="$EFS_SG_ID"

  next: review + run 07b_rds_ingress.sh to allow this task SG into harpoon RDS.
EOF
log "07_security done (autoagent SGs only)."
