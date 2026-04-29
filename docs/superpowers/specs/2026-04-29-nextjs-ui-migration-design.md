# Next.js UI Migration — Design

**Status:** Approved
**Author:** Brainstormed with Claude, 2026-04-29
**Sequenced:** Spec 1 of 2. Spec 2 (marketing-agent backend + Marketing tab) builds on this.

## Goal

Replace the single 2720-line `web/static/index.html` SPA with a Next.js 14 (App Router) application served from its own container. 1:1 feature parity with the current UI (Tasks, Freeform, Memory tabs), plus a refreshed warm-amber dark theme, typed end-to-end data flow, and a security upgrade from localStorage tokens to httpOnly cookie auth.

This migration is intentionally **a refactor with a theme refresh** — no new product features. The marketing agent and the Marketing tab live in spec 2 and become trivially additive once the Next.js shell exists.

## Non-goals

- Adding any new agent capability or backend feature.
- SSR / RSC for performance; this is an internal tool, all data is auth-gated and live.
- A light-mode toggle. Not in scope this round; `next-themes` can be added later.
- Re-architecting `orchestrator/`, `agent/`, or `claude_runner/`.
- Migrating database access into the Next.js service. FastAPI remains the only DB writer.

## Acceptance criteria

1. `docker compose up` starts both `auto-agent` (FastAPI) and `web-next` (Next.js) services. The full UI is reachable at `http://localhost:3000`.
2. All three current tabs (Tasks, Freeform, Memory) work end-to-end with feature parity:
   - Login → token cookie set → redirect to `/tasks`.
   - Task list, create task, chat messages, approve/reject, clarification round-trip, mark done, cancel, delete, priority controls, subtask bar.
   - Freeform: build-new, add-to-existing, repo list.
   - Memory: drop zone upload, review table, conflict resolver.
3. Authentication is via httpOnly `auto_agent_session` cookie. No JWT in `localStorage`. WebSocket auth uses the cookie sent on upgrade.
4. The old `web/static/index.html`, the `/` HTMLResponse handler, and the `web/static/` mount are deleted at cutover. Production deploy serves both apps behind one origin via a reverse proxy (Caddy in our VM setup).
5. Vitest + Playwright suites cover the WS client, auth, and golden-path E2E flows. Both run in CI.
6. `ruff check .` and `.venv/bin/python3 -m pytest tests/ -q` pass after the FastAPI changes.

## Architecture

### Service topology

```
                        ┌────────────────────────┐
                        │     Browser (one        │
                        │      origin)            │
                        └─────────┬──────────────┘
                                  │
                          Caddy (prod) / Next dev proxy (dev)
                                  │
              ┌───────────────────┴──────────────────┐
              │                                      │
              ▼                                      ▼
    ┌──────────────────┐                   ┌────────────────────┐
    │  web-next:3000   │                   │  auto-agent:8000   │
    │  Next.js 14 (TS) │                   │  FastAPI           │
    │  App Router      │                   │  /api/* + /ws      │
    │  Tailwind + shadcn│                  │  SQLAlchemy        │
    └──────────────────┘                   └────────────────────┘
                                                     │
                                                     ▼
                                              Postgres / Redis
```

- `web-next` is a separate container in `docker-compose.yml`, image built from `web-next/Dockerfile` (multi-stage: deps → build → runtime).
- In dev, `next.config.js` rewrites `/api/*` and `/ws` to `auto-agent:8000` so the browser only sees one origin and cookies are first-party.
- In prod (single VM), Caddy listens on 80/443 and reverse-proxies `/` to `web-next:3000`, `/api/*` and `/ws` to `auto-agent:8000`. A Caddyfile snippet is provided in the spec; deploy infra changes are documented but the actual deploy is run by the operator.
- The FastAPI process keeps owning the database, Redis, event bus, classifier, queue, and all agent runtime concerns. It also keeps owning auth (issues and validates the cookie). **No DB access from `web-next`.**

### Backend changes (FastAPI)

Three categories of change:

1. **Auth endpoints (new):**
   - `POST /api/auth/login` — body `{username, password}`. Validates credentials; sets httpOnly, signed `auto_agent_session` cookie (`SameSite=Lax`; `Secure` when behind TLS). Returns `{user: {id, username}}`.
   - `GET /api/auth/me` — returns `{user}` if cookie valid, 401 otherwise.
   - `POST /api/auth/logout` — clears the cookie.
2. **WebSocket auth migration:**
   - `/ws` reads the cookie via `ws.cookies.get('auto_agent_session')`.
   - During phase 2/3, `?token=` query-param auth remains as a fallback. Phase 4 removes the fallback.
3. **REST path normalization:**
   - `/memory/upload` → `/api/memory/upload`. Other `/memory/*` REST paths follow.
   - Phase 2 ships both the new and old paths. Phase 4 removes the old paths.
4. **Static-serving removal (phase 4):**
   - Delete the `/` HTMLResponse handler in `web/main.py`.
   - Remove `app.mount("/static", ...)`.
   - Delete `web/static/index.html`.

The existing JWT helpers (issuance, validation, secret handling) are reused; only the transport changes.

