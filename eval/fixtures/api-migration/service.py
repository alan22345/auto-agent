"""Business logic layer — uses the API client.

This is one of several callers of APIClient. It uses the synchronous
interface directly. Any migration must consider that this file and
others like it need to keep working.
"""

from client import APIClient, User, Post


class UserService:
    """High-level service for user-related operations."""

    def __init__(self, client: APIClient | None = None):
        self.client = client or APIClient()

    def get_user_summary(self, user_id: int) -> dict:
        """Get a summary of a user including post count."""
        user, posts = self.client.get_user_with_posts(user_id)
        return {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "post_count": len(posts),
            "latest_post": posts[0].title if posts else None,
        }

    def find_active_users(self) -> list[dict]:
        """Find users who have at least 5 posts."""
        users = self.client.list_users()
        active = []
        for user in users:
            posts = self.client.get_posts_by_user(user.id)
            if len(posts) >= 5:
                active.append({
                    "id": user.id,
                    "name": user.name,
                    "post_count": len(posts),
                })
        return active

    def get_user_feed(self, user_id: int, limit: int = 5) -> list[dict]:
        """Get a user's recent posts as a feed."""
        posts = self.client.get_posts_by_user(user_id)
        return [
            {"title": p.title, "body": p.body[:100]}
            for p in posts[:limit]
        ]
