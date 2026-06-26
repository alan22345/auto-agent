# Auto-agent on AWS ECS Fargate — migration runbook

Moves auto-agent off the single Azure VM (docker-compose) onto **AWS ECS Fargate**,
reusing the existing **harpoon RDS** instance for the database. Lift-and-shift:
no application code changes, `max_concurrent_workers` stays at 2.

## Target architecture

```
                Route53  autoagent.<domain>
                     │  (CNAME/alias)
              ┌──────▼───────┐  HTTPS :443  (ACM cert)
              │     ALB      │  /            → web-next  :3000
              │ (stable DNS) │  /api,/ws,... → auto-agent :2020
              └──────┬───────┘
        ┌────────────▼──────────────┐  ECS Fargate task (desired = 1)
        │  auto-agent  :2020         │  ← the monolith, max_concurrent_workers=2
        │  web-next    :3000         │  ← Next.js
        │  redis       (sidecar)     │  ← non-durable pub/sub + fail-open lease
        └──┬─────────────┬───────────┘
           │ /data (EFS) │ DATABASE_URL
           ▼             ▼
      EFS access point   harpoon RDS  →  database "autoagent" + role "autoagent"
      (Claude auth)       (managed backups + PITR — reused, ~$0 incremental)
```

- **Compute**: one Fargate service, one task, three containers. 1 vCPU / 4 GB
  (bump memory if Chromium/tests OOM — see `task-def.template.json`).
- **DB**: a dedicated `autoagent` database on the harpoon RDS instance. Auto-agent's
  Alembic migrations run unchanged (own database = own `public` schema, no collisions).
- **Config**: the whole `.env` lives in S3 (`s3://${ENV_BUCKET}/autoagent.env`) and is
  injected via ECS `environmentFiles`. Validated safe: every value is single-line.
  `05_secrets_env.sh` rewrites `DATABASE_URL`, `REDIS_URL`, `APP_BASE_URL` for AWS.
- **State**: per-user Claude auth (`/data/users/<id>/.claude/`) on **EFS** (survives
  redeploys). `workspaces` stays ephemeral on the task's local storage.
- **Ingress**: ALB → stable DNS + HTTPS (also unblocks Slack OAuth, which needs TLS).
- **Build/deploy**: images build in CI → ECR; deploy = `update-service --force-new-deployment`.

## Why this shape (decisions)

| Decision | Rationale |
|---|---|
| Reuse harpoon RDS (new DB, not new instance) | ~$0 incremental vs ~$12-25/mo for a fresh RDS |
| Dedicated **database**, not a Postgres schema | Alembic + models assume `public`; a separate DB avoids `search_path` hacks and table collisions with harpoon |
| Redis as a **sidecar**, not ElastiCache | Non-durable (events + fail-open lease); ~$15/mo cheaper, one less managed thing |
| Single task, desired=1 (no horizontal scale) | The monolith runs ~15 singleton loops + non-atomic task claim; 2 concurrent workers already run in-process. Scaling = bigger task + higher cap, not more replicas |
| **S3 environmentFiles**, not Secrets Manager | All `.env` values are single-line; S3 (locked + SSE) is the minimal lift. Upgrade path noted below |
| **ALB**, not raw public IP | Fargate IPs change every deploy → would break GitHub/Linear webhooks + OAuth. ALB DNS is stable |

## Order of operations

`source config.env` is done by every script. Fill `config.env` as you go.

```
0.  aws sso login                       # YOU — interactive
1.  ./01_discover.sh                     # prints account/region/VPC/subnets/harpoon RDS/Route53
                                         #   → paste results into config.env
2.  ./02_ecr.sh                          # create 2 ECR repos
3.  ./03_build_push.sh                   # build+push auto-agent + web-next images
4.  ./04_db.sh                           # create autoagent DB + role on harpoon RDS → sets DATABASE_URL
5.  ./06_efs.sh                          # EFS + access point + mount targets + EFS SG
6.  ./07_security.sh                     # autoagent task SG, ALB SG, ingress to RDS + EFS
7.  ./08_alb.sh                          # ACM cert (DNS-validated) + ALB + target groups + HTTPS listener
                                         #   → sets ALB_DNS / DOMAIN_NAME / APP_BASE_URL
8.  ./09_iam.sh                          # task execution role (ECR/S3/logs) + task role
9.  ./05_secrets_env.sh                  # pull VM .env, rewrite DB/REDIS/APP_BASE_URL, upload to S3
10. ./10_taskdef.sh                      # render task-def.json from template + register
11. ./11_service.sh                      # create ECS service wired to the ALB target groups
12. ./12_migrate.sh                      # pg_dump VM → harpoon autoagent; tar userdata → seed EFS; restart
13. cutover: point GitHub/Linear webhooks + OAuth callbacks at APP_BASE_URL; smoke test
14. HAND OFF — user verifies. Azure VM teardown ONLY after user confirms (see below).
```

> Step 4 (DB) and 5/6/8 (EFS/ALB) feed values that step 9 (`05_secrets_env.sh`) and
> step 10 (`10_taskdef.sh`) need, which is why the env file + task def are rendered late.

## Data migration (step 12) — done with the new service already up

1. **Postgres**: `pg_dump` the VM's container DB, `pg_restore`/`psql` into the new
   `autoagent` DB on harpoon. Carries `alembic_version`, so future migrations continue.
   Brief read-only window on the VM while dumping to avoid drift.
2. **Claude auth**: `tar` the VM `userdata` volume (`/data/users`) and unpack onto the
   EFS access-point root so every user stays paired (no re-auth).

## Cutover & Azure teardown (DEFERRED — user-gated)

The Azure VM stays running until the user verifies the AWS deployment. After sign-off:

```
# 1. confirm nothing points at the VM anymore (webhooks, DNS, bookmarks)
# 2. final DB delta dump if any tasks ran on the VM post-cutover
# 3. stop the VM, snapshot if desired, then:
az vm deallocate -g AUTO-AGENT-RG -n auto-agent-vm     # stop billing for compute
#    ...verify a few days..., then delete RG resources when confident.
```

Do **not** run teardown automatically. It is the last, explicit, user-approved step.

## Upgrade paths (later, not now)

- **Secrets Manager** instead of S3 env file (per-key rotation) — swap `environmentFiles`
  for `secrets` in the task def.
- **Bedrock via task role** instead of static `AWS_*` keys in the env file.
- **Horizontal scale**: split web/worker roles + run-once singleton loops + atomic
  `SELECT … FOR UPDATE SKIP LOCKED` task claim. Only if one task can't keep up.
