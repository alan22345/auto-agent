## 1. What this service is, in one paragraph

A **GTM (go-to-market) outbound automation service** that owns the
strategy ends of a cold-outbound campaign and vendors the middle. We
own the **entry point** (turning prose ICP + product descriptions into
a structured lead-gen query) and the **exit point** (a funnel system of
record + experiment loop). The middle — lead generation, email
sequencing, LinkedIn sequencing, mailbox infrastructure, warming — is
vendored across **Clay**, **Salesforge**, **Litemail** and
**Warmforge**. Our service stitches those vendors together via webhooks
and APIs, drives an operator UI for the campaign lifecycle (draft →
enriching → drafting → validating → awaiting_review → sending →
replies_flowing → archived), and lands every event into a single funnel
table so every choice (ICP, prompt, variant, channel, mailbox, send
time, …) is a slice-able experiment dimension.

The operator-facing primitive is a **campaign**: one ICP + product +
prompt configuration, optionally with LinkedIn enabled. Each campaign
spawns one or more **runs** (Clay enrichment passes). Runs produce
leads; leads produce drafts (one per channel); a Haiku validator gates
every draft; the operator approves (or auto-approve ships passing email
drafts); approved drafts flow into a Salesforge sequence; replies flow
back through webhooks, are classified by Haiku, and write to the funnel.

---

## 2. Scope — what to build vs. what to mock

### 2.1 External vendors — MOCK these

| Vendor | What it does (in reality) | How to mock |
|---|---|---|
| **Clay** | Lead gen + enrichment + email verification. Workflow: criteria row in (via Clay table webhook) → async enrichment → per-lead callbacks to our `/webhooks/clay` | Build a **fake Clay**: an HTTP server that accepts criteria-row POSTs, then on a timer (or via a "fire callbacks now" admin endpoint) POSTs synthetic enriched-lead payloads to our `/webhooks/clay`. Include the `Authorization: Bearer <callback_token>` header. Generate leads with realistic provenance fields (`email_found_provider`, `email_verification_status`, `signals[]`). |
| **Salesforge** | Email + LinkedIn sequencing, sending, reply ingestion | Build a **fake Salesforge** with two surfaces: (a) contact upsert + sequence-assign endpoints (email v2 + multichannel); (b) a webhook-emitter that fires `email_sent` / `email_opened` / `email_replied` / `email_bounced` / `linkedin_connection_requested` / `linkedin_connection_accepted` / `linkedin_message_sent` / `linkedin_replied` / `contact_unsubscribed` / `dnc_added` / `mailbox_paused` / `mailbox_resumed` to our `/webhooks/salesforge` on demand or on a schedule. Include a thread-fetch endpoint that returns synthetic reply bodies. |
| **Litemail** | Mailbox provisioning + DNS | No direct integration on our side — Salesforge talks to it. Mock only as **seed data** in our `mailboxes` table (mailbox_id, sending_domain, status). |
| **Warmforge** | Mailbox warming | No direct integration on our side. Same as Litemail — mock as configuration if anything. |
| **Slack** | Operator notifications | Mock as a sink: any "send DM" call writes to a `mock_notifications` table and (optionally) to stdout. |
| **LLM provider** | Sonnet (ICP translation + copywriting), Haiku (validator + reply classifier) | Behind an interface (`LLMProvider` style — auto-agent has this pattern in `agent/llm/base.py`). **Real Claude API calls are fine in dev**; mockable via a stub provider for deterministic tests. Mock is mandatory for unit tests, optional in dev. |
| **Calendar + CRM** | `meeting_booked_at`, `meeting_held_at`, `opp_created_at` | Out of v1 scope. Leave columns nullable; no integration code yet. |

**Why mock these, not build against real APIs:** Clay has no traditional
REST surface (its API is "webhook in / HTTP-API-column out"), and
Salesforge integration assumptions (webhook payload shape, customVars
round-tripping, mailbox event names) are **flagged "confirm on first
integration"** in the architecture doc. Building against fakes lets us
ship the orchestration + UI + funnel and finalise vendor contracts in
parallel.

### 2.2 What to build — OWN the rest

