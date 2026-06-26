#!/usr/bin/env bash
# Read-only. After `aws sso login`, prints the coordinates you paste into
# config.env. Touches nothing — safe to re-run.
source "$(dirname "$0")/lib.sh"

log "Caller identity"
aws sts get-caller-identity --output table

REGION="$(aws configure get region || true)"
log "Default region: ${REGION:-<none — set AWS_REGION>}"

log "harpoon RDS instances (look for the harpoon db; note Endpoint + VpcSecurityGroupId + DBSubnetGroup VPC)"
aws rds describe-db-instances \
  --query 'DBInstances[].{Id:DBInstanceIdentifier,Engine:Engine,Endpoint:Endpoint.Address,Port:Endpoint.Port,VPC:DBSubnetGroup.VpcId,SG:VpcSecurityGroups[0].VpcSecurityGroupId,Status:DBInstanceStatus}' \
  --output table

log "Secrets Manager secrets that look like RDS master creds (for HARPOON_MASTER_SECRET_ARN)"
aws secretsmanager list-secrets \
  --query 'SecretList[?contains(Name, `rds`) || contains(Name, `harpoon`)].{Name:Name,ARN:ARN}' \
  --output table || warn "no secretsmanager:ListSecrets permission — find the master creds another way"

log "VPCs (pick harpoon's VPC_ID — should match the RDS VPC above)"
aws ec2 describe-vpcs \
  --query 'Vpcs[].{VpcId:VpcId,Cidr:CidrBlock,Default:IsDefault,Name:Tags[?Key==`Name`]|[0].Value}' \
  --output table

if [ -n "${VPC_ID:-}" ]; then
  log "Public subnets in ${VPC_ID} (MapPublicIpOnLaunch=true — pick >=2 across AZs for PUBLIC_SUBNET_IDS)"
  aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'Subnets[].{Subnet:SubnetId,AZ:AvailabilityZone,Public:MapPublicIpOnLaunch,Cidr:CidrBlock,Name:Tags[?Key==`Name`]|[0].Value}' \
    --output table
else
  warn "VPC_ID not set yet — set it from the table above, then re-run to list its subnets."
fi

log "Route53 hosted zones (pick HOSTED_ZONE_ID + DOMAIN_NAME for the ACM cert; skip if you'll use the ALB DNS over HTTP first)"
aws route53 list-hosted-zones \
  --query 'HostedZones[].{Zone:Name,Id:Id,Private:Config.PrivateZone}' \
  --output table || warn "no route53 access — we'll fall back to the ALB DNS name."

log "Done. Paste the relevant values into config.env, then run ./02_ecr.sh"
