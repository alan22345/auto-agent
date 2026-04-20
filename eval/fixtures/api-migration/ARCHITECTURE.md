# Architecture Notes

## Callers of APIClient
- `service.py` — UserService (sync caller, high priority)
- Several CLI scripts (not in repo) also use APIClient synchronously
- A new async web handler is planned but not yet written

## Constraints
- The sync interface MUST continue to work for existing callers
- New code should use async where possible
- `get_user_with_posts` is a known bottleneck (2 sequential HTTP calls)
- We use stdlib only (no aiohttp, no httpx) unless there's a strong reason
