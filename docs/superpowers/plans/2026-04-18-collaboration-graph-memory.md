# Collaboration & Graph Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-user authentication and shared graph memory to auto-agent so a small team (2-5 people) can collaborate with full shared access and persistent cross-project knowledge.

**Architecture:** Username/password auth with JWT sessions, Postgres adjacency list for graph memory (MemoryNode + MemoryEdge tables), two new agent tools (memory_read, memory_write), memory context injection at task start, post-task reflection for decision recording.

**Tech Stack:** passlib[bcrypt], PyJWT, SQLAlchemy async ORM, PostgreSQL, existing FastAPI/WebSocket stack.

---

### Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt` (or `pyproject.toml` — whichever manages deps)

- [ ] **Step 1: Check which file manages dependencies**

```bash
ls -la /Users/alanyeginchibayev/Documents/Github/auto-agent/requirements*.txt /Users/alanyeginchibayev/Documents/Github/auto-agent/pyproject.toml 2>/dev/null
```

- [ ] **Step 2: Add passlib and PyJWT**

Add these lines to the dependencies file:

```
passlib[bcrypt]>=1.7.4
PyJWT>=2.8.0
```

- [ ] **Step 3: Install and verify**

```bash
pip install passlib[bcrypt] PyJWT
```

- [ ] **Step 4: Commit**

```bash
git add requirements.txt  # or pyproject.toml
git commit -m "feat: add passlib and PyJWT dependencies for auth"
```

---

### Task 2: Add User, MemoryNode, MemoryEdge ORM models

**Files:**
- Modify: `shared/models.py`
- Modify: `shared/types.py`
- Test: `tests/test_models_memory.py`

- [ ] **Step 1: Write failing test for User model**

Create `tests/test_models_memory.py`:

```python
"""Tests for User, MemoryNode, MemoryEdge ORM models."""

from shared.models import MemoryEdge, MemoryNode, User


class TestUserModel:
    def test_user_has_required_fields(self):
        """User model has id, username, password_hash, display_name."""
        user = User(username="alice", password_hash="hashed", display_name="Alice")
        assert user.username == "alice"
        assert user.password_hash == "hashed"
        assert user.display_name == "Alice"

    def test_user_tablename(self):
        assert User.__tablename__ == "users"


class TestMemoryNodeModel:
    def test_node_has_required_fields(self):
        node = MemoryNode(name="python-tooling", node_type="preference", content="use uv, ruff")
        assert node.name == "python-tooling"
        assert node.node_type == "preference"
        assert node.content == "use uv, ruff"

    def test_node_tablename(self):
        assert MemoryNode.__tablename__ == "memory_nodes"


class TestMemoryEdgeModel:
    def test_edge_has_required_fields(self):
        edge = MemoryEdge(relation="has-preference")
        assert edge.relation == "has-preference"

    def test_edge_tablename(self):
        assert MemoryEdge.__tablename__ == "memory_edges"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_models_memory.py -v
```

Expected: ImportError — `User`, `MemoryNode`, `MemoryEdge` do not exist yet.

- [ ] **Step 3: Add models to shared/models.py**

Add these imports at the top of `shared/models.py`:

```python
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
import uuid
```

Add after the `FreeformConfig` class:

```python
class User(Base):
    """Authenticated team member."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    last_login = Column(DateTime(timezone=True), nullable=True)


class MemoryNode(Base):
    """A node in the shared graph memory."""
    __tablename__ = "memory_nodes"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    node_type = Column(String(100), nullable=False)
    content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    created_by_task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)

    created_by_task = relationship("Task", foreign_keys=[created_by_task_id])
    outgoing_edges = relationship("MemoryEdge", foreign_keys="MemoryEdge.source_id", back_populates="source", cascade="all, delete-orphan")
    incoming_edges = relationship("MemoryEdge", foreign_keys="MemoryEdge.target_id", back_populates="target", cascade="all, delete-orphan")


class MemoryEdge(Base):
    """A directed edge between two memory nodes."""
    __tablename__ = "memory_edges"

    id = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id = Column(PG_UUID(as_uuid=True), ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False)
    target_id = Column(PG_UUID(as_uuid=True), ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False)
    relation = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    source = relationship("MemoryNode", foreign_keys=[source_id], back_populates="outgoing_edges")
    target = relationship("MemoryNode", foreign_keys=[target_id], back_populates="incoming_edges")
```

- [ ] **Step 4: Add `created_by_user_id` FK to Task**

In the `Task` class in `shared/models.py`, add after the `priority` column:

```python
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
```

- [ ] **Step 5: Add Pydantic types to shared/types.py**

Add at the end of `shared/types.py`:

```python
# --- Auth types ---


class UserData(BaseModel):
    """Typed representation of a user."""
    id: int
    username: str
    display_name: str
    created_at: str | None = None
    last_login: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: UserData


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str


# --- Graph memory types ---


class MemoryNodeData(BaseModel):
    """Typed representation of a memory node."""
    id: str  # UUID as string
    name: str
    node_type: str
    content: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    created_by_task_id: int | None = None


class MemoryEdgeData(BaseModel):
    """Typed representation of a memory edge."""
    id: str  # UUID as string
    source_id: str
    target_id: str
    relation: str
    created_at: str | None = None


class MemoryNodeWithEdges(BaseModel):
    """A node with its immediate edges for search results."""
    node: MemoryNodeData
    edges: list[MemoryEdgeData] = []
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_models_memory.py -v
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add shared/models.py shared/types.py tests/test_models_memory.py
git commit -m "feat: add User, MemoryNode, MemoryEdge models and Pydantic types"
```

---

### Task 3: Create database migration

**Files:**
- Create: `migrations/versions/016_add_users_and_memory.py`

- [ ] **Step 1: Generate migration**