### `web-next/` layout

```
web-next/
├─ app/
│  ├─ layout.tsx              # Root: theme provider, fonts, toaster
│  ├─ globals.css             # CSS variables (warm-amber dark palette)
│  ├─ (auth)/
│  │  └─ login/page.tsx       # Posts to /api/auth/login
│  └─ (app)/
│     ├─ layout.tsx           # Sidebar + auth guard (server check via /api/auth/me)
│     ├─ tasks/page.tsx
│     ├─ freeform/page.tsx
│     └─ memory/page.tsx
├─ components/
│  ├─ ui/                     # shadcn primitives
│  ├─ sidebar/                # Sidebar, route links, task list panel, user info
│  ├─ chat/                   # ChatArea, ApprovalBar, ClarificationBar, DoneBar
│  ├─ tasks/                  # TaskListItem, NewTaskForm, SubtaskBar, TaskActions
│  ├─ freeform/               # BuildView, ExistingView, RepoList
│  └─ memory/                 # DropZone, ReviewTable, ConflictResolver
├─ lib/
│  ├─ ws.ts                   # WS singleton: connect, reconnect, typed send/receive
│  ├─ api.ts                  # fetch wrapper, throws on non-2xx, includes credentials
│  ├─ auth.ts                 # Helpers around /api/auth/*
│  └─ queryClient.ts          # TanStack Query client config
├─ hooks/
│  ├─ useWS.ts                # useWS<T>(eventType, handler)
│  ├─ useTasks.ts             # Task list state, sourced from WS + REST
│  └─ useAuth.ts              # Current user, login, logout
├─ types/
│  ├─ api.ts                  # Generated/maintained TS mirrors of shared/types.py
│  └─ ws.ts                   # WS event discriminated union
├─ tests/                     # Vitest unit tests
├─ e2e/                       # Playwright tests
├─ tailwind.config.ts
├─ next.config.js             # /api and /ws rewrites in dev
├─ Dockerfile
├─ package.json
└─ tsconfig.json
```

### State management

- **TanStack Query** for REST-backed reads/writes. The query client is configured with a 30-second stale time on cached lists; mutations invalidate the relevant keys.
- **WebSocket client** is a typed singleton (`lib/ws.ts`). It maintains a single connection, reconnects with exponential backoff (1s → 30s cap), and on (re)connect requests a fresh task list.
- Server-pushed updates land in the TanStack cache via `queryClient.setQueryData(['tasks'], ...)`. Components read with `useQuery(['tasks'])` and re-render automatically.
- No Redux/Zustand. Component-local state covers the rest (form inputs, transient UI).

### Auth flow

```
Browser                 Next.js (web-next)        FastAPI (auto-agent)
   │   GET /tasks            │                         │
   ├──────────────────────────►                         │
   │                          │ getServerSession()     │
   │                          │   GET /api/auth/me     │
   │                          ├────────────────────────►
   │                          │   401                  │
   │                          │◄────────────────────────
   │   302 → /login           │                         │
   │◄──────────────────────────                         │
   │   POST /api/auth/login   │                         │
   ├─ rewrite ───────────────►│ proxy ─────────────────►│
   │                          │                         │ Set-Cookie: auto_agent_session
   │   200 + cookie           │                         │
   │◄──────────────────────────────────────────────────│
   │   GET /tasks (with cookie)                         │
   │   ws://.../ws (cookie sent on upgrade)             │
```

### WebSocket protocol

The wire protocol is unchanged. We codify the shape on the client side as a TypeScript discriminated union in `web-next/types/ws.ts`. Examples:

```ts
export type WSEvent =
  | { type: 'task_list'; tasks: Task[] }
  | { type: 'task_update'; task: Task }
  | { type: 'message'; task_id: number; message: ChatMessage }
  | { type: 'clarification_needed'; task_id: number; question: string }
  | { type: 'error'; message: string }
  | // …all current event types
```

`ws.send.createTask({...})` and similar typed helpers replace the current ad-hoc JSON sends.

## Theme

Warm-charcoal background with an amber/orange accent. Concrete shadcn-compatible HSL tokens:

| Token                  | HSL              | Hex       | Use                       |
|------------------------|------------------|-----------|---------------------------|
| `--background`         | `25 8% 8%`       | `#16120F` | Page background           |
| `--card`               | `25 8% 11%`      | `#1E1815` | Panels                    |
| `--popover`            | `25 8% 11%`      | `#1E1815` | Dropdowns/dialogs         |
| `--foreground`         | `30 12% 92%`     | `#EDE7E0` | Body text                 |
| `--muted-foreground`   | `30 8% 65%`      | `#A89F94` | Secondary text            |
| `--border`             | `25 8% 18%`      | `#2E2724` | Dividers, separators      |
| `--primary`            | `25 90% 58%`     | `#F08A2E` | Buttons, links            |
| `--accent`             | `15 75% 55%`     | `#DC6B3D` | Hover, highlights         |
| `--destructive`        | `0 70% 55%`      | `#D94A3D` | Delete/reject actions     |
| `--success`            | `140 50% 50%`    | `#3FBF6E` | Status dots, success toasts |