Everything below this line is in-scope:

- **ICP translator** — Sonnet structured-output call: prose → criteria
  row matching our schema.
- **Orchestrator** — campaign + run state machine, drives all transitions.
- **Clay integration layer** — outbound row POST, inbound callback
  handler, run-completion detector (3 signals: per-row callback, 15-min
  quiescence, count safety-net).
- **Copywriter** — Sonnet call producing per-channel drafts (email and,
  when LinkedIn is enabled and `person_linkedin_url` is present, also a
  LinkedIn draft) per lead.
- **Validator** — Haiku call, gates 100% of outgoing drafts on both
  channels. Email checks: tone match, factual grounding, CTA
  well-formed, banned phrases. LinkedIn checks (additional): ≤200-char
  note, no links in connection request, voice consistency. Retry-on-fail
  capped at 1.
- **Salesforge integration layer** — contact upsert by channel, sequence
  assign by channel, webhook handler, thread-fetch for reply bodies,
  `sf_contact_id → (lead_id, channel)` resolution via
  `gtm_sequence_pushes`.
- **Reply classifier** — Haiku call: positive / neutral / negative / OOO.
- **Notification dispatcher** — positive → Slack DM (real-time); others
  → inbox badge only. Dedup key `(lead_id, sf_message_id)` or
  `(lead_id, classification, day)`.
- **Cross-campaign person dedup** — 3-index join on `gtm_leads`
  (`email_address`, `person_linkedin_url`, `phone`) at drafting time.
- **Funnel SoR** — `gtm_leads`, `gtm_runs`, `gtm_campaigns`,
  `gtm_campaign_runs`, `gtm_drafts`, `gtm_sequence_pushes`,
  `gtm_notifications`, `gtm_suppressions`, `mailboxes`, plus a per-step
  events table keyed by `(run_id, lead_id, channel, step_idx, …)`.
- **Operator UI** — five surfaces per campaign: Setup, Enrichment
  monitor, Draft review queue, Reply inbox, Funnel report. Plus a
  global reply inbox view. **Styling direction: light theme, soft
  pastel-ish colours, soft borders** (low-saturation accents, generous
  whitespace, 1px borders in a muted neutral, rounded corners). The
  operator is in this UI for hours at a time reviewing copy — eye
  comfort matters. No dark mode in v1.
- **Callback-token mint + verify** — HMAC-SHA256 hashed; long-lived per
  run; invalidated on `gtm_runs.status = done`.
- **Quiescence detector** — background job that flips
  `gtm_runs.status = done` when no Clay callbacks for 15 min after at
  least one landed (+ count safety net).
- **LinkedIn capacity counter** — query over funnel events for the
  current calendar month; soft-advisory display, SF is the authoritative
  gate.

---

## 3. Locked architectural decisions (don't re-litigate)

The architecture doc went through v1 → v6 with multiple grilling rounds.
These are **closed**:

1. **One evergreen Salesforge sequence per channel per workspace.** Not
   per cohort. Cohort identity rides on contact `customVars` + `tags`.
2. **Validator gates 100% of outgoing** on both channels. No bypass.
   `auto_approve` mode skips the *human* step, not the validator step.
3. **LinkedIn drafts ALWAYS land in `awaiting_review`** regardless of
   campaign approval mode. Personal-account = non-negotiable human gate.
4. **Channel choice is per-lead at draft-approval time.** Eligible
   leads get both an email and a LinkedIn draft; operator picks one;
   approving one auto-rejects the other.
5. **Validator-fail behavior:** copywriter regenerates once with reasons
   fed back. 2nd fail → manual review with both attempts visible. Retry
   capped at 1.
6. **Cross-campaign person dedup:** 3-index join on `gtm_leads`
   (`email_address`, `person_linkedin_url`, `phone`). Hit on any of
   the three = excluded. No new `gtm_persons` table in v1. Active
   campaigns = status ∈ {enriching, drafting, validating,
   awaiting_review, sending, replies_flowing}.
7. **Mailbox rotation is Salesforge's job.** We consume
   `mailbox_paused` / `mailbox_resumed` webhooks for funnel attribution.
   No rotation logic on our side.
