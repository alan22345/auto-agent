#!/usr/bin/env bash
# Build + push the auto-agent and web-next images to ECR (linux/amd64 for
# X86_64 Fargate). Uses a docker-container buildx builder so --push works.
# GITHUB_TOKEN (for the private team-memory build-arg) is read from the env or
# `gh auth token`.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION
cd "$(dirname "$0")/../.."   # repo root

TOKEN="${GITHUB_TOKEN:-$(gh auth token)}"
[ -n "$TOKEN" ] || die "no GITHUB_TOKEN and 'gh auth token' empty — needed for team-memory"

log "ECR login"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

docker buildx inspect autoagent-builder >/dev/null 2>&1 \
  || docker buildx create --name autoagent-builder --driver docker-container
docker buildx use autoagent-builder

log "Build + push auto-agent (linux/amd64) — this is the slow one (emulated)"
docker buildx build --platform linux/amd64 \
  --build-arg GITHUB_TOKEN="$TOKEN" \
  -t "${ECR_AUTOAGENT}:${IMAGE_TAG}" --push .

log "Build + push web-next (linux/amd64)"
docker buildx build --platform linux/amd64 \
  -t "${ECR_WEBNEXT}:${IMAGE_TAG}" --push ./web-next

log "03_build_push done. Images at:"
echo "  ${ECR_AUTOAGENT}:${IMAGE_TAG}"
echo "  ${ECR_WEBNEXT}:${IMAGE_TAG}"
