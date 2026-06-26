#!/usr/bin/env bash
# SHARED-PROD CHANGE — run with eyes open.
# Adds ONE ingress rule to harpoon-prod's RDS security group: allow Postgres
# (5432) from autoagent-task-sg. This exactly mirrors the existing
# "from harpoon-prod-tasks" rule, scoped SG-to-SG (no CIDR widening). It does
# not touch harpoon's own access. Idempotent.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var VPC_ID; require_var HARPOON_RDS_SG_ID
R="$AWS_REGION"

TASK_SG=$(aws ec2 describe-security-groups --region "$R" \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=autoagent-task-sg" \
  --query 'SecurityGroups[0].GroupId' --output text)
[ -n "$TASK_SG" ] && [ "$TASK_SG" != None ] || die "autoagent-task-sg not found — run 07_security.sh first"

log "Adding 5432 ingress on $HARPOON_RDS_SG_ID  <-  $TASK_SG (autoagent-task-sg)"
aws ec2 authorize-security-group-ingress --region "$R" --group-id "$HARPOON_RDS_SG_ID" \
  --ip-permissions "IpProtocol=tcp,FromPort=5432,ToPort=5432,UserIdGroupPairs=[{GroupId=$TASK_SG,Description=from-autoagent-tasks}]" \
  2>&1 | grep -v 'InvalidPermission.Duplicate' || true
log "done."