8. **Suppression is SF-owned.** SF injects the unsubscribe footer. We
   mirror SF DNC into `gtm_suppressions` read-only. No upstream push.
9. **Run completion = 3 signals OR'd:** end-of-workflow per-row
   callback, 15-min quiescence after at least one callback, count
   safety net (`received ≥ max_companies × max_people_per_company`).
10. **Per-cohort freeze is terminal.** Bulk-adds `sf_contact_id`s to
    SF workspace DNC. Resume not supported on legacy email API.
11. **`callback_token`** is long-lived per run, no rotation. Stored
    hashed (HMAC-SHA256 + server-side pepper).
12. **No cross-channel branching.** Email and LinkedIn sequences run
    independently; a lead can be in both, attribution stays clean
    because every event is channel-tagged.
13. **LinkedIn capacity counter is soft-advisory.** SF is the
    authoritative gate.
14. **No materialized `gtm_companies` table in v1.** Companies are
    derived on demand from `gtm_leads` grouped by company domain.
15. **State machine:** `draft → enriching → drafting → validating →
    awaiting_review → sending → replies_flowing → archived`. Plus
    `paused`, `frozen` (terminal), `cancelled` as side transitions.

---

## 4. Data model (full v1 schema)

```
gtm_campaigns
├─ id (PK)
├─ operator_id
├─ icp_text, product_text          -- operator prose
├─ variant_id                       -- which framing
├─ prompt_revision                  -- email copywriting prompt rev
├─ linkedin_prompt_revision         -- nullable, only when 'linkedin' in channels
├─ icp_translation_revision         -- which entry-prompt produced criteria
├─ channels                         -- ['email'] or ['email','linkedin']
├─ approval_mode                    -- 'manual_review' | 'auto_approve' (email-only)
├─ status                           -- state-machine state
├─ launched_at, archived_at, frozen_at

gtm_runs                            -- Clay-facing technical record (1:N from campaign)
├─ id (PK)                          -- this is run_id
├─ campaign_id (FK, optional in v1; required via gtm_campaign_runs)
├─ criteria_rows                    -- jsonb, LLM-produced
├─ callback_token_hash              -- HMAC-SHA256 + pepper
├─ max_companies, max_people_per_company
├─ status                           -- 'running' | 'done' | 'failed'
├─ created_at, completed_at

gtm_campaign_runs                   -- join: campaign × run, ordinal
├─ campaign_id (FK), run_id (FK)
├─ ordinal                          -- 1st run, 2nd refresh, ...
└─ PRIMARY KEY (campaign_id, run_id)

gtm_leads                           -- one row per Clay callback
├─ lead_id (UUID PK)
├─ run_id (FK)
├─ criteria_row_id                  -- sub-cohort identity
├─ clay_row_id                      -- Clay's stable id; UNIQUE(run_id, clay_row_id)
├─ person (JSONB)                   -- name, title, seniority, dept, tenure, linkedin_url
├─ company (JSONB)                  -- name, domain, industry, size, geo, funding
├─ email_address                    -- promoted scalar; INDEX
├─ email_verification_status        -- promoted scalar
├─ email_found_provider             -- promoted scalar; analytics dim
├─ person_linkedin_url              -- promoted scalar from person JSONB; INDEX (for dedup)
├─ phone                            -- promoted scalar; INDEX (for dedup)
├─ sources (JSONB)                  -- provenance per field
├─ signals (JSONB)
├─ raw (JSONB)                      -- forensic Clay payload
├─ received_at

gtm_drafts                          -- one row per (campaign × lead × channel)
├─ id (PK)
├─ campaign_id (FK), lead_id (FK), channel  -- UNIQUE(campaign_id, lead_id, channel)
├─ subject                          -- email only
├─ attempt_1_body
├─ attempt_2_body                   -- nullable, populated only on validator-fail retry
├─ original_body                    -- pre-edit
├─ status                           -- 'pending_review' | 'approved' | 'auto_approved'
│                                   --   | 'rejected' | 'excluded_dedup'
├─ validator_status                 -- 'pass' | 'fail' | 'pass_on_retry'
├─ validator_reasons (JSONB)        -- final-attempt
├─ attempt_1_validator_reasons (JSONB)   -- nullable
├─ dedup_blocked_by_campaign_id     -- nullable; set when status='excluded_dedup'
├─ reviewer_id, reviewed_at, validated_at, edit_diff

gtm_sequence_pushes                 -- one row per (campaign × lead × channel) pushed to SF
├─ campaign_id (FK), lead_id (FK), channel
├─ sf_contact_id                    -- INDEX (webhook-handler lookup)
├─ sequence_id
├─ pushed_at
└─ PRIMARY KEY (campaign_id, lead_id, channel)

gtm_funnel_events                   -- one row per per-step event (the SoR)
├─ id (PK)
├─ run_id, lead_id, campaign_id, channel, step_idx
├─ variant_id, prompt_revision, linkedin_prompt_revision,
│  icp_translation_revision
├─ event_type                       -- scheduled | sent | opened | clicked | replied
│                                   --   | bounced | unsubscribed | connection_requested
│                                   --   | connection_accepted | message_sent
│                                   --   | mailbox_paused | mailbox_resumed | ...
├─ occurred_at
├─ mailbox_id, sending_domain       -- email events
├─ linkedin_account_id              -- linkedin events
├─ reply_body                       -- replied events
├─ reply_classification             -- 'positive'|'neutral'|'negative'|'OOO'
├─ sf_message_id, sf_thread_id      -- replied events; idempotency
├─ raw (JSONB)                      -- forensic SF webhook payload

gtm_notifications
├─ id (PK)
├─ campaign_id, lead_id, channel, classification
├─ dispatched_at
├─ external_id                      -- Slack ts / message-id
└─ UNIQUE(lead_id, sf_message_id)   -- idempotency across SF retries

gtm_suppressions                    -- read-only mirror of SF DNC
├─ email_address (PK)
├─ source                           -- 'sf_unsubscribe' | 'sf_dnc_added' | 'sf_bounce'
├─ mirrored_at

mailboxes
├─ mailbox_id (PK)
├─ sending_domain
├─ status                           -- 'active' | 'paused' | 'warming'
├─ paused_at, resumed_at, paused_reason
```

