"""HTTP API client — wraps calls to an external service.

This client uses synchronous requests. Multiple callers depend on this
interface. The codebase is migrating to async but not all callers have
migrated yet.
"""

import json
import urllib.request
import urllib.error
from dataclasses import dataclass


BASE_URL = "https://jsonplaceholder.typicode.com"


@dataclass
class User:
    id: int
    name: str
    email: str
    username: str


@dataclass
class Post:
    id: int
    user_id: int
    title: str
    body: str


class APIClient:
    """Synchronous HTTP client for the external API."""

    def __init__(self, base_url: str = BASE_URL, timeout: int = 10):
        self.base_url = base_url
        self.timeout = timeout

    def get_user(self, user_id: int) -> User:
        """Fetch a single user by ID."""
        data = self._get(f"/users/{user_id}")
        return User(
            id=data["id"],
            name=data["name"],
            email=data["email"],
            username=data["username"],
        )

    def list_users(self) -> list[User]:
        """Fetch all users."""
        data = self._get("/users")
        return [
            User(id=u["id"], name=u["name"], email=u["email"], username=u["username"])
            for u in data
        ]

    def get_posts_by_user(self, user_id: int) -> list[Post]:
        """Fetch all posts by a specific user."""
        data = self._get(f"/posts?userId={user_id}")
        return [
            Post(id=p["id"], user_id=p["userId"], title=p["title"], body=p["body"])
            for p in data
        ]

    def get_user_with_posts(self, user_id: int) -> tuple[User, list[Post]]:
        """Fetch a user and all their posts.

        Currently makes 2 sequential HTTP calls. Could be parallelized.
        """
        user = self.get_user(user_id)
        posts = self.get_posts_by_user(user_id)
        return user, posts

    def _get(self, path: str) -> dict | list:
        """Make a GET request and parse JSON response."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise APIError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise APIError(f"Connection failed: {e.reason}") from e


class APIError(Exception):
    """Raised when an API call fails."""
    pass
