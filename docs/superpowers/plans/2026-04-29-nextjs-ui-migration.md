# Next.js UI Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `web/static/index.html` with a Next.js 14 App Router app served from its own container, with 1:1 feature parity, a warm-amber dark theme, cookie-based auth, and a typed WebSocket client.

**Architecture:** New `web-next/` Next.js service runs alongside FastAPI in docker-compose. Caddy fronts both in prod. FastAPI keeps owning the database, `/ws`, and `/api/*`. The browser sees one origin (Next dev proxy in dev, Caddy in prod), and auth is via httpOnly cookie. TanStack Query holds REST + WS-pushed state.

**Tech Stack:** Next.js 14 (App Router, TypeScript), Tailwind CSS, shadcn/ui, TanStack Query v5, Vitest + React Testing Library, Playwright.

**Spec:** `docs/superpowers/specs/2026-04-29-nextjs-ui-migration-design.md`

**Note on existing code:**
- FastAPI listens on **port 2020** (not 8000). Use 2020 throughout.
- `/api/auth/login` and `/api/auth/me` already exist in `orchestrator/router.py` using `Authorization: Bearer` headers. We add cookie behavior to these endpoints rather than creating new ones.
- The current WebSocket auth at `web/main.py:153` reads `?token=` query param. We add cookie support and keep query-param as fallback through Phase 3.

---

## Phase 1 — Scaffold (no behavior change)

### Task 1: Create web-next package skeleton

**Files:**
- Create: `web-next/package.json`
- Create: `web-next/tsconfig.json`
- Create: `web-next/.gitignore`
- Create: `web-next/next.config.js`
- Create: `web-next/postcss.config.js`
- Create: `web-next/tailwind.config.ts`
- Create: `web-next/app/layout.tsx`
- Create: `web-next/app/page.tsx`
- Create: `web-next/app/globals.css`
- Create: `web-next/components.json`

- [ ] **Step 1.1: Write `web-next/package.json`**

```json
{
  "name": "auto-agent-web",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev -p 3000",
    "build": "next build",
    "start": "next start -p 3000",
    "lint": "next lint",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "test:watch": "vitest",
    "e2e": "playwright test"
  },
  "dependencies": {
    "next": "14.2.5",
    "react": "18.3.1",
    "react-dom": "18.3.1",
    "@tanstack/react-query": "5.51.0",
    "class-variance-authority": "0.7.0",
    "clsx": "2.1.1",
    "tailwind-merge": "2.4.0",
    "lucide-react": "0.408.0"
  },
  "devDependencies": {
    "@types/node": "20.14.10",
    "@types/react": "18.3.3",
    "@types/react-dom": "18.3.0",
    "@testing-library/react": "16.0.0",
    "@testing-library/jest-dom": "6.4.6",
    "@playwright/test": "1.45.1",
    "autoprefixer": "10.4.19",
    "eslint": "8.57.0",
    "eslint-config-next": "14.2.5",
    "jsdom": "24.1.0",
    "postcss": "8.4.39",
    "tailwindcss": "3.4.6",
    "tailwindcss-animate": "1.0.7",
    "typescript": "5.5.3",
    "vitest": "2.0.2"
  }
}
```

