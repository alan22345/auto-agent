"""Linear helpers — post comments back to Linear issues.

Incoming webhooks are handled by the orchestrator's webhook endpoint.
This module provides outbound helpers only.
"""

from __future__ import annotations

import httpx

from shared.config import settings

LINEAR_API = "https://api.linear.app/graphql"


async def update_linear_status(issue_id: str, comment: str) -> None:
    """Post a comment on a Linear issue."""
    mutation = """
    mutation($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
      }
    }
    """
    async with httpx.AsyncClient() as client:
        await client.post(
            LINEAR_API,
            headers={
                "Authorization": settings.linear_api_key,
                "Content-Type": "application/json",
            },
            json={"query": mutation, "variables": {"issueId": issue_id, "body": comment}},
        )
