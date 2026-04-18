"""Tests for User, MemoryNode, MemoryEdge ORM models and Pydantic types."""

from shared.models import MemoryEdge, MemoryNode, User
from shared.types import (
    CreateUserRequest,
    LoginRequest,
    LoginResponse,
    MemoryEdgeData,
    MemoryNodeData,
    MemoryNodeWithEdges,
    UserData,
)


class TestUserModel:
    def test_user_has_required_fields(self):
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


class TestPydanticTypes:
    def test_user_data(self):
        data = UserData(id=1, username="alice", display_name="Alice")
        assert data.username == "alice"

    def test_login_request(self):
        req = LoginRequest(username="alice", password="secret")
        assert req.username == "alice"

    def test_memory_node_data(self):
        node = MemoryNodeData(id="abc", name="test", node_type="preference")
        assert node.name == "test"

    def test_memory_edge_data(self):
        edge = MemoryEdgeData(id="abc", source_id="s", target_id="t", relation="rel")
        assert edge.relation == "rel"

    def test_memory_node_with_edges(self):
        node = MemoryNodeData(id="abc", name="test", node_type="pref")
        result = MemoryNodeWithEdges(node=node, edges=[])
        assert result.node.name == "test"
        assert result.edges == []