Typography: Inter (UI), JetBrains Mono (code blocks, IDs). Loaded via `next/font/google` for self-hosting.

## Migration plan (4 phases / ~6 PRs)

### Phase 1 — Scaffold (no behavior change)

- Add `web-next/` with Next.js 14 (App Router, TS, Tailwind, shadcn baseline).
- Apply the warm-amber palette in `globals.css` and `tailwind.config.ts`.
- Add `web-next/Dockerfile` and `web-next` service to `docker-compose.yml`.
- Document Caddyfile snippet for prod (in `docs/`); not deployed yet.
- Placeholder page at `:3000`. Existing UI at `:8000/` still serves.

### Phase 2 — Backend prep

- Add `POST /api/auth/login`, `GET /api/auth/me`, `POST /api/auth/logout` to FastAPI. Cookie-based.
- `/ws` accepts cookie auth; query-param fallback retained.
- Add `/api/memory/*` paths alongside existing `/memory/*`.
- Generate `web-next/types/api.ts` from `shared/types.py` using `pydantic-to-typescript` (committed; regen documented).
- pytest coverage for new auth endpoints + cookie-based WS auth.

### Phase 3 — Port tabs (three sub-PRs)

- **3a**: Login page, auth guard layout, sidebar shell, theme. WS client + `useWS` + `lib/api.ts` complete.
- **3b**: `/tasks` route — task list panel, chat area, approval / clarification / done bars, new-task form, subtask bar, priority controls, cancel/delete actions. `useTasks` hook complete.
- **3c**: `/freeform` and `/memory` routes — build-new, add-to-existing, repo list, drop zone, review table, conflict resolver.

After each sub-PR, the ported screen works in `web-next` while the old UI remains at `:8000/` for comparison.

### Phase 4 — Cutover

- Caddyfile committed under `deploy/` (or wherever ops configs live; check during impl).
- Delete `web/static/index.html`, the `/` HTMLResponse handler in `web/main.py`, and the `app.mount("/static", ...)` line.
- Remove the `?token=` WS auth fallback.
- Remove legacy `/memory/*` paths.
- Update `CLAUDE.md` (path map, build commands).

## Testing

- **Vitest + React Testing Library** in `web-next/tests/`:
  - WS client: connect, reconnect with backoff, message dispatch, type narrowing.
  - `useTasks` hook: WS update merges into TanStack cache.
  - Auth guard: redirects unauthenticated users.
  - Chat reducer / approval flow logic.
  - Smoke tests on key components (login, sidebar, task list item).
- **Playwright** in `web-next/e2e/`:
  - Login → tasks tab loads.
  - Create task → appears in list.
  - Send message → echoed in chat.
  - Approve / reject flow round-trip.
  - Clarification round-trip.
  - Memory upload → review table populates.
  - Runs against `docker compose up` of both services.
- **FastAPI unit tests** in `tests/test_auth.py` and `tests/test_ws_auth.py`.
- **Manual side-by-side** before phase 4 cutover lands.

CI gains two jobs:
- `web-next-test`: `npm ci && npm run lint && npm run typecheck && npm run test`.
- `e2e`: `docker compose up -d && npx playwright test`.

## Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Cookie auth breaks existing native API consumers (e.g. Telegram webhook callbacks calling internal endpoints). | Audit during phase 2 — internal callers currently don't go through `/`; only the `/ws` and memory upload paths are user-facing. Server-to-server calls keep using bearer-token auth on dedicated routes. |
| TS types drift from Pydantic models. | Commit a regeneration script (`scripts/gen-ts-types.sh`) and document it in `CLAUDE.md`. Optional CI check in a follow-up. |
| Two services in dev raises onboarding friction. | One-command `docker compose up` brings both up. README updated. |
| Reverse proxy misconfig in prod. | Caddyfile is short and tested locally before cutover. Cutover PR includes step-by-step prod runbook. |
| WS reconnect storms during long sessions. | Exponential backoff with jitter in `lib/ws.ts`; dedupe simultaneous reconnect attempts. |

## Out of scope (deferred to future specs)

- Marketing agent backend and Marketing tab (spec 2).
- Light mode / theme switcher.
- Mobile-responsive layout tuning.
- Replacing `pydantic-to-typescript` with a richer codegen (e.g. orval, openapi-typescript) — only worth it once the REST surface is wider.
- Auth provider integrations (SSO, OAuth) — out of scope.

## File-level change list

**New files:**
- `web-next/**` (entire app — scaffold in phase 1, populated through phases 3a–3c)
- `tests/test_auth.py`, `tests/test_ws_auth.py`
- `scripts/gen-ts-types.sh`
- `docs/deploy/Caddyfile.example`

**Modified files:**
- `docker-compose.yml` — add `web-next` service.
- `web/main.py` — add auth endpoints, switch `/ws` cookie auth, namespace memory routes; phase 4 removes static serving.
- `shared/types.py` — only if missing fields surface during TS generation.
- `CLAUDE.md` — update path map and build commands.

**Deleted files (phase 4):**
- `web/static/index.html`
- `web/static/` directory.