```bash
cd /Users/alanyeginchibayev/Documents/Github/auto-agent && alembic revision --autogenerate -m "add users and graph memory tables"
```

- [ ] **Step 2: Review the generated migration**

Read the file and verify it creates:
- `users` table with correct columns
- `memory_nodes` table with UUID primary key
- `memory_edges` table with FK constraints and CASCADE deletes
- `created_by_user_id` column added to `tasks` table

- [ ] **Step 3: Commit**

```bash
git add migrations/
git commit -m "feat: add migration for users and graph memory tables"
```

---

### Task 4: Auth module — password hashing and JWT

**Files:**
- Create: `orchestrator/auth.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_auth.py`:

```python
"""Tests for authentication — password hashing and JWT tokens."""

import time

import pytest

from orchestrator.auth import create_token, hash_password, verify_password, verify_token


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("secret123")
        assert hashed != "secret123"
        assert verify_password("secret123", hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("secret123")
        assert not verify_password("wrong", hashed)

    def test_different_hashes_for_same_password(self):
        h1 = hash_password("secret123")
        h2 = hash_password("secret123")
        assert h1 != h2  # bcrypt uses random salt


class TestJWT:
    def test_create_and_verify(self):
        token = create_token(user_id=1, username="alice")
        payload = verify_token(token)
        assert payload is not None
        assert payload["user_id"] == 1
        assert payload["username"] == "alice"

    def test_invalid_token_returns_none(self):
        assert verify_token("garbage.token.here") is None

    def test_expired_token_returns_none(self):
        token = create_token(user_id=1, username="alice", expires_seconds=0)
        time.sleep(1)
        assert verify_token(token) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/test_auth.py -v
```

Expected: ImportError — `orchestrator.auth` does not exist.

- [ ] **Step 3: Implement auth module**

Create `orchestrator/auth.py`:

```python
"""Authentication utilities — password hashing and JWT tokens."""

from __future__ import annotations

import time

import jwt
from passlib.hash import bcrypt

# Secret for JWT signing. In production, set JWT_SECRET env var.
# For a 2-5 person team on a private instance, a hardcoded default is acceptable.
import os

JWT_SECRET = os.environ.get("JWT_SECRET", "auto-agent-jwt-secret-change-me")
JWT_ALGORITHM = "HS256"
DEFAULT_EXPIRY = 7 * 24 * 3600  # 7 days


def hash_password(password: str) -> str:
    """Hash a password with bcrypt."""
    return bcrypt.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Check a password against a bcrypt hash."""
    return bcrypt.verify(password, hashed)


def create_token(
    user_id: int,
    username: str,
    expires_seconds: int = DEFAULT_EXPIRY,
) -> str:
    """Create a JWT token."""
    payload = {
        "user_id": user_id,
        "username": username,
        "exp": int(time.time()) + expires_seconds,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    """Verify and decode a JWT token. Returns payload dict or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_auth.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/auth.py tests/test_auth.py
git commit -m "feat: add auth module with password hashing and JWT"
```

---

### Task 5: Auth API endpoints

**Files:**
- Modify: `orchestrator/router.py` (add `/api/auth/login`, `/api/auth/me`, `/api/auth/users` endpoints)
- Modify: `shared/config.py` (add admin seed config)

- [ ] **Step 1: Add admin seed config to shared/config.py**

Add to the `Settings` class, replacing the existing `web_auth_password` field:

```python
    # Auth — seed admin user on first boot
    admin_username: str = "admin"
    admin_password: str = ""  # Must be set for first boot
    jwt_secret: str = "auto-agent-jwt-secret-change-me"
```

- [ ] **Step 2: Read orchestrator/router.py to understand existing pattern**

```bash
head -60 /Users/alanyeginchibayev/Documents/Github/auto-agent/orchestrator/router.py
```

- [ ] **Step 3: Add auth endpoints to the router**

Add these endpoints to `orchestrator/router.py`. The exact insertion point depends on the existing structure (add after imports):

```python
from orchestrator.auth import create_token, hash_password, verify_password, verify_token
from shared.models import User
from shared.types import CreateUserRequest, LoginRequest, LoginResponse, UserData


@router.post("/auth/login")
async def login(req: LoginRequest):
    """Authenticate and return a JWT token."""
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.username == req.username)
        )
        user = result.scalar_one_or_none()
        if not user or not verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        user.last_login = datetime.now(timezone.utc)
        await session.commit()
        token = create_token(user_id=user.id, username=user.username)
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


@router.get("/auth/me")
async def get_me(authorization: str = Header(None)):
    """Return the current user from a JWT token."""
    payload = _verify_auth_header(authorization)
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.id == payload["user_id"])
        )
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


@router.post("/auth/users")
async def create_user(req: CreateUserRequest, authorization: str = Header(None)):
    """Create a new user. Requires authentication."""
    _verify_auth_header(authorization)
    async with async_session() as session:
        existing = await session.execute(
            select(User).where(User.username == req.username)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Username already exists")
        user = User(
            username=req.username,
            password_hash=hash_password(req.password),
            display_name=req.display_name,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return UserData(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            created_at=user.created_at.isoformat() if user.created_at else None,
        )


@router.get("/auth/users")
async def list_users(authorization: str = Header(None)):
    """List all users. Requires authentication."""
    _verify_auth_header(authorization)
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        users = result.scalars().all()
        return [
            UserData(
                id=u.id,
                username=u.username,
                display_name=u.display_name,
                created_at=u.created_at.isoformat() if u.created_at else None,
                last_login=u.last_login.isoformat() if u.last_login else None,
            )
            for u in users
        ]


def _verify_auth_header(authorization: str | None) -> dict:
    """Extract and verify JWT from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    payload = verify_token(authorization[7:])
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload
```

- [ ] **Step 4: Add admin seed on startup**

