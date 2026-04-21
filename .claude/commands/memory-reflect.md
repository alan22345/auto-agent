# Memory Reflection

Reflect on what was learned during the task you just completed.

## Instructions

1. Use the `memory_search` tool to check if related knowledge already exists in the graph.
2. Consider what was learned:
   - Were any architectural or tooling **decisions** made? (e.g., chose library X over Y, adopted pattern Z)
   - Were any new **capabilities** created? (e.g., this project now produces/exposes X)
   - Were any existing team **preferences** applied or discovered? (e.g., the team prefers X approach)
3. For each item worth recording:
   - Search first to avoid duplicates
   - Use `memory_create_node` to record new knowledge (types: decision, capability, preference, project)
   - Use `memory_create_edge` to link related nodes
   - Use `memory_append_decision` if updating an existing decision (preserves history)

If nothing notable was learned, say so and stop. Don't create noise in the graph.

Keep node names descriptive and consistent with existing graph vocabulary.