---

## 5. Key API surfaces

### 5.1 HTTP endpoints we expose

| Method | Path | Caller | Purpose |
|---|---|---|---|
| POST | `/icp/translate` | UI | prose → criteria rows preview |
| POST | `/campaigns` | UI | create campaign (status=draft) |
| POST | `/campaigns/{id}/launch` | UI | draft → enriching; mint callback_token, POST to Clay |
| POST | `/webhooks/clay` | Clay | per-lead enrichment callback (bearer auth) |
| POST | `/webhooks/salesforge` | SF | per-event delivery (path token or signature) |
| GET | `/campaigns/{id}` | UI | full campaign state |
| GET | `/campaigns/{id}/leads` | UI | enrichment monitor data |
| GET | `/campaigns/{id}/drafts` | UI | review queue |
| POST | `/drafts/{id}/approve` | UI | approve one channel for a lead, auto-reject other |
| POST | `/drafts/{id}/reject` | UI | reject a draft |
| POST | `/drafts/{id}/edit` | UI | inline edit body |
| GET | `/campaigns/{id}/replies` | UI | reply inbox per campaign |
| GET | `/replies` | UI | global reply inbox |
| GET | `/campaigns/{id}/funnel` | UI | funnel + cohort cuts |
| POST | `/campaigns/{id}/freeze` | UI | terminal cohort freeze (bulk SF DNC) |
| POST | `/campaigns/{id}/pause` | UI | non-terminal pause |
| POST | `/campaigns/{id}/archive` | UI | replies_flowing → archived |

### 5.2 Outbound calls we make

