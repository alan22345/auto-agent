#!/usr/bin/env bash
# Build the S3 environment file the Fargate task loads via environmentFiles.
# Pulls the VM's .env, rewrites DATABASE_URL / REDIS_URL / APP_BASE_URL /
# WORKSPACES_DIR for AWS, uploads to a locked, encrypted S3 bucket.
# Secret values flow through temp files only — never echoed.
#
# Handles secrets (the whole .env + the autoagent DB password). Run yourself if
# the agent's permission layer blocks it: `! bash scripts/aws/05_secrets_env.sh`.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION; require_var ENV_BUCKET; require_var HARPOON_RDS_ENDPOINT; require_var APP_BASE_URL
R="$AWS_REGION"
VM="azureuser@172.190.26.82"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# 1. locked, encrypted bucket
if ! aws s3api head-bucket --bucket "$ENV_BUCKET" 2>/dev/null; then
  log "creating bucket $ENV_BUCKET"
  aws s3api create-bucket --bucket "$ENV_BUCKET" --region "$R" \
    --create-bucket-configuration "LocationConstraint=$R"
  aws s3api put-public-access-block --bucket "$ENV_BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$ENV_BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
else
  log "bucket $ENV_BUCKET exists"
fi

# 2. pull VM .env (secrets — to a temp file, never printed)
log "pulling VM .env"
ssh -o ConnectTimeout=15 -o BatchMode=yes "$VM" 'cat ~/auto-agent/.env' > "$TMP/in.env"
[ -s "$TMP/in.env" ] || die "VM .env came back empty"

# 3. autoagent DB password (our own secret)
PW=$(aws secretsmanager get-secret-value --region "$R" --secret-id autoagent/db-password --query SecretString --output text)
[ -n "$PW" ] || die "autoagent/db-password secret missing — run 04_db.sh first"

# 4. rewrite the AWS-specific keys (python: precise, no echo)
DATABASE_URL="postgresql+asyncpg://autoagent:${PW}@${HARPOON_RDS_ENDPOINT}:5432/autoagent" \
REDIS_URL="redis://localhost:6379/0" \
APP_BASE_URL="$APP_BASE_URL" \
WORKSPACES_DIR="/workspaces" \
IN="$TMP/in.env" OUT="$TMP/out.env" python3 - <<'PY'
import os
overrides = {k: os.environ[k] for k in ("DATABASE_URL","REDIS_URL","APP_BASE_URL","WORKSPACES_DIR")}
seen = set()
lines = []
for raw in open(os.environ["IN"]):
    s = raw.rstrip("\n")
    if "=" in s and not s.lstrip().startswith("#"):
        key = s.split("=", 1)[0].strip()
        if key in overrides:
            lines.append(f"{key}={overrides[key]}"); seen.add(key); continue
    lines.append(s)
for k, v in overrides.items():
    if k not in seen:
        lines.append(f"{k}={v}")
open(os.environ["OUT"], "w").write("\n".join(lines) + "\n")
print(f"wrote {len(lines)} lines ({len(seen)} overridden in place)")
PY

# 5. upload
aws s3 cp "$TMP/out.env" "s3://${ENV_BUCKET}/autoagent.env" --sse AES256 >/dev/null
log "uploaded s3://${ENV_BUCKET}/autoagent.env"
log "05_secrets_env done."
