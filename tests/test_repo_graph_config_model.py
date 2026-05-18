"""Schema-level tests for the ADR-016 code-graph models.

These run without a database — they inspect SQLAlchemy metadata so they
catch column/FK drift in CI even on machines without Postgres.

A separate migration round-trip test (``test_repo_graph_migration_033``) takes
the same DB the existing ``test_migration_029`` suite uses.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect

from shared.models import Base, Repo, RepoGraph, RepoGraphConfig


class TestRepoGraphConfigSchema:
    def test_table_name(self) -> None:
        assert RepoGraphConfig.__tablename__ == "repo_graph_configs"

    def test_repo_id_is_primary_key(self) -> None:
        pk = inspect(RepoGraphConfig).primary_key
        assert [c.name for c in pk] == ["repo_id"]

    def test_repo_id_fk_cascades(self) -> None:
        fks = [fk for fk in RepoGraphConfig.__table__.foreign_keys if fk.parent.name == "repo_id"]
        assert len(fks) == 1
        assert fks[0].column.table.name == Repo.__tablename__
        assert fks[0].ondelete == "CASCADE"

    def test_last_analysis_id_fk_set_null(self) -> None:
        fks = [
            fk
            for fk in RepoGraphConfig.__table__.foreign_keys
            if fk.parent.name == "last_analysis_id"
        ]
        assert len(fks) == 1
        assert fks[0].column.table.name == "repo_graphs"
        assert fks[0].ondelete == "SET NULL"

    def test_analysis_branch_required(self) -> None:
        col = RepoGraphConfig.__table__.columns["analysis_branch"]
        assert col.nullable is False

    def test_workspace_path_required(self) -> None:
        col = RepoGraphConfig.__table__.columns["workspace_path"]
        assert col.nullable is False

    def test_analyser_version_defaults_empty(self) -> None:
        col = RepoGraphConfig.__table__.columns["analyser_version"]
        assert col.nullable is False
        # server_default is a DefaultClause; its value lives on .arg
        assert col.server_default is not None


class TestRepoGraphSchema:
    def test_table_name(self) -> None:
        assert RepoGraph.__tablename__ == "repo_graphs"

    def test_repo_id_fk_cascades(self) -> None:
        fks = [fk for fk in RepoGraph.__table__.foreign_keys if fk.parent.name == "repo_id"]
        assert len(fks) == 1
        assert fks[0].column.table.name == Repo.__tablename__
        assert fks[0].ondelete == "CASCADE"

    def test_graph_json_required(self) -> None:
        col = RepoGraph.__table__.columns["graph_json"]
        assert col.nullable is False

    def test_status_defaults_ok(self) -> None:
        col = RepoGraph.__table__.columns["status"]
        assert col.nullable is False
        # server_default is set to literal "ok" — keeps Phase-2 inserts simple.
        assert col.server_default is not None


# ---------------------------------------------------------------------------
# Round-trip against a real DB — skipped if DATABASE_URL is absent.
# ---------------------------------------------------------------------------


def _skip_if_no_db() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs a running Postgres (set DATABASE_URL)")


@pytest.mark.asyncio
async def test_repo_graph_config_round_trip() -> None:
    _skip_if_no_db()

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from shared.models import Organization

    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with factory() as session:
        await session.begin()
        try:
            # Create a real Organization + Repo so the FKs resolve.
            org = Organization(name=f"graph-test-org-{os.getpid()}")
            session.add(org)
            await session.flush()

            repo = Repo(
                name=f"graph-test-repo-{os.getpid()}",
                url="https://github.com/example/x",
                default_branch="main",
                organization_id=org.id,
            )
            session.add(repo)
            await session.flush()

            cfg = RepoGraphConfig(
                repo_id=repo.id,
                organization_id=org.id,
                analysis_branch="main",
                workspace_path=f"/tmp/graph-workspaces/{repo.id}",
            )
            session.add(cfg)
            await session.flush()

            result = await session.execute(
                sa.select(RepoGraphConfig).where(RepoGraphConfig.repo_id == repo.id)
            )
            fetched = result.scalar_one()
            assert fetched.analysis_branch == "main"
            assert fetched.last_analysis_id is None
            assert fetched.analyser_version == ""
            assert fetched.workspace_path.endswith(str(repo.id))
        finally:
            await session.rollback()
            await session.close()
    await engine.dispose()


def test_metadata_includes_new_tables() -> None:
    """Sanity-check that Base.metadata sees both new tables — guards against
    a forgotten import in production startup paths."""
    table_names = set(Base.metadata.tables.keys())
    assert "repo_graph_configs" in table_names
    assert "repo_graphs" in table_names