| To | Surface | Purpose |
|---|---|---|
| Clay | per-table webhook URL | POST criteria rows on launch |
| SF (email v2) | `/workspaces/{ws}/contacts` + `/sequences/{id}/assign` | upsert email contacts, assign to email sequence |
| SF (multichannel) | node/condition graph contact + enrollment endpoints | upsert LinkedIn contacts, assign to LinkedIn sequence |
| SF | thread-fetch / message-fetch | pull reply body when `*_replied` webhook arrives |
| SF | workspace DNC bulk-add | terminal cohort freeze |
| LLM | Sonnet (structured output) | ICP translation, copywriting |
| LLM | Haiku | validator, reply classifier |
| Slack | DM webhook / API | positive-reply real-time notification |

---

## 6. State machines

### 6.1 Campaign

```
draft ──launch──> enriching ──run done──> drafting ──per-draft──> validating
                                                                       │
                                                                       ├── pass / pass_on_retry
                                                                       │      ├── email + auto_approve → (auto-ship)
                                                                       │      └── otherwise → awaiting_review
                                                                       │
                                                                       └── fail (final) → awaiting_review

awaiting_review ──all approvals decided──> sending ──first event lands──> replies_flowing ──operator──> archived

   side transitions (from any non-terminal state):
     ─paused─> (in-flight SF sends complete; new transitions blocked)
     ─frozen─> TERMINAL (SF DNC bulk-add)
     ─cancelled─> TERMINAL
```

### 6.2 Run

```
running ──first callback──> running
running ──15-min quiescence after ≥1 callback──> done
running ──count >= max_companies × max_per_company──> done
running ──admin/manual fail──> failed
```

### 6.3 Draft

```
pending_review ──operator approve──> approved
pending_review ──operator reject──> rejected
(at creation, if dedup blocked)──> excluded_dedup
(at creation, if email + auto_approve + validator pass on attempt 1 or 2)──> auto_approved
```

---

## 7. Critical invariants (load-bearing — don't break)

1. **`run_id` is OUR tag**, not a Clay concept. We stamp it on every
   criteria row; Clay carries it through via explicit column mapping;
   it echoes back on every callback. Demuxing happens on our side.
2. **`callback_token` is hashed at rest.** Plaintext only exists during
   the launch POST → Clay column write. We never store plaintext.
3. **Webhook `sf_contact_id → (lead_id, channel)` resolution is O(1)
   via `gtm_sequence_pushes`.** Don't add a join-fan-out resolver.
4. **One channel per lead per campaign actually ships.** Approving one
   channel's draft MUST auto-reject the other. UI + backend both
   enforce this; the `gtm_drafts.status` transition for the rejected
   draft is part of the same transaction.
5. **`gtm_funnel_events.channel` is non-nullable.** Every event is
   channel-tagged at write time. Funnel reports filter on it.
6. **LinkedIn drafts cannot auto-ship.** The validator may pass them,
   but the routing matrix MUST land them in `awaiting_review` regardless
   of campaign approval mode.
7. **Validator runs even in `auto_approve` mode.** The mode skips human,
   not validation.
8. **Dedup check happens at drafting time, not later.** The dedup query
   must read active-status campaigns only.
9. **SF DNC is read-only for us.** No upstream push. `gtm_suppressions`
   is a mirror, not a source.
10. **Reply notifications dedup on `(lead_id, sf_message_id)`.** SF
    can re-fire webhooks; do not double-page.

---

## 8. Suggested build order

The architecture is layered such that the funnel SoR + Clay loop can
ship before anything else and prove value end-to-end. A recommended
phasing:

### Phase 1 — skeleton, mocked vendors, single happy path
1. Database schema (all tables above) + migrations.
2. Mock Clay server (criteria-row in, callbacks out on admin trigger).
3. Mock Salesforge server (contact upsert, sequence assign, webhook
   emitter on admin trigger, thread-fetch).
4. ICP translator (Sonnet structured output) + `/icp/translate`.
5. Campaign create + launch endpoints.
6. Clay launch flow: callback_token mint, criteria-row POST to mock Clay.
7. `/webhooks/clay` callback handler with bearer validation, upsert
   into `gtm_leads`.
8. Run-completion detector (per-row callback path first; quiescence +
   count safety net second).
9. Smoke test: launch a campaign, see leads land.