Add a startup function (in `orchestrator/router.py` or wherever the app startup is handled):

```python
async def seed_admin_user():
    """Create the admin user if no users exist."""
    async with async_session() as session:
        result = await session.execute(select(User).limit(1))
        if result.scalar_one_or_none() is None:
            if not settings.admin_password:
                log.warning("No admin_password set and no users exist — set ADMIN_PASSWORD env var")
                return
            admin = User(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                display_name=settings.admin_username.title(),
            )
            session.add(admin)
            await session.commit()
            log.info("admin_user_created", username=settings.admin_username)
```

- [ ] **Step 5: Commit**

```bash
git add orchestrator/router.py shared/config.py
git commit -m "feat: add auth API endpoints and admin seed"
```

---

### Task 6: WebSocket authentication and user attribution

**Files:**
- Modify: `web/main.py`

- [ ] **Step 1: Add token verification to WebSocket connect**

In `web/main.py`, modify the `websocket_endpoint` function. After `await ws.accept()`, extract the token from query params (WebSocket can't use cookies reliably across browsers, so the JS client will pass `?token=...`):

```python
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    # Authenticate
    token = ws.query_params.get("token")
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

    user_id = payload["user_id"]
    username = payload["username"]
    connected_clients.add(ws)

    # ... rest of handler unchanged, but pass user_id/username to handlers
```

- [ ] **Step 2: Add user attribution to broadcast messages**

Update `_handle_send_message`, `_handle_create_task`, and `_handle_send_guidance` (pass `data` with user info). For task creation, add `created_by_user_id` to the API call:

In `_handle_create_task`, add to the JSON payload:
```python
"created_by_user_id": user_id,
```

In `_handle_send_message`, change the broadcast:
```python
await broadcast({"type": "user", "message": text, "username": username})
```

In `_handle_send_guidance`, add username to the broadcast:
```python
await broadcast({
    "type": "guidance_sent",
    "task_id": task_id,
    "message": message,
    "username": username,
})
```

- [ ] **Step 3: Store connected client metadata**

Change `connected_clients` from `set[WebSocket]` to a dict so we can track user info:

```python
# Connected websocket clients: ws -> {"user_id": int, "username": str}
connected_clients: dict[WebSocket, dict] = {}
```

Update `broadcast` accordingly:
```python
async def broadcast(message: dict) -> None:
    dead: set[WebSocket] = set()
    for ws in connected_clients:
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    for ws in dead:
        connected_clients.pop(ws, None)
```

- [ ] **Step 4: Commit**

```bash
git add web/main.py
git commit -m "feat: add WebSocket auth and user attribution"
```

---

### Task 7: Memory read tool

**Files:**
- Create: `agent/tools/memory_read.py`
- Test: `tests/test_memory_read_tool.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_memory_read_tool.py`:

```python
"""Tests for the memory_read agent tool."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.memory_read import MemoryReadTool


@pytest.fixture
def tool():
    return MemoryReadTool()


@pytest.fixture
def ctx():
    return ToolContext(workspace="/tmp/test")


class TestMemoryReadToolDefinition:
    def test_name(self, tool):
        assert tool.name == "memory_read"

    def test_is_readonly(self, tool):
        assert tool.is_readonly is True

    def test_has_action_parameter(self, tool):
        assert "action" in tool.parameters["properties"]

    def test_actions_are_search_traverse_chain_roots(self, tool):
        actions = tool.parameters["properties"]["action"]["enum"]
        assert set(actions) == {"search", "traverse", "get_decision_chain", "list_roots"}


class TestMemoryReadSearch:
    @pytest.mark.asyncio
    async def test_search_returns_matching_nodes(self, tool, ctx):
        fake_node_id = str(uuid.uuid4())
        mock_nodes = [
            MagicMock(
                id=uuid.UUID(fake_node_id),
                name="python-tooling",
                node_type="preference",
                content="use uv, ruff, pytest",
                outgoing_edges=[],
                incoming_edges=[],
            )
        ]
        with patch("agent.tools.memory_read._search_nodes", new_callable=AsyncMock, return_value=mock_nodes):
            result = await tool.execute({"action": "search", "query": "python"}, ctx)
            assert "python-tooling" in result.output
            assert not result.is_error


class TestMemoryReadListRoots:
    @pytest.mark.asyncio
    async def test_list_roots_returns_nodes_without_incoming(self, tool, ctx):
        fake_id = str(uuid.uuid4())
        mock_nodes = [
            MagicMock(
                id=uuid.UUID(fake_id),
                name="company-standards",
                node_type="root",
                content="Top-level standards",
            )
        ]
        with patch("agent.tools.memory_read._list_root_nodes", new_callable=AsyncMock, return_value=mock_nodes):
            result = await tool.execute({"action": "list_roots"}, ctx)
            assert "company-standards" in result.output
            assert not result.is_error
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/test_memory_read_tool.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement memory_read tool**

Create `agent/tools/memory_read.py`:

```python
"""Memory read tool — search and traverse the shared graph memory."""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import async_session
from shared.models import MemoryEdge, MemoryNode

logger = structlog.get_logger()


async def _search_nodes(query: str, limit: int = 10) -> list[MemoryNode]:
    """Search nodes by name or content using ILIKE."""
    pattern = f"%{query}%"
    async with async_session() as session:
        result = await session.execute(
            select(MemoryNode)
            .where(or_(MemoryNode.name.ilike(pattern), MemoryNode.content.ilike(pattern)))
            .options(selectinload(MemoryNode.outgoing_edges), selectinload(MemoryNode.incoming_edges))
            .limit(limit)
        )
        return list(result.scalars().all())


async def _traverse_node(node_id: uuid.UUID, depth: int = 2) -> dict:
    """Traverse from a node, following edges up to N levels."""
    visited: set[uuid.UUID] = set()
    nodes: list[dict] = []
    edges: list[dict] = []

    async with async_session() as session:
        queue = [(node_id, 0)]
        while queue:
            current_id, current_depth = queue.pop(0)
            if current_id in visited or current_depth > depth:
                continue
            visited.add(current_id)

            result = await session.execute(
                select(MemoryNode)
                .where(MemoryNode.id == current_id)
                .options(selectinload(MemoryNode.outgoing_edges), selectinload(MemoryNode.incoming_edges))
            )
            node = result.scalar_one_or_none()
            if not node:
                continue

            nodes.append({
                "id": str(node.id),
                "name": node.name,
                "type": node.node_type,
                "content": node.content,
            })

            for edge in node.outgoing_edges:
                edges.append({
                    "source": str(edge.source_id),
                    "target": str(edge.target_id),
                    "relation": edge.relation,
                })
                if edge.target_id not in visited and current_depth + 1 <= depth:
                    queue.append((edge.target_id, current_depth + 1))

            for edge in node.incoming_edges:
                edges.append({
                    "source": str(edge.source_id),
                    "target": str(edge.target_id),
                    "relation": edge.relation,
                })
                if edge.source_id not in visited and current_depth + 1 <= depth:
                    queue.append((edge.source_id, current_depth + 1))

    return {"nodes": nodes, "edges": edges}


async def _get_decision_chain(node_id: uuid.UUID) -> list[dict]:
    """Follow evolved-from edges from newest to oldest."""
    chain: list[dict] = []
    visited: set[uuid.UUID] = set()
    current_id = node_id

    async with async_session() as session:
        while current_id and current_id not in visited:
            visited.add(current_id)
            result = await session.execute(
                select(MemoryNode)
                .where(MemoryNode.id == current_id)
                .options(selectinload(MemoryNode.outgoing_edges))
            )
            node = result.scalar_one_or_none()
            if not node:
                break

            chain.append({
                "id": str(node.id),
                "name": node.name,
                "type": node.node_type,
                "content": node.content,
                "created_at": node.created_at.isoformat() if node.created_at else None,
            })

            # Find the next node in the chain via evolved-from edge
            next_id = None
            for edge in node.outgoing_edges:
                if edge.relation == "evolved-from":
                    next_id = edge.target_id
                    break
            current_id = next_id

    return chain


async def _list_root_nodes() -> list[MemoryNode]:
    """Return all nodes with no incoming edges (root nodes)."""
    async with async_session() as session:
        # Subquery: all node IDs that are targets of an edge
        has_incoming = select(MemoryEdge.target_id).distinct()
        result = await session.execute(
            select(MemoryNode).where(MemoryNode.id.notin_(has_incoming))
        )
        return list(result.scalars().all())


class MemoryReadTool(Tool):
    """Search and traverse the shared graph memory."""

    name = "memory_read"
    description = (
        "Search and traverse the shared team graph memory. "
        "Use 'search' to find nodes by keyword, 'traverse' to explore connections, "
        "'get_decision_chain' to see decision history, 'list_roots' for top-level entries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "traverse", "get_decision_chain", "list_roots"],
                "description": "The read operation to perform.",
            },
            "query": {
                "type": "string",
                "description": "Search query (required for 'search' action).",
            },
            "node_id": {
                "type": "string",
                "description": "UUID of the node (required for 'traverse' and 'get_decision_chain').",
            },
            "depth": {
                "type": "integer",
                "description": "Traversal depth (default 2, for 'traverse' action).",
            },
        },
        "required": ["action"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        action = arguments["action"]

        try:
            if action == "search":
                query = arguments.get("query", "")
                if not query:
                    return ToolResult(output="Error: 'query' is required for search", is_error=True)
                nodes = await _search_nodes(query)
                if not nodes:
                    return ToolResult(output=f"No memory nodes found matching '{query}'")
                output_parts = []
                for node in nodes:
                    edges_str = ""
                    for e in node.outgoing_edges:
                        edges_str += f"\n  --[{e.relation}]--> {e.target_id}"
                    for e in node.incoming_edges:
                        edges_str += f"\n  <--[{e.relation}]-- {e.source_id}"
                    output_parts.append(
                        f"[{node.node_type}] {node.name} (id: {node.id})\n"
                        f"  Content: {node.content}{edges_str}"
                    )
                return ToolResult(output="\n\n".join(output_parts))

            elif action == "traverse":
                node_id_str = arguments.get("node_id", "")
                if not node_id_str:
                    return ToolResult(output="Error: 'node_id' is required for traverse", is_error=True)
                depth = arguments.get("depth", 2)
                result = await _traverse_node(uuid.UUID(node_id_str), depth)
                return ToolResult(output=json.dumps(result, indent=2))

            elif action == "get_decision_chain":
                node_id_str = arguments.get("node_id", "")
                if not node_id_str:
                    return ToolResult(output="Error: 'node_id' is required for get_decision_chain", is_error=True)
                chain = await _get_decision_chain(uuid.UUID(node_id_str))
                if not chain:
                    return ToolResult(output="No decision chain found for this node")
                output_parts = []
                for i, node in enumerate(chain):
                    prefix = "CURRENT" if i == 0 else f"v{len(chain) - i}"
                    output_parts.append(
                        f"[{prefix}] {node['name']} ({node['created_at']})\n  {node['content']}"
                    )
                return ToolResult(output="\n\n".join(output_parts))

            elif action == "list_roots":
                nodes = await _list_root_nodes()
                if not nodes:
                    return ToolResult(output="No root nodes in memory. The graph is empty.")
                output_parts = [
                    f"[{n.node_type}] {n.name} (id: {n.id})" for n in nodes
                ]
                return ToolResult(output="Root nodes:\n" + "\n".join(output_parts))

            else:
                return ToolResult(output=f"Unknown action: {action}", is_error=True)

        except Exception as e:
            logger.error("memory_read_error", action=action, error=str(e))
            return ToolResult(output=f"Error: {e}", is_error=True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_memory_read_tool.py -v
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add agent/tools/memory_read.py tests/test_memory_read_tool.py
git commit -m "feat: add memory_read tool for graph search and traversal"
```

---

### Task 8: Memory write tool

**Files:**
- Create: `agent/tools/memory_write.py`
- Test: `tests/test_memory_write_tool.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_memory_write_tool.py`:

```python
"""Tests for the memory_write agent tool."""

from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.memory_write import MemoryWriteTool


@pytest.fixture
def tool():
    return MemoryWriteTool()


@pytest.fixture
def ctx():
    return ToolContext(workspace="/tmp/test")


class TestMemoryWriteToolDefinition:
    def test_name(self, tool):
        assert tool.name == "memory_write"

    def test_is_not_readonly(self, tool):
        assert tool.is_readonly is False

    def test_has_action_parameter(self, tool):
        assert "action" in tool.parameters["properties"]

    def test_actions(self, tool):
        actions = tool.parameters["properties"]["action"]["enum"]
        assert set(actions) == {
            "create_node", "create_edge", "append_decision", "update_node", "delete_node"
        }


class TestMemoryWriteCreateNode:
    @pytest.mark.asyncio
    async def test_create_node_requires_name(self, tool, ctx):
        result = await tool.execute(
            {"action": "create_node", "node_type": "test", "content": "x"}, ctx
        )
        assert result.is_error
        assert "name" in result.output.lower()

    @pytest.mark.asyncio
    async def test_create_node_requires_node_type(self, tool, ctx):
        result = await tool.execute(
            {"action": "create_node", "name": "test", "content": "x"}, ctx
        )
        assert result.is_error
        assert "node_type" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/test_memory_write_tool.py -v
```

- [ ] **Step 3: Implement memory_write tool**

Create `agent/tools/memory_write.py`:

```python
"""Memory write tool — create, update, and delete nodes and edges in graph memory."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import async_session
from shared.models import MemoryEdge, MemoryNode

logger = structlog.get_logger()


class MemoryWriteTool(Tool):
    """Modify the shared graph memory — create nodes/edges, append decisions, update or delete."""

    name = "memory_write"
    description = (
        "Modify the shared team graph memory. "
        "Use 'create_node' to add knowledge, 'create_edge' to link nodes, "
        "'append_decision' to add to a decision chain (preserving history), "
        "'update_node' to amend content (corrections only), 'delete_node' to remove."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create_node", "create_edge", "append_decision", "update_node", "delete_node"],
                "description": "The write operation to perform.",
            },
            "name": {
                "type": "string",
                "description": "Node name (for create_node).",
            },
            "node_type": {
                "type": "string",
                "description": "Node type (for create_node), e.g. 'project', 'preference', 'decision', 'capability'.",
            },
            "content": {
                "type": "string",
                "description": "Node content (for create_node, append_decision, update_node).",
            },
            "node_id": {
                "type": "string",
                "description": "UUID of existing node (for update_node, delete_node, append_decision).",
            },
            "source_id": {
                "type": "string",
                "description": "Source node UUID (for create_edge).",
            },
            "target_id": {
                "type": "string",
                "description": "Target node UUID (for create_edge).",
            },
            "relation": {
                "type": "string",
                "description": "Edge relation label (for create_edge, append_decision). Default 'evolved-from' for append_decision.",
            },
            "task_id": {
                "type": "integer",
                "description": "Task ID that triggered this write (optional, for attribution).",
            },
        },
        "required": ["action"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        action = arguments["action"]
        task_id = arguments.get("task_id")

        try:
            if action == "create_node":
                return await self._create_node(arguments, task_id)
            elif action == "create_edge":
                return await self._create_edge(arguments)
            elif action == "append_decision":
                return await self._append_decision(arguments, task_id)
            elif action == "update_node":
                return await self._update_node(arguments)
            elif action == "delete_node":
                return await self._delete_node(arguments)
            else:
                return ToolResult(output=f"Unknown action: {action}", is_error=True)
        except Exception as e:
            logger.error("memory_write_error", action=action, error=str(e))
            return ToolResult(output=f"Error: {e}", is_error=True)

    async def _create_node(self, args: dict, task_id: int | None) -> ToolResult:
        name = args.get("name")
        node_type = args.get("node_type")
        content = args.get("content", "")
        if not name:
            return ToolResult(output="Error: 'name' is required for create_node", is_error=True)
        if not node_type:
            return ToolResult(output="Error: 'node_type' is required for create_node", is_error=True)

        node = MemoryNode(
            name=name,
            node_type=node_type,
            content=content,
            created_by_task_id=task_id,
        )
        async with async_session() as session:
            session.add(node)
            await session.commit()
            await session.refresh(node)
            return ToolResult(
                output=f"Created node '{name}' (type: {node_type}, id: {node.id})"
            )

    async def _create_edge(self, args: dict) -> ToolResult:
        source_id = args.get("source_id")
        target_id = args.get("target_id")
        relation = args.get("relation")
        if not all([source_id, target_id, relation]):
            return ToolResult(
                output="Error: 'source_id', 'target_id', and 'relation' are required for create_edge",
                is_error=True,
            )
        edge = MemoryEdge(
            source_id=uuid.UUID(source_id),
            target_id=uuid.UUID(target_id),
            relation=relation,
        )
        async with async_session() as session:
            session.add(edge)
            await session.commit()
            return ToolResult(output=f"Created edge: {source_id} --[{relation}]--> {target_id}")

    async def _append_decision(self, args: dict, task_id: int | None) -> ToolResult:
        parent_id = args.get("node_id")
        content = args.get("content", "")
        relation = args.get("relation", "evolved-from")
        if not parent_id:
            return ToolResult(output="Error: 'node_id' is required for append_decision", is_error=True)
        if not content:
            return ToolResult(output="Error: 'content' is required for append_decision", is_error=True)

        async with async_session() as session:
            # Verify parent exists
            result = await session.execute(
                select(MemoryNode).where(MemoryNode.id == uuid.UUID(parent_id))
            )
            parent = result.scalar_one_or_none()
            if not parent:
                return ToolResult(output=f"Error: node {parent_id} not found", is_error=True)

            # Create new decision node
            new_node = MemoryNode(
                name=parent.name,  # inherit name from parent
                node_type="decision",
                content=content,
                created_by_task_id=task_id,
            )
            session.add(new_node)
            await session.flush()  # get the ID

            # Link new -> parent via relation
            edge = MemoryEdge(
                source_id=new_node.id,
                target_id=uuid.UUID(parent_id),
                relation=relation,
            )
            session.add(edge)
            await session.commit()

            return ToolResult(
                output=f"Appended decision to '{parent.name}' chain (new id: {new_node.id})"
            )

    async def _update_node(self, args: dict) -> ToolResult:
        node_id = args.get("node_id")
        content = args.get("content")
        if not node_id:
            return ToolResult(output="Error: 'node_id' is required for update_node", is_error=True)
        if not content:
            return ToolResult(output="Error: 'content' is required for update_node", is_error=True)

        async with async_session() as session:
            result = await session.execute(
                select(MemoryNode).where(MemoryNode.id == uuid.UUID(node_id))
            )
            node = result.scalar_one_or_none()
            if not node:
                return ToolResult(output=f"Error: node {node_id} not found", is_error=True)
            node.content = content
            await session.commit()
            return ToolResult(output=f"Updated node '{node.name}' content")

    async def _delete_node(self, args: dict) -> ToolResult:
        node_id = args.get("node_id")
        if not node_id:
            return ToolResult(output="Error: 'node_id' is required for delete_node", is_error=True)

        uid = uuid.UUID(node_id)
        async with async_session() as session:
            result = await session.execute(
                select(MemoryNode).where(MemoryNode.id == uid)
            )
            node = result.scalar_one_or_none()
            if not node:
                return ToolResult(output=f"Error: node {node_id} not found", is_error=True)
            name = node.name
            # Edges cascade-delete via FK, but explicit for clarity
            await session.execute(
                delete(MemoryEdge).where(
                    (MemoryEdge.source_id == uid) | (MemoryEdge.target_id == uid)
                )
            )
            await session.delete(node)
            await session.commit()
            return ToolResult(output=f"Deleted node '{name}' and its edges")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/test_memory_write_tool.py -v
```

- [ ] **Step 5: Commit**

```bash
git add agent/tools/memory_write.py tests/test_memory_write_tool.py
git commit -m "feat: add memory_write tool for graph mutations"
```

---

### Task 9: Register memory tools in tool registry

**Files:**
- Modify: `agent/tools/__init__.py`

- [ ] **Step 1: Add imports and register**

In `agent/tools/__init__.py`, add:

```python
from agent.tools.memory_read import MemoryReadTool
from agent.tools.memory_write import MemoryWriteTool
```

In `create_default_registry`, add after the read-only tools:

```python
    # Memory tools — always available (read is readonly, write needs write mode)
    registry.register(MemoryReadTool())

    if not readonly:
        registry.register(MemoryWriteTool())
```

- [ ] **Step 2: Verify imports work**

```bash
.venv/bin/python3 -c "from agent.tools import create_default_registry; r = create_default_registry(); print(r.names())"
```

Expected: list includes `memory_read` and `memory_write`.

- [ ] **Step 3: Commit**

```bash
git add agent/tools/__init__.py
git commit -m "feat: register memory_read and memory_write tools"
```

---

### Task 10: Memory context injection at task start

**Files:**
- Create: `agent/context/memory.py`
- Modify: `agent/context/system.py`
- Test: `tests/test_memory_context.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_memory_context.py`:

```python
"""Tests for memory context injection."""

from unittest.mock import AsyncMock, patch

import pytest

from agent.context.memory import query_relevant_memory


class TestQueryRelevantMemory:
    @pytest.mark.asyncio
    async def test_returns_empty_for_no_matches(self):
        with patch("agent.context.memory._search_nodes", new_callable=AsyncMock, return_value=[]):
            result = await query_relevant_memory("build a todo app")
            assert result == ""

    @pytest.mark.asyncio
    async def test_returns_formatted_context_for_matches(self):
        import uuid
        from unittest.mock import MagicMock

        node = MagicMock()
        node.id = uuid.uuid4()
        node.name = "frontend-stack"
        node.node_type = "preference"
        node.content = "use nextjs, tailwind"
        node.outgoing_edges = []
        node.incoming_edges = []

        with patch("agent.context.memory._search_nodes", new_callable=AsyncMock, return_value=[node]):
            result = await query_relevant_memory("build a frontend")
            assert "frontend-stack" in result
            assert "nextjs" in result
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python3 -m pytest tests/test_memory_context.py -v
```

- [ ] **Step 3: Implement memory context module**

Create `agent/context/memory.py`:

```python
"""Graph memory context — queries relevant memory for task injection."""

from __future__ import annotations

import structlog

from agent.tools.memory_read import _list_root_nodes, _search_nodes

logger = structlog.get_logger()


async def query_relevant_memory(task_description: str) -> str:
    """Query graph memory for context relevant to a task description.

    Extracts keywords from the description, searches the graph, and
    formats matching nodes into a context string for the system prompt.

    Returns empty string if no relevant memory found.
    """
    if not task_description:
        return ""

    # Extract meaningful keywords (skip common words)
    stop_words = {
        "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "must", "to", "of",
        "in", "for", "on", "with", "at", "by", "from", "as", "into", "about",
        "it", "its", "this", "that", "and", "or", "but", "not", "no", "so",
        "if", "then", "than", "when", "what", "which", "who", "how", "all",
        "each", "every", "both", "few", "more", "most", "some", "any", "i",
        "me", "my", "we", "our", "you", "your", "he", "she", "they", "them",
        "please", "create", "build", "make", "add", "fix", "update", "change",
        "implement", "write", "new", "use", "using",
    }

    words = task_description.lower().split()
    keywords = [w.strip(".,!?;:'\"()[]{}") for w in words if w.lower().strip(".,!?;:'\"()[]{}") not in stop_words and len(w) > 2]

    if not keywords:
        return ""

    # Search for each keyword and deduplicate
    seen_ids = set()
    matched_nodes = []

    for keyword in keywords[:5]:  # Cap at 5 keywords to avoid too many queries
        try:
            nodes = await _search_nodes(keyword, limit=3)
            for node in nodes:
                if node.id not in seen_ids:
                    seen_ids.add(node.id)
                    matched_nodes.append(node)
        except Exception as e:
            logger.warning("memory_search_failed", keyword=keyword, error=str(e))

    if not matched_nodes:
        return ""

    # Format into context block
    parts = ["## Shared Team Memory (relevant to this task)\n"]
    for node in matched_nodes[:10]:  # Cap context size
        edges_info = ""
        for e in getattr(node, "outgoing_edges", []):
            edges_info += f"\n    -> [{e.relation}] {e.target_id}"
        for e in getattr(node, "incoming_edges", []):
            edges_info += f"\n    <- [{e.relation}] {e.source_id}"

        parts.append(
            f"- **[{node.node_type}] {node.name}** (id: {node.id})\n"
            f"  {node.content}{edges_info}"
        )

    return "\n".join(parts)
```

- [ ] **Step 4: Modify SystemPromptBuilder to inject memory**

In `agent/context/system.py`, modify the `build` method to accept and inject memory context. Add parameter `memory_context: str | None = None` and add this block after the repo summary section:

```python
        # Graph memory (team knowledge relevant to this task)
        if memory_context:
            parts.append(memory_context)
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python3 -m pytest tests/test_memory_context.py -v
```

- [ ] **Step 6: Commit**

```bash
git add agent/context/memory.py agent/context/system.py tests/test_memory_context.py
git commit -m "feat: add memory context injection into system prompt"
```

---

### Task 11: Post-task memory reflection

**Files:**
- Modify: `agent/main.py`
- Modify: `agent/prompts.py`

- [ ] **Step 1: Read the current task completion flow in agent/main.py**

Look for where tasks are completed (status changes to DONE, PR created, etc.) to find the insertion point for the reflection step.

```bash
grep -n "DONE\|code_complete\|task.*complete\|post.*task" /Users/alanyeginchibayev/Documents/Github/auto-agent/agent/main.py
```

- [ ] **Step 2: Add reflection prompt to agent/prompts.py**

Add to `agent/prompts.py`:

```python
MEMORY_REFLECTION_PROMPT = """\
Task is complete. Before finishing, reflect on what was learned:

1. Were any architectural or tooling DECISIONS made? (e.g., chose library X over Y, adopted pattern Z)
2. Were any new CAPABILITIES created? (e.g., this project now produces/exposes X)
3. Were any existing team PREFERENCES applied or discovered? (e.g., the team prefers X approach)

For each item:
- Use memory_read to check if related knowledge already exists in the graph
- Use memory_write to record new decisions (append_decision for evolving existing ones, create_node + create_edge for new knowledge)

If nothing notable was learned, that's fine — skip writing.

Keep node names descriptive and consistent with existing graph vocabulary.
"""
```

- [ ] **Step 3: Inject reflection prompt at task completion**

In `agent/main.py`, at the point where a task completes successfully (after coding is done, before status is set to DONE), add the reflection prompt as an additional user message in the conversation before the final agent turn. The exact insertion depends on the current flow — look for where `task.code_complete` or the transition to DONE happens.

Add the reflection as a final agent loop turn:

```python
# Post-task memory reflection
from agent.prompts import MEMORY_REFLECTION_PROMPT
# Run one more turn with reflection prompt
await loop.run_single_turn(MEMORY_REFLECTION_PROMPT)
```

- [ ] **Step 4: Commit**

```bash
git add agent/main.py agent/prompts.py
git commit -m "feat: add post-task memory reflection for graph learning"
```

---

### Task 12: Wire memory context into task start

**Files:**
- Modify: `agent/main.py`
- Modify: `agent/context/__init__.py`

- [ ] **Step 1: Add memory query to ContextManager**

In `agent/context/__init__.py`, add:

```python
from agent.context.memory import query_relevant_memory
```

Add a method to `ContextManager`:

```python
    async def build_system_prompt(
        self,
        repo_summary: str | None = None,
        extra_instructions: str | None = None,
        include_methodology: bool = False,
        task_description: str | None = None,
    ) -> str:
        """Build the full system prompt for this workspace."""
        memory_context = None
        if task_description:
            try:
                memory_context = await query_relevant_memory(task_description)
            except Exception:
                logger.warning("memory_context_query_failed")

        return await self.system.build(
            self._workspace,
            repo_summary=repo_summary,
            extra_instructions=extra_instructions,
            include_methodology=include_methodology,
            memory_context=memory_context,
        )
```

- [ ] **Step 2: Pass task description when building system prompt**

In `agent/main.py`, find where `context_manager.build_system_prompt()` is called and add `task_description=task.description` (or `task_description=task_data.description`) to the call.

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```

Expected: All existing tests pass, no regressions.

- [ ] **Step 4: Commit**

```bash
git add agent/context/__init__.py agent/main.py
git commit -m "feat: inject relevant graph memory into agent system prompt"
```

---

### Task 13: Login UI and user attribution in web frontend

**Files:**
- Modify: `web/static/index.html`

- [ ] **Step 1: Add login screen**

Add a login overlay/modal to `index.html` that:
- Shows username and password fields
- On submit, POSTs to `/api/auth/login`
- On success, stores the JWT token in localStorage
- Opens the WebSocket with `?token=<jwt>` query parameter
- Hides the login screen and shows the main UI

```javascript
// Login handler
async function handleLogin(e) {
    e.preventDefault();
    const username = document.getElementById('login-username').value;
    const password = document.getElementById('login-password').value;
    
    const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({username, password}),
    });
    
    if (resp.ok) {
        const data = await resp.json();
        localStorage.setItem('auth_token', data.token);
        localStorage.setItem('current_user', JSON.stringify(data.user));
        document.getElementById('login-screen').style.display = 'none';
        document.getElementById('main-app').style.display = '';
        connectWebSocket(data.token);
    } else {
        document.getElementById('login-error').textContent = 'Invalid credentials';
    }
}
```

- [ ] **Step 2: Update WebSocket connection to include token**

```javascript
function connectWebSocket(token) {
    const wsUrl = `ws://${window.location.host}/ws?token=${token}`;
    ws = new WebSocket(wsUrl);
    // ... existing handlers
}
```

- [ ] **Step 3: Add user display names to messages**

When rendering chat messages, show the username:

```javascript
// For user messages
function renderUserMessage(data) {
    const username = data.username || 'Unknown';
    // Show: "Alice: message text"
    addMessage(`<strong>${username}:</strong> ${data.message}`, 'user-message');
}

// For guidance messages
function renderGuidance(data) {
    const username = data.username || 'Unknown';
    addMessage(`<strong>${username}</strong> (guidance): ${data.message}`, 'guidance-message');
}
```

- [ ] **Step 4: Add user management section**

Add a simple settings area (accessible via a gear icon or "Users" tab) with:
- Current user display
- "Add User" form (username, display name, password)
- List of existing users

- [ ] **Step 5: On page load, check for existing token**

```javascript
window.addEventListener('load', () => {
    const token = localStorage.getItem('auth_token');
    if (token) {
        // Verify token is still valid
        fetch('/api/auth/me', {headers: {'Authorization': `Bearer ${token}`}})
            .then(r => r.ok ? r.json() : Promise.reject())
            .then(user => {
                localStorage.setItem('current_user', JSON.stringify(user));
                document.getElementById('login-screen').style.display = 'none';
                document.getElementById('main-app').style.display = '';
                connectWebSocket(token);
            })
            .catch(() => {
                localStorage.removeItem('auth_token');
                document.getElementById('login-screen').style.display = '';
            });
    } else {
        document.getElementById('login-screen').style.display = '';
    }
});
```

- [ ] **Step 6: Commit**

```bash
git add web/static/index.html
git commit -m "feat: add login screen and user attribution to web UI"
```

---

### Task 14: Update orchestrator to handle created_by_user_id

**Files:**
- Modify: `orchestrator/router.py`
- Modify: `shared/types.py`

- [ ] **Step 1: Add created_by_user_id to task creation endpoint**

In the task creation endpoint in `orchestrator/router.py`, accept `created_by_user_id` in the request body and pass it to the Task model.

- [ ] **Step 2: Add created_by_user_id to TaskData**

In `shared/types.py`, add to `TaskData`:

```python
    created_by_user_id: int | None = None
    created_by_username: str | None = None
```

- [ ] **Step 3: Include user info in task serialization**

When serializing tasks for the API, include the user's display name if `created_by_user_id` is set.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/router.py shared/types.py
git commit -m "feat: add user attribution to task creation and serialization"
```

---

### Task 15: Final integration — remove old HTTP Basic auth, run full tests

**Files:**
- Modify: `shared/config.py` (remove `web_auth_password`)
- Modify: `web/main.py` (remove any HTTP Basic auth middleware if present)

- [ ] **Step 1: Remove old auth config**

Remove `web_auth_password` from `Settings` in `shared/config.py` (keep the new `admin_username`, `admin_password`, `jwt_secret` fields).

- [ ] **Step 2: Remove HTTP Basic auth from web**

Search for and remove any HTTP Basic auth middleware in `web/main.py` or related files.

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/python3 -m pytest tests/ -q
```

- [ ] **Step 4: Run linter**

```bash
ruff check .
ruff format --check .
```

Fix any issues.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: remove legacy HTTP Basic auth, finalize collaboration setup"
```

---

### Task 16: Create PR

- [ ] **Step 1: Push branch and create PR**

```bash
git push -u origin feature/collaboration-graph-memory
gh pr create --title "feat: multi-user collaboration with shared graph memory" --body "$(cat <<'EOF'
## Summary

- Adds per-user authentication (username/password with JWT sessions)
- Adds shared graph memory (MemoryNode + MemoryEdge in Postgres) for cross-project knowledge
- Agent writes decisions, capabilities, and preferences to the graph during task completion
- Relevant memory is automatically injected into the system prompt at task start
- All users share full access to tasks, messages, and memory
- WebSocket connections authenticated with user attribution on all messages

## Test plan

- [ ] Run full test suite: `.venv/bin/python3 -m pytest tests/ -q`
- [ ] Run linter: `ruff check . && ruff format --check .`
- [ ] Run migration against test DB
- [ ] Verify login flow in web UI
- [ ] Create a second user and verify shared visibility
- [ ] Run a task and verify memory reflection writes nodes
- [ ] Start a new task and verify relevant memory appears in context

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
