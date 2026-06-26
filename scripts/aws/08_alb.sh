#!/usr/bin/env bash
# Internet-facing ALB (HTTP :80, locked to CloudFront via the ALB SG) with two
# IP target groups and path rules:
#   /api/*  /ws  /health   -> tg-api  (auto-agent :2020)
#   default (/, UI)        -> tg-web  (web-next   :3000)
# Idempotent-ish: resolves existing ALB/TGs by name before creating.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var VPC_ID; require_var PUBLIC_SUBNET_IDS
require_var ALB_SG_ID
R="$AWS_REGION"
IFS=',' read -ra SUBNETS <<< "$PUBLIC_SUBNET_IDS"

arn_of_lb() { aws elbv2 describe-load-balancers --region "$R" --names autoagent-alb --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null | grep -v None || true; }
arn_of_tg() { aws elbv2 describe-target-groups --region "$R" --names "$1" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null | grep -v None || true; }

ALB_ARN="$(arn_of_lb)"
if [ -z "$ALB_ARN" ]; then
  log "creating ALB autoagent-alb"
  ALB_ARN=$(aws elbv2 create-load-balancer --region "$R" --name autoagent-alb \
    --type application --scheme internet-facing \
    --subnets "${SUBNETS[@]}" --security-groups "$ALB_SG_ID" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
else
  log "ALB exists"
fi

mk_tg() {  # mk_tg NAME PORT HEALTHPATH MATCHER
  local arn; arn="$(arn_of_tg "$1")"
  if [ -z "$arn" ]; then
    log "creating target group $1 (:$2)"
    arn=$(aws elbv2 create-target-group --region "$R" --name "$1" \
      --protocol HTTP --port "$2" --vpc-id "$VPC_ID" --target-type ip \
      --health-check-path "$3" --matcher "HttpCode=$4" \
      --health-check-interval-seconds 30 --healthy-threshold-count 2 --unhealthy-threshold-count 5 \
      --query 'TargetGroups[0].TargetGroupArn' --output text)
  fi
  echo "$arn"
}
TG_API="$(mk_tg autoagent-tg-api 2020 /health 200)"
TG_WEB="$(mk_tg autoagent-tg-web 3000 / 200-399)"

# listener :80 — default to web
LST=$(aws elbv2 describe-listeners --region "$R" --load-balancer-arn "$ALB_ARN" \
  --query 'Listeners[?Port==`80`].ListenerArn | [0]' --output text 2>/dev/null | grep -v None || true)
if [ -z "$LST" ]; then
  log "creating :80 listener (default -> web)"
  LST=$(aws elbv2 create-listener --region "$R" --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=$TG_WEB" \
    --query 'Listeners[0].ListenerArn' --output text)
fi

add_rule() {  # add_rule PRIORITY PATHPATTERN TG_ARN
  aws elbv2 describe-rules --region "$R" --listener-arn "$LST" \
    --query "Rules[?Priority=='$1']" --output text | grep -q . && { log "rule prio $1 exists"; return; }
  aws elbv2 create-rule --region "$R" --listener-arn "$LST" --priority "$1" \
    --conditions "Field=path-pattern,Values=$2" \
    --actions "Type=forward,TargetGroupArn=$3" >/dev/null
  log "rule prio $1 ($2 -> api)"
}
add_rule 10 '/api/*'  "$TG_API"
add_rule 20 '/ws'     "$TG_API"
add_rule 30 '/health' "$TG_API"

ALB_DNS=$(aws elbv2 describe-load-balancers --region "$R" --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)
cat <<EOF

  paste into config.env:
  export ALB_ARN="$ALB_ARN"
  export ALB_DNS="$ALB_DNS"
  TG_API=$TG_API
  TG_WEB=$TG_WEB
EOF
log "08_alb done."