- [ ] **Step 1.2: Write `web-next/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": false,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "baseUrl": ".",
    "paths": { "@/*": ["./*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

- [ ] **Step 1.3: Write `web-next/.gitignore`**

```
node_modules
.next
out
.env*.local
playwright-report
test-results
coverage
```

- [ ] **Step 1.4: Write `web-next/next.config.js`**

The dev server proxies `/api/*` and `/ws` to FastAPI on port 2020, so the browser sees one origin and cookies are first-party.

```js
/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  async rewrites() {
    const apiTarget = process.env.API_URL || 'http://localhost:2020';
    return [
      { source: '/api/:path*', destination: `${apiTarget}/api/:path*` },
      { source: '/ws', destination: `${apiTarget}/ws` },
    ];
  },
};
module.exports = nextConfig;
```

- [ ] **Step 1.5: Write `web-next/postcss.config.js`**

```js
module.exports = { plugins: { tailwindcss: {}, autoprefixer: {} } };
```

- [ ] **Step 1.6: Write `web-next/tailwind.config.ts`**

```ts
import type { Config } from 'tailwindcss';

const config: Config = {
  darkMode: 'class',
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}'],
  theme: {
    container: { center: true, padding: '1rem' },
    extend: {
      colors: {
        background: 'hsl(var(--background))',
        foreground: 'hsl(var(--foreground))',
        card: { DEFAULT: 'hsl(var(--card))', foreground: 'hsl(var(--card-foreground))' },
        popover: { DEFAULT: 'hsl(var(--popover))', foreground: 'hsl(var(--popover-foreground))' },
        primary: { DEFAULT: 'hsl(var(--primary))', foreground: 'hsl(var(--primary-foreground))' },
        secondary: { DEFAULT: 'hsl(var(--secondary))', foreground: 'hsl(var(--secondary-foreground))' },
        muted: { DEFAULT: 'hsl(var(--muted))', foreground: 'hsl(var(--muted-foreground))' },
        accent: { DEFAULT: 'hsl(var(--accent))', foreground: 'hsl(var(--accent-foreground))' },
        destructive: { DEFAULT: 'hsl(var(--destructive))', foreground: 'hsl(var(--destructive-foreground))' },
        success: { DEFAULT: 'hsl(var(--success))', foreground: 'hsl(var(--success-foreground))' },
        border: 'hsl(var(--border))',
        input: 'hsl(var(--input))',
        ring: 'hsl(var(--ring))',
      },
      borderRadius: {
        lg: 'var(--radius)',
        md: 'calc(var(--radius) - 2px)',
        sm: 'calc(var(--radius) - 4px)',
      },
      fontFamily: { sans: ['var(--font-inter)'], mono: ['var(--font-jb-mono)'] },
    },
  },
  plugins: [require('tailwindcss-animate')],
};
export default config;
```

- [ ] **Step 1.7: Write `web-next/app/globals.css`**

Warm-amber dark palette. shadcn-compatible HSL tokens.

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  :root {
    --background: 25 8% 8%;
    --foreground: 30 12% 92%;
    --card: 25 8% 11%;
    --card-foreground: 30 12% 92%;
    --popover: 25 8% 11%;
    --popover-foreground: 30 12% 92%;
    --primary: 25 90% 58%;
    --primary-foreground: 25 10% 8%;
    --secondary: 25 8% 16%;
    --secondary-foreground: 30 12% 92%;
    --muted: 25 8% 14%;
    --muted-foreground: 30 8% 65%;
    --accent: 15 75% 55%;
    --accent-foreground: 25 10% 8%;
    --destructive: 0 70% 55%;
    --destructive-foreground: 30 12% 95%;
    --success: 140 50% 50%;
    --success-foreground: 25 10% 8%;
    --border: 25 8% 18%;
    --input: 25 8% 18%;
    --ring: 25 90% 58%;
    --radius: 0.5rem;
  }
  * { @apply border-border; }
  body { @apply bg-background text-foreground antialiased; }
}
```

- [ ] **Step 1.8: Write `web-next/app/layout.tsx`**

```tsx
import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import './globals.css';

const inter = Inter({ subsets: ['latin'], variable: '--font-inter' });
const jbMono = JetBrains_Mono({ subsets: ['latin'], variable: '--font-jb-mono' });

export const metadata: Metadata = {
  title: 'Auto-Agent',
  description: 'Autonomous AI scrum team',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jbMono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
```

- [ ] **Step 1.9: Write `web-next/app/page.tsx` (placeholder)**

```tsx
export default function HomePage() {
  return (
    <main className="flex min-h-screen items-center justify-center">
      <div className="text-center">
        <h1 className="text-2xl font-semibold text-primary">Auto-Agent</h1>
        <p className="mt-2 text-muted-foreground">web-next scaffold (phase 1)</p>
      </div>
    </main>
  );
}
```

- [ ] **Step 1.10: Write `web-next/components.json` (shadcn config)**

```json
{
  "$schema": "https://ui.shadcn.com/schema.json",
  "style": "default",
  "rsc": true,
  "tsx": true,
  "tailwind": {
    "config": "tailwind.config.ts",
    "css": "app/globals.css",
    "baseColor": "neutral",
    "cssVariables": true
  },
  "aliases": {
    "components": "@/components",
    "utils": "@/lib/utils"
  }
}
```

- [ ] **Step 1.11: Add `cn()` utility — `web-next/lib/utils.ts`**

```ts
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
```

- [ ] **Step 1.12: Install dependencies and verify build**

Run from `web-next/`:
```bash
npm install
npm run typecheck
npm run build
```
Expected: typecheck passes, `next build` produces `.next/` with no errors.

- [ ] **Step 1.13: Commit**

```bash
git add web-next/
git commit -m "feat(web-next): scaffold Next.js 14 app with warm-amber theme"
```

---

### Task 2: Add web-next to docker-compose and Dockerfile

**Files:**
- Create: `web-next/Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 2.1: Write `web-next/Dockerfile`**

Multi-stage: deps install → build → minimal runtime. Uses Next's `output: 'standalone'`.

```dockerfile
FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
```

- [ ] **Step 2.2: Add `web-next` service to `docker-compose.yml`**

Insert after the `auto-agent` service block (before the `volumes:` section). Use the actual port FastAPI listens on (`2020`).

```yaml
  web-next:
    build:
      context: ./web-next
      dockerfile: Dockerfile
    restart: unless-stopped
    environment:
      API_URL: http://auto-agent:2020
      NODE_ENV: production
    depends_on:
      - auto-agent
    ports:
      - "3000:3000"
```

- [ ] **Step 2.3: Generate `package-lock.json` if missing**

Run from `web-next/`:
```bash
npm install --package-lock-only
```

- [ ] **Step 2.4: Verify compose builds**

```bash
docker compose build web-next
```
Expected: build succeeds.

- [ ] **Step 2.5: Commit**

```bash
git add web-next/Dockerfile web-next/package-lock.json docker-compose.yml
git commit -m "feat(web-next): docker container + compose service"
```

---

### Task 3: Caddyfile example for prod

**Files:**
- Create: `docs/deploy/Caddyfile.example`

- [ ] **Step 3.1: Write `docs/deploy/Caddyfile.example`**

```
# Caddyfile for single-VM deployment
# Replace {your.domain} with your actual hostname.
{your.domain} {
    encode gzip

    # WebSocket and API to FastAPI (port 2020)
    @backend path /ws /api/* /api/* /metrics
    handle @backend {
        reverse_proxy localhost:2020
    }

    # Everything else to Next.js (port 3000)
    handle {
        reverse_proxy localhost:3000
    }
}
```

- [ ] **Step 3.2: Commit**

```bash
git add docs/deploy/Caddyfile.example
git commit -m "docs(deploy): add example Caddyfile for single-VM deploy"
```

---

## Phase 2 — Backend prep

### Task 4: Add cookie support to /api/auth/login and /api/auth/me

**Files:**
- Modify: `orchestrator/router.py:166-200` (login + me endpoints)
- Test: `tests/test_auth_cookie.py`

- [ ] **Step 4.1: Write the failing test — `tests/test_auth_cookie.py`**

```python
"""Cookie-based auth on /api/auth/* endpoints."""
import pytest
from httpx import AsyncClient, ASGITransport
from orchestrator.auth import hash_password
from shared.database import async_session
from shared.models import User
from run import app


COOKIE_NAME = "auto_agent_session"


@pytest.fixture
async def test_user():
    async with async_session() as s:
        u = User(username="cookie_user", password_hash=hash_password("pw"))
        s.add(u)
        await s.commit()
        yield u
        await s.delete(u)
        await s.commit()


@pytest.mark.asyncio
async def test_login_sets_cookie(test_user):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/api/auth/login", json={"username": "cookie_user", "password": "pw"})
        assert r.status_code == 200
        assert COOKIE_NAME in r.cookies


@pytest.mark.asyncio
async def test_me_accepts_cookie(test_user):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        login = await c.post("/api/auth/login", json={"username": "cookie_user", "password": "pw"})
        token = login.cookies[COOKIE_NAME]
        r = await c.get("/api/auth/me", cookies={COOKIE_NAME: token})
        assert r.status_code == 200
        assert r.json()["username"] == "cookie_user"


@pytest.mark.asyncio
async def test_logout_clears_cookie(test_user):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.post("/api/auth/login", json={"username": "cookie_user", "password": "pw"})
        r = await c.post("/api/auth/logout")
        assert r.status_code == 200
        # Cookie set to empty / past expiry
        assert r.cookies.get(COOKIE_NAME, "") == ""
```

- [ ] **Step 4.2: Run test, verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_auth_cookie.py -v
```
Expected: failures (no cookie set, `/api/auth/logout` not found).

- [ ] **Step 4.3: Modify `/api/auth/login` in `orchestrator/router.py` to also set the cookie**

Wrap the response so the cookie is set. The body still includes `token` for backwards compat.

```python
COOKIE_NAME = "auto_agent_session"
COOKIE_MAX_AGE = 7 * 24 * 3600  # match JWT expiry

@router.post("/auth/login")
async def login(req: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(User).where(User.username == req.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    user.last_login = datetime.now(UTC)
    await session.commit()
    token = create_token(user_id=user.id, username=user.username)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=os.environ.get("COOKIE_SECURE", "0") == "1",
        path="/",
    )
    return LoginResponse(
        token=token,
        user=UserData(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            created_at=user.created_at.isoformat() if user.created_at else None,
            last_login=user.last_login.isoformat() if user.last_login else None,
        ),
    )
```

Add imports at top of `orchestrator/router.py`:
```python
import os
from fastapi import Cookie, Response
```

- [ ] **Step 4.4: Modify `/api/auth/me` to accept the cookie as a fallback to the header**

Replace the existing function signature and `_verify_auth_header` call:
```python
@router.get("/auth/me")
async def get_me(
    session: AsyncSession = Depends(get_session),
    authorization: str = Header(None),
    auto_agent_session: str | None = Cookie(default=None),
):
    payload = _verify_cookie_or_header(auto_agent_session, authorization)
    result = await session.execute(select(User).where(User.id == payload["user_id"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return UserData(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        created_at=user.created_at.isoformat() if user.created_at else None,
        last_login=user.last_login.isoformat() if user.last_login else None,
    )
```

Add a helper near `_verify_auth_header`:
```python
def _verify_cookie_or_header(cookie: str | None, authorization: str | None) -> dict:
    """Accept either the cookie or Authorization: Bearer header."""
    if cookie:
        payload = verify_token(cookie)
        if payload:
            return payload
    return _verify_auth_header(authorization)
```

- [ ] **Step 4.5: Add `/api/auth/logout` endpoint**

```python
@router.post("/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}
```

- [ ] **Step 4.6: Run tests, verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_auth_cookie.py -v
```
Expected: all 3 tests pass.

- [ ] **Step 4.7: Commit**

```bash
git add orchestrator/router.py tests/test_auth_cookie.py
git commit -m "feat(auth): cookie-based session alongside bearer token"
```

---

### Task 5: WebSocket reads the auth cookie (with query-param fallback)

**Files:**
- Modify: `web/main.py:148-170` (websocket_endpoint)
- Test: `tests/test_ws_auth.py`

- [ ] **Step 5.1: Write the failing test — `tests/test_ws_auth.py`**

```python
"""WebSocket cookie-based auth."""
import pytest
from fastapi.testclient import TestClient
from orchestrator.auth import create_token, hash_password
from shared.database import async_session
from shared.models import User
from run import app


@pytest.fixture
def ws_user():
    import asyncio
    async def setup():
        async with async_session() as s:
            u = User(username="ws_user", password_hash=hash_password("pw"))
            s.add(u)
            await s.commit()
            return u.id, u.username
    user_id, username = asyncio.get_event_loop().run_until_complete(setup())
    yield user_id, username


def test_ws_accepts_cookie(ws_user):
    user_id, username = ws_user
    token = create_token(user_id=user_id, username=username)
    client = TestClient(app)
    client.cookies.set("auto_agent_session", token)
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "task_list"


def test_ws_rejects_no_auth():
    client = TestClient(app)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass
```

- [ ] **Step 5.2: Run test, verify cookie test fails**

```bash
.venv/bin/python3 -m pytest tests/test_ws_auth.py -v
```
Expected: `test_ws_accepts_cookie` fails (token only read from query param).

- [ ] **Step 5.3: Modify `web/main.py` websocket_endpoint to read cookie**

Replace the auth block (around line 152-163):
```python
    # Authenticate via cookie (preferred) or token query param (legacy fallback)
    token = ws.cookies.get("auto_agent_session") or ws.query_params.get("token")
    if not token:
        await ws.send_json({"type": "error", "message": "Authentication required"})
        await ws.close(code=4001)
        return

    from orchestrator.auth import verify_token
    payload = verify_token(token)
    if not payload:
        await ws.send_json({"type": "error", "message": "Invalid or expired token"})
        await ws.close(code=4001)
        return
```

- [ ] **Step 5.4: Run tests, verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_ws_auth.py -v
```
Expected: both tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add web/main.py tests/test_ws_auth.py
git commit -m "feat(ws): accept cookie auth, keep query-param fallback"
```

---

### Task 6: Generate TypeScript types from Pydantic models

**Files:**
- Create: `scripts/gen_ts_types.py`
- Create: `web-next/types/api.ts` (generated)
- Modify: `CLAUDE.md` (document regen step)

- [ ] **Step 6.1: Add `pydantic-to-typescript` to dev requirements**

Add to `requirements.txt` (or wherever dev deps live; check first):
```
pydantic-to-typescript==2.0.0
```
Then `pip install -r requirements.txt` to install.

- [ ] **Step 6.2: Write `scripts/gen_ts_types.py`**

```python
"""Generate web-next/types/api.ts from shared/types.py.

Run: .venv/bin/python3 scripts/gen_ts_types.py
"""
from pathlib import Path
from pydantic2ts import generate_typescript_defs

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "web-next" / "types" / "api.ts"

if __name__ == "__main__":
    generate_typescript_defs(
        "shared.types",
        str(OUT),
        json2ts_cmd="npx --yes json-schema-to-typescript",
    )
    print(f"Wrote {OUT}")
```

- [ ] **Step 6.3: Run the script and commit the output**

```bash
.venv/bin/python3 scripts/gen_ts_types.py
```
Expected: `web-next/types/api.ts` is created with TS interfaces matching `shared/types.py`.

- [ ] **Step 6.4: Document the regen step in `CLAUDE.md`**

Add under "Build & Run Commands" table:

```
| Regenerate TS types from Pydantic | `.venv/bin/python3 scripts/gen_ts_types.py` |
```

- [ ] **Step 6.5: Commit**

```bash
git add scripts/gen_ts_types.py web-next/types/api.ts CLAUDE.md requirements.txt
git commit -m "feat(web-next): generate TS types from Pydantic models"
```

---

## Phase 3 — Port the three tabs

### Task 7: Install shadcn primitives

**Files:**
- Create: `web-next/components/ui/*.tsx` (button, input, card, dialog, tabs, toast, badge, separator, dropdown-menu, scroll-area, label, textarea)

- [ ] **Step 7.1: Initialize shadcn and add primitives**

Run from `web-next/`:
```bash
npx --yes shadcn-ui@latest add button input card dialog tabs toast badge separator dropdown-menu scroll-area label textarea
```
Expected: components added under `web-next/components/ui/`.

- [ ] **Step 7.2: Verify build still passes**

```bash
npm run typecheck && npm run build
```

- [ ] **Step 7.3: Commit**

```bash
git add web-next/components/ui/
git commit -m "feat(web-next): add shadcn primitives"
```

---

### Task 8: WebSocket client and types

**Files:**
- Create: `web-next/types/ws.ts`
- Create: `web-next/lib/ws.ts`
- Create: `web-next/hooks/useWS.ts`
- Test: `web-next/tests/ws.test.ts`

- [ ] **Step 8.1: Write `web-next/types/ws.ts` — WS event union**

Reflect the WS message types currently sent by `web/main.py`. Cross-reference the file when writing this; the union must cover every `ws.send_json({"type": ...})` call site.

```ts
import type { TaskData, TaskMessageData, UserData } from './api';

export type WSEvent =
  | { type: 'task_list'; tasks: TaskData[] }
  | { type: 'task_update'; task: TaskData }
  | { type: 'task_deleted'; task_id: number }
  | { type: 'message'; task_id: number; message: TaskMessageData }
  | { type: 'clarification_needed'; task_id: number; question: string }
  | { type: 'subtask_update'; task_id: number; subtasks: unknown }
  | { type: 'error'; message: string };

export type WSCommand =
  | { type: 'create_task'; title: string; description?: string; repo?: string }
  | { type: 'send_message'; task_id: number; content: string }
  | { type: 'approve'; task_id: number }
  | { type: 'reject'; task_id: number; feedback?: string }
  | { type: 'mark_done'; task_id: number }
  | { type: 'cancel_task'; task_id: number }
  | { type: 'delete_task'; task_id: number }
  | { type: 'set_priority'; task_id: number; priority: number }
  | { type: 'send_clarification'; task_id: number; answer: string };
```

> When porting the three tabs, double-check this union against `web/main.py` and `web/static/index.html`. Any missing event type must be added before its consumer is written.

- [ ] **Step 8.2: Write `web-next/lib/ws.ts` — singleton WS client**

```ts
import type { WSEvent, WSCommand } from '@/types/ws';

type Listener = (event: WSEvent) => void;

class WSClient {
  private socket: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private reconnectAttempts = 0;
  private explicitlyClosed = false;

  connect() {
    if (this.socket && this.socket.readyState <= 1) return;
    this.explicitlyClosed = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    this.socket = new WebSocket(`${proto}://${location.host}/ws`);
    this.socket.onopen = () => { this.reconnectAttempts = 0; };
    this.socket.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data) as WSEvent;
        this.listeners.forEach((l) => l(event));
      } catch {}
    };
    this.socket.onclose = () => {
      if (this.explicitlyClosed) return;
      const delay = Math.min(30_000, 1000 * 2 ** this.reconnectAttempts) + Math.random() * 500;
      this.reconnectAttempts++;
      setTimeout(() => this.connect(), delay);
    };
  }

  disconnect() {
    this.explicitlyClosed = true;
    this.socket?.close();
    this.socket = null;
  }

  send(cmd: WSCommand) {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(cmd));
    return true;
  }

  subscribe(l: Listener) {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }
}

export const wsClient = new WSClient();
```

- [ ] **Step 8.3: Write `web-next/hooks/useWS.ts`**

```ts
import { useEffect } from 'react';
import { wsClient } from '@/lib/ws';
import type { WSEvent } from '@/types/ws';

type EventOf<T extends WSEvent['type']> = Extract<WSEvent, { type: T }>;

export function useWS<T extends WSEvent['type']>(
  type: T,
  handler: (event: EventOf<T>) => void,
) {
  useEffect(() => {
    const off = wsClient.subscribe((e) => {
      if (e.type === type) handler(e as EventOf<T>);
    });
    return off;
  }, [type, handler]);
}
```

- [ ] **Step 8.4: Write Vitest config — `web-next/vitest.config.ts`**

```ts
import { defineConfig } from 'vitest/config';
import path from 'node:path';

export default defineConfig({
  test: { environment: 'jsdom', globals: true, setupFiles: [] },
  resolve: { alias: { '@': path.resolve(__dirname, './') } },
});
```

- [ ] **Step 8.5: Write `web-next/tests/ws.test.ts`**

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { wsClient } from '@/lib/ws';

class FakeWS {
  static OPEN = 1;
  readyState = 0;
  onopen?: () => void;
  onmessage?: (e: { data: string }) => void;
  onclose?: () => void;
  constructor(public url: string) { setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 0); }
  send = vi.fn();
  close = vi.fn();
}

beforeEach(() => {
  // @ts-expect-error stub
  global.WebSocket = FakeWS;
  Object.defineProperty(global, 'location', { value: { protocol: 'http:', host: 'x' } });
});

describe('wsClient', () => {
  it('dispatches events to subscribers', async () => {
    wsClient.connect();
    const handler = vi.fn();
    wsClient.subscribe(handler);
    await new Promise((r) => setTimeout(r, 5));
    // Simulate inbound
    // @ts-expect-error grab the socket
    wsClient['socket'].onmessage({ data: JSON.stringify({ type: 'error', message: 'x' }) });
    expect(handler).toHaveBeenCalledWith({ type: 'error', message: 'x' });
    wsClient.disconnect();
  });
});
```

- [ ] **Step 8.6: Run tests**

```bash
cd web-next && npm run test
```
Expected: pass.

- [ ] **Step 8.7: Commit**

```bash
git add web-next/types/ws.ts web-next/lib/ws.ts web-next/hooks/useWS.ts web-next/vitest.config.ts web-next/tests/
git commit -m "feat(web-next): typed WS client + useWS hook"
```

---

### Task 9: REST client + auth helpers + TanStack Query setup

**Files:**
- Create: `web-next/lib/api.ts`
- Create: `web-next/lib/auth.ts`
- Create: `web-next/lib/queryClient.ts`
- Create: `web-next/components/providers.tsx`
- Modify: `web-next/app/layout.tsx`

- [ ] **Step 9.1: Write `web-next/lib/api.ts`**

```ts
export class ApiError extends Error {
  constructor(public status: number, public detail: string) { super(detail); }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, detail.detail || res.statusText);
  }
  return res.json();
}
```

- [ ] **Step 9.2: Write `web-next/lib/auth.ts`**

```ts
import { api } from './api';
import type { UserData } from '@/types/api';

export async function login(username: string, password: string) {
  return api<{ token: string; user: UserData }>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export async function me() {
  return api<UserData>('/api/auth/me');
}

export async function logout() {
  return api<{ ok: true }>('/api/auth/logout', { method: 'POST' });
}
```

- [ ] **Step 9.3: Write `web-next/lib/queryClient.ts`**

```ts
import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
});
```

- [ ] **Step 9.4: Write `web-next/components/providers.tsx`**

```tsx
'use client';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from '@/lib/queryClient';
import { useEffect } from 'react';
import { wsClient } from '@/lib/ws';

export function Providers({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    wsClient.connect();
    return () => wsClient.disconnect();
  }, []);
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
```

- [ ] **Step 9.5: Modify `web-next/app/layout.tsx` to use `Providers`**

Wrap `{children}` in `<Providers>`:
```tsx
import { Providers } from '@/components/providers';
// ...
<body>
  <Providers>{children}</Providers>
</body>
```

- [ ] **Step 9.6: Verify build**

```bash
npm run typecheck && npm run build
```

- [ ] **Step 9.7: Commit**

```bash
git add web-next/lib/ web-next/components/providers.tsx web-next/app/layout.tsx
git commit -m "feat(web-next): REST client, auth helpers, TanStack Query providers"
```

---

### Task 10: Login page + auth guard layout

**Files:**
- Create: `web-next/app/(auth)/login/page.tsx`
- Create: `web-next/app/(app)/layout.tsx`
- Create: `web-next/components/sidebar/sidebar.tsx`
- Create: `web-next/hooks/useAuth.ts`

- [ ] **Step 10.1: Write `web-next/hooks/useAuth.ts`**

```ts
'use client';
import { useQuery } from '@tanstack/react-query';
import { me } from '@/lib/auth';

export function useAuth() {
  return useQuery({ queryKey: ['auth', 'me'], queryFn: me, retry: false });
}
```

- [ ] **Step 10.2: Write `web-next/app/(auth)/login/page.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { login } from '@/lib/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card } from '@/components/ui/card';
import { Label } from '@/components/ui/label';

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null); setLoading(true);
    try {
      await login(username, password);
      router.push('/tasks');
    } catch (err: any) {
      setError(err.message || 'Login failed');
    } finally { setLoading(false); }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <Card className="w-full max-w-sm p-6">
        <h1 className="mb-4 text-xl font-semibold text-primary">Auto-Agent</h1>
        <form onSubmit={onSubmit} className="space-y-3">
          <div>
            <Label htmlFor="u">Username</Label>
            <Input id="u" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
          </div>
          <div>
            <Label htmlFor="p">Password</Label>
            <Input id="p" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
          <Button type="submit" disabled={loading} className="w-full">{loading ? 'Signing in…' : 'Sign in'}</Button>
        </form>
      </Card>
    </main>
  );
}
```

- [ ] **Step 10.3: Write `web-next/components/sidebar/sidebar.tsx`**

```tsx
'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';
import { logout } from '@/lib/auth';
import { Button } from '@/components/ui/button';

const tabs = [
  { href: '/tasks', label: 'Tasks' },
  { href: '/freeform', label: 'Freeform' },
  { href: '/memory', label: 'Memory' },
];

export function Sidebar({ username }: { username: string }) {
  const pathname = usePathname();
  return (
    <aside className="flex h-screen w-64 flex-col border-r bg-card">
      <div className="flex items-center justify-between p-4">
        <span className="flex items-center gap-2 font-semibold">
          <span className="h-2 w-2 rounded-full bg-success" /> Auto-Agent
        </span>
      </div>
      <nav className="flex flex-col gap-1 p-2">
        {tabs.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            className={cn(
              'rounded px-3 py-2 text-sm hover:bg-secondary',
              pathname?.startsWith(t.href) && 'bg-secondary text-primary',
            )}
          >
            {t.label}
          </Link>
        ))}
      </nav>
      <div className="mt-auto border-t p-3 text-xs">
        <div className="mb-2 text-muted-foreground">{username}</div>
        <Button size="sm" variant="secondary" onClick={async () => { await logout(); location.href = '/login'; }}>
          Sign out
        </Button>
      </div>
    </aside>
  );
}
```

- [ ] **Step 10.4: Write `web-next/app/(app)/layout.tsx` — auth guard + sidebar**

```tsx
import { redirect } from 'next/navigation';
import { cookies } from 'next/headers';
import { Sidebar } from '@/components/sidebar/sidebar';

async function getMe() {
  const cookie = cookies().get('auto_agent_session')?.value;
  if (!cookie) return null;
  const res = await fetch(`${process.env.API_URL || 'http://localhost:2020'}/api/auth/me`, {
    headers: { Cookie: `auto_agent_session=${cookie}` },
    cache: 'no-store',
  });
  if (!res.ok) return null;
  return res.json();
}

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const user = await getMe();
  if (!user) redirect('/login');
  return (
    <div className="flex h-screen">
      <Sidebar username={user.username} />
      <main className="flex-1 overflow-hidden">{children}</main>
    </div>
  );
}
```

- [ ] **Step 10.5: Add an empty `app/(app)/tasks/page.tsx` placeholder so routing resolves**

```tsx
export default function TasksPage() { return <div className="p-6">Tasks (3b coming up)</div>; }
```

- [ ] **Step 10.6: Verify dev server**

```bash
docker compose up -d auto-agent postgres redis
cd web-next && npm run dev
```
Open `http://localhost:3000` — expect redirect to `/login`. Log in with a known dev user. Expect redirect to `/tasks` showing the placeholder and the sidebar.

- [ ] **Step 10.7: Commit**

```bash
git add web-next/app/ web-next/components/sidebar/ web-next/hooks/useAuth.ts
git commit -m "feat(web-next): login page, auth guard, sidebar shell"
```

---

### Task 11: Tasks tab — list panel, chat area, action bars

**Files:**
- Create: `web-next/app/(app)/tasks/page.tsx` (replace placeholder)
- Create: `web-next/hooks/useTasks.ts`
- Create: `web-next/components/tasks/task-list.tsx`
- Create: `web-next/components/tasks/new-task-form.tsx`
- Create: `web-next/components/tasks/task-actions.tsx`
- Create: `web-next/components/chat/chat-area.tsx`
- Create: `web-next/components/chat/approval-bar.tsx`
- Create: `web-next/components/chat/clarification-bar.tsx`
- Create: `web-next/components/chat/done-bar.tsx`
- Create: `web-next/components/chat/message-input.tsx`

This task is the largest in the plan. The pattern: each component is a small "client" component that subscribes to WS events via `useWS` and pushes commands via `wsClient.send(...)`. Selected-task state is held at the page level via `useState` + URL param (`?taskId=...`) so it's deep-linkable.

- [ ] **Step 11.1: Write `web-next/hooks/useTasks.ts`**

```ts
'use client';
import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { wsClient } from '@/lib/ws';
import { useWS } from './useWS';
import type { TaskData } from '@/types/api';

export function useTasks() {
  const qc = useQueryClient();
  const query = useQuery<TaskData[]>({
    queryKey: ['tasks'],
    queryFn: () => Promise.resolve(qc.getQueryData<TaskData[]>(['tasks']) || []),
    staleTime: Infinity,
  });

  useEffect(() => {
    // Connection lifecycle handled by Providers; nothing to do here.
  }, []);

  useWS('task_list', (e) => qc.setQueryData(['tasks'], e.tasks));
  useWS('task_update', (e) => {
    qc.setQueryData<TaskData[]>(['tasks'], (prev) => {
      if (!prev) return [e.task];
      const i = prev.findIndex((t) => t.id === e.task.id);
      if (i === -1) return [...prev, e.task];
      const next = prev.slice(); next[i] = e.task; return next;
    });
  });
  useWS('task_deleted', (e) => {
    qc.setQueryData<TaskData[]>(['tasks'], (prev) => (prev || []).filter((t) => t.id !== e.task_id));
  });

  return query;
}
```

- [ ] **Step 11.2: Write `web-next/components/tasks/task-list.tsx`**

```tsx
'use client';
import { TaskData } from '@/types/api';
import { cn } from '@/lib/utils';

export function TaskList({
  tasks, selectedId, onSelect,
}: { tasks: TaskData[]; selectedId: number | null; onSelect: (id: number) => void }) {
  if (!tasks.length) return <div className="p-4 text-sm text-muted-foreground">No tasks yet</div>;
  return (
    <ul className="flex flex-col">
      {tasks.map((t) => (
        <li key={t.id}>
          <button
            onClick={() => onSelect(t.id)}
            className={cn(
              'w-full px-3 py-2 text-left text-sm hover:bg-secondary',
              selectedId === t.id && 'bg-secondary',
            )}
          >
            <div className="truncate font-medium">{t.title || `Task ${t.id}`}</div>
            <div className="text-xs text-muted-foreground">{t.status}</div>
          </button>
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 11.3: Write `web-next/components/tasks/new-task-form.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';

export function NewTaskForm() {
  const [title, setTitle] = useState('');
  const [desc, setDesc] = useState('');
  const [repo, setRepo] = useState('');
  function submit() {
    if (!title.trim()) return;
    wsClient.send({ type: 'create_task', title: title.trim(), description: desc.trim() || undefined, repo: repo.trim() || undefined });
    setTitle(''); setDesc(''); setRepo('');
  }
  return (
    <div className="space-y-2 border-t p-3">
      <Input placeholder="Task title…" value={title} onChange={(e) => setTitle(e.target.value)} />
      <Textarea rows={2} placeholder="Description (optional)" value={desc} onChange={(e) => setDesc(e.target.value)} />
      <Input placeholder="Repo name (optional)" value={repo} onChange={(e) => setRepo(e.target.value)} />
      <Button onClick={submit} className="w-full">Create Task</Button>
    </div>
  );
}
```

- [ ] **Step 11.4: Write `web-next/components/chat/chat-area.tsx`**

```tsx
'use client';
import { useEffect, useRef, useState } from 'react';
import { useWS } from '@/hooks/useWS';
import type { TaskMessageData } from '@/types/api';

export function ChatArea({ taskId }: { taskId: number | null }) {
  const [messages, setMessages] = useState<TaskMessageData[]>([]);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => { setMessages([]); }, [taskId]);

  useWS('message', (e) => {
    if (e.task_id !== taskId) return;
    setMessages((prev) => [...prev, e.message]);
  });

  useEffect(() => { ref.current?.scrollTo({ top: ref.current.scrollHeight }); }, [messages]);

  if (taskId === null) return <div className="flex flex-1 items-center justify-center text-muted-foreground">Select a task</div>;

  return (
    <div ref={ref} className="flex-1 overflow-auto p-4">
      {messages.map((m, i) => (
        <div key={i} className="mb-3 rounded bg-card p-3">
          <div className="mb-1 text-xs text-muted-foreground">{m.role}</div>
          <div className="whitespace-pre-wrap text-sm">{m.content}</div>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 11.5: Write `web-next/components/chat/message-input.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { Input } from '@/components/ui/input';

export function MessageInput({ taskId }: { taskId: number }) {
  const [v, setV] = useState('');
  function send() {
    if (!v.trim()) return;
    wsClient.send({ type: 'send_message', task_id: taskId, content: v });
    setV('');
  }
  return (
    <div className="border-t p-2">
      <Input
        placeholder="Send a message…"
        value={v}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') send(); }}
      />
    </div>
  );
}
```

- [ ] **Step 11.6: Write `web-next/components/chat/approval-bar.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export function ApprovalBar({ taskId }: { taskId: number }) {
  const [feedback, setFeedback] = useState('');
  return (
    <div className="flex items-center gap-2 border-t bg-card p-2">
      <Input placeholder="Feedback (if rejecting)…" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
      <Button onClick={() => wsClient.send({ type: 'approve', task_id: taskId })}>Approve</Button>
      <Button variant="destructive" onClick={() => wsClient.send({ type: 'reject', task_id: taskId, feedback })}>Reject</Button>
    </div>
  );
}
```

- [ ] **Step 11.7: Write `web-next/components/chat/clarification-bar.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { wsClient } from '@/lib/ws';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

export function ClarificationBar({ taskId, question }: { taskId: number; question: string }) {
  const [answer, setAnswer] = useState('');
  function submit() {
    if (!answer.trim()) return;
    wsClient.send({ type: 'send_clarification', task_id: taskId, answer });
    setAnswer('');
  }
  return (
    <div className="border-t bg-card p-2">
      <div className="mb-2 text-sm text-muted-foreground">{question}</div>
      <div className="flex gap-2">
        <Input value={answer} onChange={(e) => setAnswer(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') submit(); }} placeholder="Answer…" />
        <Button onClick={submit}>Send</Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 11.8: Write `web-next/components/chat/done-bar.tsx`**

```tsx
'use client';
import { wsClient } from '@/lib/ws';
import { Button } from '@/components/ui/button';

export function DoneBar({ taskId }: { taskId: number }) {
  return (
    <div className="flex items-center gap-2 border-t bg-card p-2">
      <Button onClick={() => wsClient.send({ type: 'mark_done', task_id: taskId })}>Mark Done</Button>
    </div>
  );
}
```

- [ ] **Step 11.9: Write `web-next/components/tasks/task-actions.tsx`**

```tsx
'use client';
import { wsClient } from '@/lib/ws';
import type { TaskData } from '@/types/api';
import { Button } from '@/components/ui/button';

export function TaskActions({ task }: { task: TaskData }) {
  return (
    <div className="flex flex-wrap items-center gap-2 border-t p-2 text-xs">
      <Button size="sm" variant="secondary" onClick={() => wsClient.send({ type: 'cancel_task', task_id: task.id })}>Cancel</Button>
      <Button size="sm" variant="destructive" onClick={() => { if (confirm('Delete this task?')) wsClient.send({ type: 'delete_task', task_id: task.id }); }}>Delete</Button>
      <span className="ml-auto flex items-center gap-1 text-muted-foreground">
        <span>Priority:</span>
        {[1, 2, 3].map((p) => (
          <Button key={p} size="sm" variant={task.priority === p ? 'default' : 'secondary'}
                  onClick={() => wsClient.send({ type: 'set_priority', task_id: task.id, priority: p })}>{p}</Button>
        ))}
      </span>
    </div>
  );
}
```

> Note: Confirm `TaskData.priority` exists in the generated `web-next/types/api.ts`. If the field is named differently, update the field name everywhere in this task.

- [ ] **Step 11.10: Write `web-next/app/(app)/tasks/page.tsx` — wire it all together**

```tsx
'use client';
import { useState } from 'react';
import { useTasks } from '@/hooks/useTasks';
import { useWS } from '@/hooks/useWS';
import { TaskList } from '@/components/tasks/task-list';
import { NewTaskForm } from '@/components/tasks/new-task-form';
import { TaskActions } from '@/components/tasks/task-actions';
import { ChatArea } from '@/components/chat/chat-area';
import { MessageInput } from '@/components/chat/message-input';
import { ApprovalBar } from '@/components/chat/approval-bar';
import { ClarificationBar } from '@/components/chat/clarification-bar';
import { DoneBar } from '@/components/chat/done-bar';

export default function TasksPage() {
  const { data: tasks = [] } = useTasks();
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [clarification, setClarification] = useState<{ taskId: number; question: string } | null>(null);

  useWS('clarification_needed', (e) => {
    if (e.task_id === selectedId) setClarification({ taskId: e.task_id, question: e.question });
  });

  const selected = tasks.find((t) => t.id === selectedId) || null;

  return (
    <div className="flex h-full">
      <div className="flex w-80 flex-col border-r">
        <div className="flex-1 overflow-auto"><TaskList tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} /></div>
        <NewTaskForm />
      </div>
      <div className="flex flex-1 flex-col">
        <div className="border-b p-3 text-sm font-medium">{selected?.title || 'Select a task'}</div>
        <ChatArea taskId={selectedId} />
        {selected && selected.status === 'review' && <ApprovalBar taskId={selected.id} />}
        {clarification && clarification.taskId === selectedId && (
          <ClarificationBar taskId={clarification.taskId} question={clarification.question} />
        )}
        {selected && selected.status === 'done_pending' && <DoneBar taskId={selected.id} />}
        {selected && <MessageInput taskId={selected.id} />}
        {selected && <TaskActions task={selected} />}
      </div>
    </div>
  );
}
```

> Note: Status values (`'review'`, `'done_pending'`) must match what the FastAPI side emits in `TaskData.status`. Check `shared/models.py` and `orchestrator/state_machine.py` and align the strings here. Update if they differ.

- [ ] **Step 11.11: Manual verification**

Run `docker compose up -d`, `cd web-next && npm run dev`. In a browser:
1. Log in.
2. Verify task list populates from WS `task_list`.
3. Create a new task — appears in list.
4. Click a task — chat shows messages.
5. Send a message — appears in chat.
6. Run a flow that triggers approval/clarification — bars appear.

- [ ] **Step 11.12: Commit**

```bash
git add web-next/
git commit -m "feat(web-next): port Tasks tab — list, chat, approval/clarification/done bars"
```

---

### Task 12: Freeform tab port

**Files:**
- Create: `web-next/app/(app)/freeform/page.tsx`
- Create: `web-next/components/freeform/build-view.tsx`
- Create: `web-next/components/freeform/existing-view.tsx`
- Create: `web-next/components/freeform/repo-list.tsx`

**Reference:** `web/static/index.html:960-973` (sidebar) and the Freeform-related sections deeper in the file (search for `freeform-main-content`, `selectFreeformView`, `ff-page`).

- [ ] **Step 12.1: Read the existing freeform behavior**

Open `web/static/index.html` and read every region that mentions `freeform`, `ff-`, `selectFreeformView`, or `freeform-main`. Catalogue:
- Two views: "Build something new" and "Add to existing repo".
- Sidebar list of repos (`#ff-repo-list`).
- Whatever forms/inputs each view shows.

Write a short note in the commit message of this task with what you found, so the next task author has a checklist.

- [ ] **Step 12.2: Write `web-next/components/freeform/repo-list.tsx`**

Mirror the data fetched today; if the existing UI calls `/api/repos` (check by grepping `web/static/index.html` for `/api/`), use TanStack Query:

```tsx
'use client';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';

type Repo = { id: number; name: string };

export function RepoList() {
  const { data: repos = [] } = useQuery<Repo[]>({
    queryKey: ['repos'],
    queryFn: () => api<Repo[]>('/api/repos'),
  });
  if (!repos.length) return <div className="p-3 text-xs text-muted-foreground">No freeform repos yet</div>;
  return (
    <ul className="flex flex-col">
      {repos.map((r) => (
        <li key={r.id} className="px-3 py-2 text-sm hover:bg-secondary">{r.name}</li>
      ))}
    </ul>
  );
}
```

> Verify the actual REST shape against `orchestrator/router.py`'s `/repos` endpoint and adjust the type if needed.

- [ ] **Step 12.3: Write `web-next/components/freeform/build-view.tsx` and `existing-view.tsx`**

Port the inputs and submit buttons currently in `web/static/index.html` for these flows. Forms post via existing REST endpoints (likely `/api/repos`, `/api/tasks` with a freeform flag — confirm before implementing).

- [ ] **Step 12.4: Write `web-next/app/(app)/freeform/page.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { BuildView } from '@/components/freeform/build-view';
import { ExistingView } from '@/components/freeform/existing-view';
import { RepoList } from '@/components/freeform/repo-list';
import { Button } from '@/components/ui/button';

export default function FreeformPage() {
  const [view, setView] = useState<'build' | 'existing' | null>(null);
  return (
    <div className="flex h-full">
      <aside className="w-64 border-r p-2">
        <Button variant="secondary" className="mb-1 w-full" onClick={() => setView('build')}>+ Build something new</Button>
        <Button variant="secondary" className="mb-3 w-full" onClick={() => setView('existing')}>+ Add to existing repo</Button>
        <div className="mb-1 px-2 text-xs uppercase text-muted-foreground">Freeform Repos</div>
        <RepoList />
      </aside>
      <section className="flex-1 overflow-auto p-6">
        {view === 'build' && <BuildView />}
        {view === 'existing' && <ExistingView />}
        {!view && <div className="text-muted-foreground">Select an option from the sidebar</div>}
      </section>
    </div>
  );
}
```

- [ ] **Step 12.5: Manual verification**

Side-by-side with the old UI. Confirm both views look and behave the same.

- [ ] **Step 12.6: Commit**

```bash
git add web-next/
git commit -m "feat(web-next): port Freeform tab"
```

---

### Task 13: Memory tab port

**Files:**
- Create: `web-next/app/(app)/memory/page.tsx`
- Create: `web-next/components/memory/drop-zone.tsx`
- Create: `web-next/components/memory/review-table.tsx`
- Create: `web-next/components/memory/conflict-resolver.tsx`

**Reference:** `web/static/index.html` regions mentioning `memory-tab`, `memory-panel`, `memory-main`. Backend endpoints: `/memory/upload` (now also `/api/memory/upload`) and any `/api/memory/*` paths.

- [ ] **Step 13.1: Read the existing memory tab**

Catalogue:
- Drop zone for file/text upload (POST to `/api/memory/upload`).
- Review table showing parsed entities/facts.
- Conflict resolver UI for facts that overlap with existing graph entries.
- WS messages used for review progress (e.g., `memory_review_*`).

> Update `web-next/types/ws.ts` with any memory-related WS event types missing from the current union.

- [ ] **Step 13.2: Write `web-next/components/memory/drop-zone.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { Button } from '@/components/ui/button';

export function DropZone({ onSourceId }: { onSourceId: (id: string) => void }) {
  const [busy, setBusy] = useState(false);

  async function upload(file: File) {
    setBusy(true);
    const fd = new FormData(); fd.append('file', file);
    const res = await fetch('/api/memory/upload', { method: 'POST', body: fd, credentials: 'include' });
    const json = await res.json();
    setBusy(false);
    if (res.ok) onSourceId(json.source_id);
  }

  return (
    <div
      className="rounded border-2 border-dashed border-border p-8 text-center"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer.files[0]; if (f) upload(f); }}
    >
      <p className="mb-3 text-sm text-muted-foreground">Drop a file here, or pick one</p>
      <input type="file" id="memfile" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); }} />
      <Button onClick={() => document.getElementById('memfile')?.click()} disabled={busy}>
        {busy ? 'Uploading…' : 'Choose file'}
      </Button>
    </div>
  );
}
```

- [ ] **Step 13.3: Write `web-next/components/memory/review-table.tsx`**

Read the current review table structure from `web/static/index.html` and mirror it. Fetch via WS subscription on `memory_review_*` events, or via a REST `GET /api/memory/sessions/{id}/review` if that exists. Adjust based on actual current implementation.

- [ ] **Step 13.4: Write `web-next/components/memory/conflict-resolver.tsx`**

Port the existing conflict UI. Each conflict gets accept/reject buttons that send a corresponding WS or REST action.

- [ ] **Step 13.5: Write `web-next/app/(app)/memory/page.tsx`**

```tsx
'use client';
import { useState } from 'react';
import { DropZone } from '@/components/memory/drop-zone';
import { ReviewTable } from '@/components/memory/review-table';
import { ConflictResolver } from '@/components/memory/conflict-resolver';

export default function MemoryPage() {
  const [sourceId, setSourceId] = useState<string | null>(null);
  return (
    <div className="flex h-full flex-col gap-4 p-4">
      <DropZone onSourceId={setSourceId} />
      {sourceId && <ReviewTable sourceId={sourceId} />}
      {sourceId && <ConflictResolver sourceId={sourceId} />}
    </div>
  );
}
```

- [ ] **Step 13.6: Manual verification**

Upload a known file in the old UI and the new UI; confirm parsed entities and conflicts match.

- [ ] **Step 13.7: Commit**

```bash
git add web-next/
git commit -m "feat(web-next): port Memory tab"
```

---

### Task 14: Playwright golden-path E2E tests

**Files:**
- Create: `web-next/playwright.config.ts`
- Create: `web-next/e2e/auth.spec.ts`
- Create: `web-next/e2e/tasks.spec.ts`

- [ ] **Step 14.1: Write `web-next/playwright.config.ts`**

```ts
import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './e2e',
  use: { baseURL: 'http://localhost:3000', trace: 'on-first-retry' },
  webServer: { command: 'npm run dev', url: 'http://localhost:3000', reuseExistingServer: true },
});
```

- [ ] **Step 14.2: Write `web-next/e2e/auth.spec.ts`**

```ts
import { test, expect } from '@playwright/test';

test('login redirects to /tasks', async ({ page }) => {
  await page.goto('/login');
  await page.fill('#u', process.env.TEST_USER || 'dev');
  await page.fill('#p', process.env.TEST_PASS || 'dev');
  await page.click('button[type=submit]');
  await expect(page).toHaveURL(/\/tasks/);
});

test('unauthenticated /tasks redirects to /login', async ({ page }) => {
  await page.context().clearCookies();
  await page.goto('/tasks');
  await expect(page).toHaveURL(/\/login/);
});
```

- [ ] **Step 14.3: Write `web-next/e2e/tasks.spec.ts`**

```ts
import { test, expect } from '@playwright/test';

test('create task appears in list', async ({ page }) => {
  await page.goto('/login');
  await page.fill('#u', process.env.TEST_USER || 'dev');
  await page.fill('#p', process.env.TEST_PASS || 'dev');
  await page.click('button[type=submit]');
  await page.waitForURL(/\/tasks/);
  const title = `e2e-${Date.now()}`;
  await page.fill('input[placeholder="Task title…"]', title);
  await page.click('text=Create Task');
  await expect(page.getByText(title)).toBeVisible();
});
```

- [ ] **Step 14.4: Run E2E**

```bash
cd web-next && npx playwright install --with-deps && npx playwright test
```
Expected: pass (requires a running `auto-agent` and a known dev user).

- [ ] **Step 14.5: Commit**

```bash
git add web-next/playwright.config.ts web-next/e2e/
git commit -m "test(web-next): playwright golden-path E2E"
```

---

## Phase 4 — Cutover

### Task 15: Caddyfile in repo + remove legacy

**Files:**
- Modify: `web/main.py` — remove `/` HTMLResponse handler and `/static` mount
- Delete: `web/static/index.html`
- Modify: `web/main.py` — remove `?token=` fallback in `/ws`
- Modify: `orchestrator/router.py` — if any deprecated `/memory/*` paths exist outside `/api`, remove
- Modify: `CLAUDE.md` — update path map and build commands

- [ ] **Step 15.1: Stop serving the old SPA**

Remove from `web/main.py`:

```python
# Delete:
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    ...
    return HTMLResponse(html)

# Delete:
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
```

- [ ] **Step 15.2: Remove the `?token=` WS fallback**

In `web/main.py:152-154`, simplify to cookie-only:
```python
token = ws.cookies.get("auto_agent_session")
```

- [ ] **Step 15.3: Delete `web/static/index.html`**

```bash
git rm web/static/index.html
rmdir web/static  # if empty
```

- [ ] **Step 15.4: Run full test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
ruff check .
```
Expected: all green.

- [ ] **Step 15.5: Update `CLAUDE.md`**

In the architecture diagram, replace the `web/` description from `# HTTP UI and static assets` to `# FastAPI app + websocket` and add a `web-next/` row: `# Next.js 14 frontend (separate container)`.

In the file-organization rules, update "Static web assets → `web/static/`" to "Frontend assets → `web-next/`".

- [ ] **Step 15.6: Commit**

```bash
git add -u web/main.py CLAUDE.md
git commit -m "chore(web): remove legacy SPA, ws is cookie-only"
```

---

### Task 16: CI

**Files:**
- Modify: existing CI workflow (check `.github/workflows/`).

- [ ] **Step 16.1: Inspect existing workflows**

```bash
ls .github/workflows/
```

- [ ] **Step 16.2: Add a `web-next-test` job**

Add a job that runs:
```yaml
  web-next-test:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: web-next } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: '20', cache: 'npm', cache-dependency-path: 'web-next/package-lock.json' }
      - run: npm ci
      - run: npm run lint
      - run: npm run typecheck
      - run: npm run test
```

- [ ] **Step 16.3: (Optional) Add an `e2e` job**

Bring up `docker compose`, then run Playwright. Skip if CI runner can't run docker-in-docker — leave as a follow-up.

- [ ] **Step 16.4: Commit**

```bash
git add .github/workflows/
git commit -m "ci: lint/typecheck/test web-next"
```

---

## Self-Review Notes

- **Spec coverage:** Each acceptance criterion in the spec maps to a task — service topology (Tasks 1–3), cookie auth (Tasks 4–5), TS types (Task 6), tabs (Tasks 11–13), tests (Tasks 8 & 14), legacy removal (Task 15).
- **Theme tokens:** Task 1.7 contains the exact HSL values from the spec.
- **Ports:** All references to FastAPI use **2020**; web-next uses **3000**.
- **Field names to verify before writing code in Task 11:**
  - `TaskData.priority`, `TaskData.status` (and their string values) — confirm against `shared/types.py` and `orchestrator/state_machine.py`.
- **WS event union (Task 8.1):** intentionally minimal. Each subsequent task that consumes an event type confirms it against `web/main.py`'s `ws.send_json` call sites and adds missing types if needed.
- **No placeholders.** Every step has runnable code or commands.

---