### Phase 2 — drafting + validator + manual review
10. Copywriter (Sonnet) — generates email drafts only.
11. Validator (Haiku) — email checks only.
12. Validator-fail retry-once loop.
13. Cross-campaign dedup at drafting time (email + linkedin_url + phone).
14. Draft review queue UI (one row per lead, validator output inline).
15. Approve / reject / edit endpoints.
16. SF contact upsert + email sequence assign (mock SF).
17. `/webhooks/salesforge` handler for `email_sent` / `email_opened` /
    `email_replied` / `email_bounced` / `contact_unsubscribed`.
18. Reply classifier (Haiku) + `gtm_funnel_events` writes.
19. Funnel report UI (email-only cuts).

### Phase 3 — auto-approve + notifications + suppressions
20. `auto_approve` mode (validator-pass → auto-ship without human).
21. Notification dispatcher: positive → mock Slack DM (idempotent via
    `(lead_id, sf_message_id)`).
22. SF DNC mirror into `gtm_suppressions`.
23. `mailbox_paused` / `mailbox_resumed` webhook handling.

### Phase 4 — LinkedIn channel
24. `campaign.channels` toggle + `linkedin_prompt_revision`.
25. LinkedIn copywriter prompt + LinkedIn validator checks (note ≤200
    chars, no links in connection request, voice consistency).
26. Per-lead dual-draft generation (email + LinkedIn when eligible).
27. Side-by-side review UI with per-channel approve/reject + auto-
    rejection of the other channel.
28. SF multichannel sequence assign + LinkedIn webhook handlers.
29. LinkedIn capacity counter on review queue header.
30. Channel-dimension on funnel report cuts.

### Phase 5 — operations
31. Per-campaign freeze (terminal SF DNC bulk-add).
32. Pause / cancel transitions.
33. Validator override audit.
34. Archive.

Within each phase, write the failing test first (per repo CLAUDE.md
methodology), then implement.

---

## 9. Open items (architecture doc punts these to first integration)

These are flagged in the architecture doc as "confirm during first
integration with the real vendor" — they don't block the build because
the mock can pick a shape and the real vendor's shape gets reconciled
later:

- Clay's actual `clay_row_id` field name + stability.
- Canonical company key (domain vs name vs LinkedIn vs Clay company id).
- SF webhook payload — do `customVars` and `tags` round-trip? If not,
  fall back to `gtm_sequence_pushes` lookup (already implemented).
- SF webhook signing (or path token fallback).
- SF contact-uniqueness behavior (existing-id return vs 4xx).
- SF reply-body inline vs always-fetch.
- SF rate limits.
- SF mailbox-pause/resume exact event names.
- SF `linkedin_replied` payload shape — connection-message reply vs
  subsequent-message reply distinction; thread continuity model.
- SF billing-month anchor for LinkedIn capacity (calendar vs
  subscription anniversary).
- LinkedIn account import operator UX + required SF plan tier.

Build assuming the architecture-doc defaults; flag any test that
hard-codes these so it's easy to swap when the real shape is known.

---

## 10. References inside the architecture doc

The companion `clay-gtm-architecture.md` has more depth than is
reproduced here. When in doubt, the architecture doc is the source of
truth. Key sections to revisit:

- §"Clay integration mechanics" — webhook source + HTTP API column
  primitives, run-completion 3-signal detector, callback auth.
- §"Clay callback — capturing enriched leads" — storage shape,
  provenance fields, idempotency natural keys.
- §"Salesforge integration" — outbound flow, inbound flow, suppression,
  LinkedIn channel mechanics.
- §"What we build — campaign management" — state machine, UI surfaces,
  validator agent (this is the most operationally important section).
- §"What we build — exit: feedback loop + data model" — funnel SoR
  schema, conversion-stage ladders, experiment cuts.

---

## 11. Hand-off checklist for the next agent

Before starting code:

- [ ] Read `~/Documents/clay-gtm-architecture.md` end-to-end at least once.
- [ ] Re-read §"What we build — campaign management" — the state
      machine, validator, and dual-draft review UI are the most
      operationally subtle parts.
