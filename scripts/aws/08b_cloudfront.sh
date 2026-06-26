#!/usr/bin/env bash
# CloudFront distribution in front of the ALB — gives a stable *.cloudfront.net
# HTTPS URL (valid default cert, no domain needed) and is the only thing allowed
# to reach the ALB. Caching disabled (dynamic app); all methods + WebSockets
# forwarded via the AllViewer origin request policy.
source "$(dirname "$0")/lib.sh"
require_var ALB_DNS
CALLER="autoagent-cf-v1"   # stable -> re-runs find the existing distribution

# already exists? (match on Comment)
EXISTING=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?Comment=='autoagent'].Id | [0]" --output text 2>/dev/null | grep -v None || true)
if [ -n "$EXISTING" ]; then
  DOMAIN=$(aws cloudfront get-distribution --id "$EXISTING" --query 'Distribution.DomainName' --output text)
  log "CloudFront already exists: $EXISTING ($DOMAIN)"
else
  CFG=$(cat <<JSON
{
  "CallerReference": "${CALLER}",
  "Comment": "autoagent",
  "Enabled": true,
  "PriceClass": "PriceClass_100",
  "Origins": { "Quantity": 1, "Items": [ {
    "Id": "alb",
    "DomainName": "${ALB_DNS}",
    "CustomOriginConfig": {
      "HTTPPort": 80, "HTTPSPort": 443,
      "OriginProtocolPolicy": "http-only",
      "OriginSslProtocols": { "Quantity": 1, "Items": ["TLSv1.2"] },
      "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5
    }
  } ] },
  "DefaultCacheBehavior": {
    "TargetOriginId": "alb",
    "ViewerProtocolPolicy": "redirect-to-https",
    "Compress": true,
    "AllowedMethods": {
      "Quantity": 7,
      "Items": ["GET","HEAD","OPTIONS","PUT","POST","PATCH","DELETE"],
      "CachedMethods": { "Quantity": 2, "Items": ["GET","HEAD"] }
    },
    "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
    "OriginRequestPolicyId": "216adef6-5c7f-47e4-b989-5492eafa07d3"
  },
  "ViewerCertificate": { "CloudFrontDefaultCertificate": true }
}
JSON
)
  log "creating CloudFront distribution"
  OUT=$(aws cloudfront create-distribution --distribution-config "$CFG")
  EXISTING=$(echo "$OUT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Distribution"]["Id"])')
  DOMAIN=$(echo "$OUT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["Distribution"]["DomainName"])')
  log "created $EXISTING ($DOMAIN) — propagation takes ~5-15 min"
fi

cat <<EOF

  paste into config.env:
  export CLOUDFRONT_ID="$EXISTING"
  export CLOUDFRONT_DOMAIN="$DOMAIN"
  export APP_BASE_URL="https://$DOMAIN"
EOF
log "08b_cloudfront done."
