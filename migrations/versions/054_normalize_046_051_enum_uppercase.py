"""Add uppercase variants for the enum values added in migrations 046 and 051.

Migration 052 documented the long-standing case-mismatch issue: SQLAlchemy
:class:`Enum` columns serialise Python enum *names* (UPPERCASE) at bind time,
but several early migrations added enum values in lowercase. 052 normalised
the original lowercase set but explicitly scoped itself to migration 001's
values, on the assumption that later migrations had added uppercase variants
by their respective migrations.

That assumption was wrong for two follow-ups:

* **046_scaffold_complexity_and_states** added ``'scaffold'`` to
  ``taskcomplexity`` and 10 statuses (``awaiting_intent_grill``,
  ``building_root_adr``, ``awaiting_root_adr_approval``,
  ``building_domain_adrs``, ``awaiting_domain_adr_approval``,
  ``dispatching_domain_builds``, ``building_domains``,
  ``awaiting_final_verification``, ``awaiting_domain_grill``) to
  ``taskstatus`` — all lowercase only. Visible failure: POST
  /api/freeform/create-repo crashes with::

      invalid input value for enum taskcomplexity: "SCAFFOLD"

  because ``orchestrator/create_repo.py`` inserts a task with
  ``complexity=TaskComplexity.SCAFFOLD`` and SQLAlchemy binds the name
  ``SCAFFOLD`` — which the enum doesn't accept.

* **051_awaiting_required_secrets** added ``'awaiting_required_secrets'`` to
  ``taskstatus`` lowercase only. Same latent bug — every transition into
  ``TaskStatus.AWAITING_REQUIRED_SECRETS`` would fail the same way once it
  ran in practice.

This migration adds the missing UPPERCASE variants using
``ADD VALUE IF NOT EXISTS`` so it is safe to re-run on stacks that already
applied an out-of-band fix. Lowercase values are left in place — Postgres
has no portable ``DROP VALUE`` and the lowercase values support any raw-SQL
paths that already wrote them.

Revision ID: 054
Revises: 053
Create Date: 2026-05-22
"""

from __future__ import annotations

from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


_TO_ADD: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("taskcomplexity", ("SCAFFOLD",)),
    (
        "taskstatus",
        (
            # From migration 046
            "AWAITING_INTENT_GRILL",
            "BUILDING_ROOT_ADR",
            "AWAITING_ROOT_ADR_APPROVAL",
            "BUILDING_DOMAIN_ADRS",
            "AWAITING_DOMAIN_ADR_APPROVAL",
            "DISPATCHING_DOMAIN_BUILDS",
            "BUILDING_DOMAINS",
            "AWAITING_FINAL_VERIFICATION",
            "AWAITING_DOMAIN_GRILL",
            # From migration 051
            "AWAITING_REQUIRED_SECRETS",
        ),
    ),
)


def upgrade() -> None:
    for enum_name, values in _TO_ADD:
        for value in values:
            op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE. Same posture as 052.
    pass