- [ ] Decide tech stack (the architecture doc is language-agnostic; if
      reusing the auto-agent monorepo, follow its CLAUDE.md — Python +
      FastAPI + SQLAlchemy async + Next.js front-end. If standalone,
      pick a stack and document it as an ADR.)
- [ ] UI styling: light theme, soft pastel-ish colours, soft borders,
      generous whitespace, rounded corners. No dark mode in v1.
- [ ] Hosting target: **AWS ECS** (containerised services). Plan the
      service as one or more long-running containers (Dockerfile per
      service), with the database on RDS (Postgres), background jobs
      either as separate ECS services or as scheduled ECS tasks
      (quiescence detector, capacity counter reset, etc.). Secrets via
      AWS Secrets Manager / SSM Parameter Store. ALB in front of the
      HTTP API + UI. See §12.
- [ ] Stand up the database schema and the two mock vendor servers
      before any feature code.
- [ ] Follow the TDD methodology in the project's CLAUDE.md: failing
      test first, then implementation.
- [ ] Treat each phase in §8 as a milestone with its own smoke test.
- [ ] When the real Clay or Salesforge integration comes online, swap
      the mock vendor servers for real clients behind the same
      interface — the rest of the service should not need to change.

---

## 12. Deployment — AWS ECS

Target environment is **AWS ECS** (Fargate or EC2-backed; Fargate is
the simpler default). Plan the service as containerised workloads from
day one — local `docker compose` for dev, ECS task definitions for
staging/prod.

**Component → AWS service mapping:**

| Component | AWS service | Notes |
|---|---|---|
| HTTP API (FastAPI or equivalent) | ECS service behind ALB | public-facing for `/webhooks/clay`, `/webhooks/salesforge`; auth-gated for operator endpoints |
| Operator UI (Next.js or equivalent) | ECS service behind ALB (or S3 + CloudFront if static export) | same ALB, separate target group |
| Background jobs (quiescence detector, capacity counter, dedup index maintenance) | ECS scheduled tasks (EventBridge cron) OR a long-running worker ECS service consuming a queue | pick per job: scheduled for cron-style, worker for queue-driven |
| Database | RDS Postgres | private subnet; the `gtm_*` schema; enable automated backups + PITR |
| Cache / queue (if needed) | ElastiCache Redis OR SQS | queue for webhook fan-out / Slack notifications; Redis if we add read-side caching |
| Secrets (Clay callback pepper, SF API key, LLM API key, Slack token, DB creds) | AWS Secrets Manager (or SSM Parameter Store for non-rotating values) | injected into ECS tasks via task-definition `secrets` block — never baked into images |
| Logs | CloudWatch Logs | structured JSON logs from every container |
| Metrics | CloudWatch Metrics + (optional) Grafana | funnel-stage counts, validator pass rate, webhook latency |
| Mock vendor servers (dev/staging only) | ECS services in the staging account | so we can smoke-test against synthetic Clay/SF traffic end-to-end before swapping to real vendors |
| Image registry | ECR | one repo per service; tag by git SHA |
| CI/CD | GitHub Actions → ECR push → ECS deploy | standard ECS rolling deployment; consider blue/green if zero-downtime becomes critical |

**Networking:**
- ALB in public subnets; ECS services in private subnets; RDS in a
  separate private subnet with security-group ingress only from the
  service security groups.
- Outbound to Clay, Salesforge, Slack, LLM provider via NAT Gateway.
- Inbound webhooks (`/webhooks/clay`, `/webhooks/salesforge`) hit the
  ALB → API service. The path-token or bearer-token check in the
  handler is the auth — no IP allowlisting (Clay and Salesforge don't
  publish stable IP ranges).

**Notes for the build phase:**
- Write the service as if hosting were portable (12-factor:
  configuration from env, no local filesystem state, stateless app
  containers). ECS specifics live in the task definitions and the IaC
  layer, not in app code.
- The `docker compose` setup for local dev should mirror the prod
  topology: one container per service, mocks for vendors, Postgres in
  its own container. That way the dev-to-ECS path is just a registry
  push + task-definition update.
- Database migrations run as an ECS one-shot task before each deploy
  (Alembic or equivalent — same pattern as auto-agent).
