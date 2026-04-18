"""Graph memory context — queries relevant memory for task injection."""

from __future__ import annotations

import structlog

from agent.tools.memory_read import _list_root_nodes, _search_nodes

logger = structlog.get_logger()

# Common words to skip when extracting search keywords
_STOP_WORDS = {
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


async def query_relevant_memory(task_description: str) -> str:
    """Query graph memory for context relevant to a task description.

    Returns formatted context string or empty string if nothing found.
    """
    if not task_description:
        return ""

    words = task_description.lower().split()
    keywords = [
        w.strip(".,!?;:'\"()[]{}") for w in words
        if w.lower().strip(".,!?;:'\"()[]{}") not in _STOP_WORDS and len(w) > 2
    ]

    if not keywords:
        return ""

    seen_ids = set()
    matched_nodes = []

    for keyword in keywords[:5]:
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

    parts = ["## Shared Team Memory (relevant to this task)\n"]
    for node in matched_nodes[:10]:
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
