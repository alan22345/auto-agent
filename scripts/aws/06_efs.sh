#!/usr/bin/env bash
# EFS filesystem + access point (/data, owned by uid/gid 1000 = the `node` user)
# + one mount target per public subnet, on the autoagent-efs-sg. This is where
# per-user Claude auth (/data/users/<id>/.claude) persists across redeploys.
# Idempotent via a creation token + name tag.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var EFS_SG_ID; require_var PUBLIC_SUBNET_IDS
R="$AWS_REGION"

EFS_ID=$(aws efs describe-file-systems --region "$R" \
  --query 'FileSystems[?Name==`autoagent-data`].FileSystemId | [0]' --output text 2>/dev/null)
if [ -z "$EFS_ID" ] || [ "$EFS_ID" = None ]; then
  log "Creating EFS filesystem autoagent-data"
  EFS_ID=$(aws efs create-file-system --region "$R" \
    --creation-token autoagent-data --encrypted \
    --performance-mode generalPurpose --throughput-mode bursting \
    --tags Key=Name,Value=autoagent-data \
    --query FileSystemId --output text)
  log "waiting for EFS $EFS_ID to be available"
  while [ "$(aws efs describe-file-systems --region "$R" --file-system-id "$EFS_ID" --query 'FileSystems[0].LifeCycleState' --output text)" != available ]; do sleep 3; done
else
  log "EFS autoagent-data already = $EFS_ID"
fi

# mount targets — one per subnet (skip subnets that already have one)
IFS=',' read -ra SUBNETS <<< "$PUBLIC_SUBNET_IDS"
for sn in "${SUBNETS[@]}"; do
  existing=$(aws efs describe-mount-targets --region "$R" --file-system-id "$EFS_ID" \
    --query "MountTargets[?SubnetId=='$sn'].MountTargetId | [0]" --output text 2>/dev/null)
  if [ -z "$existing" ] || [ "$existing" = None ]; then
    log "mount target in $sn"
    aws efs create-mount-target --region "$R" --file-system-id "$EFS_ID" \
      --subnet-id "$sn" --security-groups "$EFS_SG_ID" --query MountTargetId --output text
  else
    log "mount target in $sn exists ($existing)"
  fi
done

# access point: enforce uid/gid 1000, root dir /data
AP_ID=$(aws efs describe-access-points --region "$R" --file-system-id "$EFS_ID" \
  --query "AccessPoints[?Tags[?Key=='Name'&&Value=='autoagent-data-ap']].AccessPointId | [0]" --output text 2>/dev/null)
if [ -z "$AP_ID" ] || [ "$AP_ID" = None ]; then
  log "Creating access point autoagent-data-ap"
  AP_ID=$(aws efs create-access-point --region "$R" --file-system-id "$EFS_ID" \
    --tags Key=Name,Value=autoagent-data-ap \
    --posix-user Uid=1000,Gid=1000 \
    --root-directory 'Path=/data,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=0755}' \
    --query AccessPointId --output text)
else
  log "access point already = $AP_ID"
fi

cat <<EOF

  paste into config.env:
  export EFS_ID="$EFS_ID"
  export EFS_ACCESS_POINT_ID="$AP_ID"
EOF
log "06_efs done."
