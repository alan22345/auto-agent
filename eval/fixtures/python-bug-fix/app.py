"""Simple web API with a bug in the pagination logic."""

from dataclasses import dataclass


@dataclass
class User:
    id: int
    name: str
    email: str


# In-memory database
USERS = [User(id=i, name=f"User {i}", email=f"user{i}@example.com") for i in range(1, 51)]


def get_users(page: int = 1, per_page: int = 10) -> dict:
    """Return paginated users. BUG: off-by-one error in pagination."""
    start = page * per_page  # BUG: should be (page - 1) * per_page
    end = start + per_page
    users = USERS[start:end]

    return {
        "users": [{"id": u.id, "name": u.name, "email": u.email} for u in users],
        "page": page,
        "per_page": per_page,
        "total": len(USERS),
        "total_pages": len(USERS) // per_page,  # BUG: doesn't handle remainder
    }


def search_users(query: str) -> list[dict]:
    """Search users by name. BUG: case-sensitive comparison."""
    results = []
    for user in USERS:
        if query in user.name:  # BUG: should be case-insensitive
            results.append({"id": user.id, "name": user.name, "email": user.email})
    return results
