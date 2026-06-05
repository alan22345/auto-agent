"""Clone detection via suffix array over per-function token streams (ADR-016 Phase 11 §2).

Public API
----------
normalize_tokens(tokens, mode) -> list[str]
    Convert a raw ``(token_type, token_text)`` stream to a list of normalised
    token strings ready for the suffix-array pass.

compute_clones(tokens_by_node, nodes_by_id, *, min_tokens, mode) -> list[CloneGroup]
    Detect duplicate code blocks across all function nodes using a suffix
    array built over the combined (sentinel-separated) token sequence.

Algorithm
---------
1. Normalise each function's token stream according to ``mode``.
2. Build a combined integer sequence by mapping each distinct normalised
   token to a unique positive integer, concatenating all functions' int
   sequences, and inserting a *unique negative sentinel* between (and
   after) each function.  Unique sentinels prevent the SA from matching
   across function boundaries — if a sentinel fell in the middle of a
   hypothetical match the LCP value would be forced to zero at that point.
3. Build a suffix array (SA) over the combined sequence using prefix-
   doubling (O(n log² n)) and Kasai's algorithm for the LCP array.
4. Slide a window over the SA+LCP to collect maximal repeated substrings
   of length >= ``min_tokens`` owned by >= 2 distinct functions.
5. Assemble :class:`~shared.types.CloneGroup` objects, dedup by node-id
   set (keep largest token_len), compute family ids, and return sorted by
   stable id.

Known v1 limitations
--------------------
a) **Function-level granularity** — clones are reported at the containing
   function's boundaries (``line_start``/``line_end`` from the Node),
   not at the exact duplicated sub-block line range.  Fine-grained
   sub-block line extraction is deferred.
b) **Intra-function self-clones deferred** — a repeated subsequence that
   appears twice within the same function is not reported.  Only repeats
   spanning >= 2 distinct function nodes are emitted.
c) **Single mode per run** — ``compute_clones`` accepts one ``mode``
   value; callers wanting multi-mode results must call it once per mode.
   The pipeline uses the default ``"mild"`` mode.
d) **min_tokens default 50** — very short functions are unlikely to
   produce meaningful clone groups; the threshold filters out noise at the
   cost of missing micro-patterns.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from shared.types import CloneGroup, CloneInstance

if TYPE_CHECKING:
    from shared.types import Node

# ---------------------------------------------------------------------------
# Token normalisation
# ---------------------------------------------------------------------------

#: Token types considered numeric literals across Python + TypeScript grammars.
_NUMERIC_TYPES: frozenset[str] = frozenset({"integer", "float", "number"})

#: Token types considered string literals.
_STRING_TYPES: frozenset[str] = frozenset({"string", "string_fragment", "string_content"})

#: Token types considered identifiers.
_IDENTIFIER_TYPES: frozenset[str] = frozenset({"identifier", "property_identifier"})

#: Token types considered type identifiers (TypeScript).
_TYPE_IDENTIFIER_TYPES: frozenset[str] = frozenset({"type_identifier"})


def normalize_tokens(
    tokens: list[tuple[str, str]],
    mode: str,
) -> list[str]:
    """Return a list of normalised token strings for *tokens*.

    Args:
        tokens: Raw ``(token_type, token_text)`` pairs as produced by
            :func:`~agent.graph_analyzer.parsers.collect_leaf_tokens`.
        mode: One of ``"strict"``, ``"mild"``, ``"weak"``, ``"semantic"``.

    Mode semantics
    --------------
    ``strict`` / ``mild``
        Exact token text is preserved.  (Whitespace/trivia are never
        emitted by the tree-sitter leaf walk, so both modes are identical
        at the token level.)
    ``weak``
        Numeric and string literal *values* are replaced by the placeholders
        ``"<NUM>"`` and ``"<STR>"`` respectively.  Identifiers and type names
        retain their exact text.
    ``semantic``
        Extends ``weak``: identifiers (``identifier``, ``property_identifier``)
        become ``"<ID>"``, type names (``type_identifier``) become
        ``"<TYPE>"``.  Keywords, operators, and punctuation retain their
        exact text.

    Returns
    -------
    list[str]
        One string per input token.
    """
    if mode in ("strict", "mild"):
        return [text for _typ, text in tokens]

    out: list[str] = []
    if mode == "weak":
        for typ, text in tokens:
            if typ in _NUMERIC_TYPES:
                out.append("<NUM>")
            elif typ in _STRING_TYPES:
                out.append("<STR>")
            else:
                out.append(text)
        return out

    if mode == "semantic":
        for typ, text in tokens:
            if typ in _NUMERIC_TYPES:
                out.append("<NUM>")
            elif typ in _STRING_TYPES:
                out.append("<STR>")
            elif typ in _IDENTIFIER_TYPES:
                out.append("<ID>")
            elif typ in _TYPE_IDENTIFIER_TYPES:
                out.append("<TYPE>")
            else:
                out.append(text)
        return out

    raise ValueError(f"Unknown normalization mode: {mode!r}")


# ---------------------------------------------------------------------------
# Suffix array construction (prefix-doubling, O(n log² n))
# ---------------------------------------------------------------------------


def _build_suffix_array(seq: list[int]) -> list[int]:
    """Build the suffix array of *seq* using prefix-doubling (Manber-Myers).

    Returns
    -------
    list[int]
        ``sa`` such that ``sa[i]`` is the starting index of the
        lexicographically ``i``-th suffix of *seq*.

    The implementation uses an O(n log² n) prefix-doubling approach:
    sort suffixes by their first ``2^k`` characters, doubling ``k`` each
    round until the ranking is unique.  No external libraries are used.
    Negative sentinel values (-1, -2, …) sort *before* all real tokens
    so they never form a shared LCP with positive-integer runs — any
    comparison that crosses a sentinel yields a mismatch immediately.
    """
    n = len(seq)
    if n == 0:
        return []

    # Initial rank: use the raw integer value as rank (we map tokens to
    # distinct positive ints and sentinels to distinct negatives, so this
    # gives a valid initial total order).
    rank = list(seq)
    sa = sorted(range(n), key=lambda i: rank[i])

    gap = 1
    while gap < n:
        # Build a sort key of (rank[i], rank[i+gap]) for each suffix i.
        def _key(i: int, _gap: int = gap, _rank: list = rank, _n: int = n) -> tuple:
            return (_rank[i], _rank[i + _gap] if i + _gap < _n else -1)

        sa.sort(key=_key)

        # Re-rank: assign new ranks based on the sorted order.
        new_rank = [0] * n
        for pos in range(1, n):
            prev = sa[pos - 1]
            curr = sa[pos]
            # Equal key → same rank; otherwise increment.
            new_rank[curr] = new_rank[prev] + (0 if _key(curr) == _key(prev) else 1)

        rank = new_rank
        if rank[sa[-1]] == n - 1:
            # All ranks unique — SA is complete.
            break
        gap *= 2

    return sa


# ---------------------------------------------------------------------------
# LCP array (Kasai's algorithm, O(n))
# ---------------------------------------------------------------------------


def _build_lcp_array(seq: list[int], sa: list[int]) -> list[int]:
    """Build the LCP array for *seq* / *sa* using Kasai's algorithm.

    ``lcp[i]`` is the length of the longest common prefix between the
    suffixes ``sa[i-1]`` and ``sa[i]`` (``lcp[0]`` is conventionally 0).
    Sentinels (negative integers) never match positive integers, so LCP
    naturally truncates at function boundaries.
    """
    n = len(seq)
    if n == 0:
        return []

    # Inverse SA: rank[sa[i]] = i
    rank = [0] * n
    for i, s in enumerate(sa):
        rank[s] = i

    lcp = [0] * n
    h = 0  # current LCP length
    for i in range(n):
        if rank[i] == 0:
            h = 0
            continue
        j = sa[rank[i] - 1]
        while i + h < n and j + h < n and seq[i + h] == seq[j + h]:
            # Sentinels are distinct negative integers; they will never
            # match each other (and never match positive tokens), so the
            # while condition exits naturally at a boundary.
            h += 1
        lcp[rank[i]] = h
        if h > 0:
            h -= 1

    return lcp


# ---------------------------------------------------------------------------
# Clone detection
# ---------------------------------------------------------------------------


def compute_clones(
    tokens_by_node: dict[str, list[tuple[str, str]]],
    nodes_by_id: dict[str, Node],
    *,
    min_tokens: int = 50,
    mode: str = "mild",
) -> list[CloneGroup]:
    """Detect duplicate code blocks across function nodes.

    Uses a suffix array built over the combined sentinel-separated token
    sequence of all qualifying functions.  Only inter-function clones are
    reported (see module docstring for v1 limitations).

    Args:
        tokens_by_node: ``{node_id: [(token_type, token_text), ...]}``
            as produced by the parser pass.
        nodes_by_id: ``{node_id: Node}`` — the full node set from the blob.
            Functions not present here are silently skipped.
        min_tokens: Minimum length (in tokens) of a duplicated run to be
            reported as a clone.
        mode: Normalisation mode — see :func:`normalize_tokens`.

    Returns
    -------
    list[CloneGroup]
        Sorted by ``group.id`` for determinism across runs.
    """
    # ---------------------------------------------------------------
    # 1. Collect function nodes and normalise their token streams.
    # ---------------------------------------------------------------
    func_ids = [
        nid for nid in tokens_by_node if nid in nodes_by_id and nodes_by_id[nid].kind == "function"
    ]

    if len(func_ids) < 2:
        return []

    # Sort for determinism — SA output depends on concatenation order.
    func_ids.sort()

    normalised: dict[str, list[str]] = {}
    for nid in func_ids:
        normalised[nid] = normalize_tokens(tokens_by_node[nid], mode)

    # ---------------------------------------------------------------
    # 2. Build the combined integer sequence.
    #    - Distinct normalised tokens → positive integers starting at 1.
    #    - Each function boundary → a unique negative integer (-1, -2, …).
    #    - Sentinels: one after each function (including the last), so the
    #      sequence ends with a sentinel.
    # ---------------------------------------------------------------
    vocab: dict[str, int] = {}
    next_id = [1]

    def _tok_id(tok: str) -> int:
        if tok not in vocab:
            vocab[tok] = next_id[0]
            next_id[0] += 1
        return vocab[tok]

    combined: list[int] = []
    # pos_owner[i] = node_id of the function owning position i, or None for sentinels.
    pos_owner: list[str | None] = []

    sentinel_counter = [-1]  # sentinels are distinct: -1, -2, -3, …

    for nid in func_ids:
        toks = normalised[nid]
        for t in toks:
            combined.append(_tok_id(t))
            pos_owner.append(nid)
        # Append a unique sentinel after this function's tokens.
        combined.append(sentinel_counter[0])
        pos_owner.append(None)
        sentinel_counter[0] -= 1

    n = len(combined)
    if n == 0:
        return []

    # ---------------------------------------------------------------
    # 3. Build SA + LCP.
    # ---------------------------------------------------------------
    sa = _build_suffix_array(combined)
    lcp = _build_lcp_array(combined, sa)

    # ---------------------------------------------------------------
    # 4. Scan for maximal repeated substrings of length >= min_tokens
    #    owned by >= 2 distinct function nodes.
    #
    #    Strategy: scan consecutive pairs (sa[i-1], sa[i]) in the SA.
    #    When lcp[i] >= min_tokens, the two suffixes share exactly lcp[i]
    #    tokens.  We accumulate contiguous blocks of such pairs, tracking
    #    the set of owning functions and the maximum LCP within the block.
    #
    #    A "block" is a maximal range [block_start, block_end) of SA
    #    indices where every lcp[k] >= min_tokens for k in that range.
    #    Within a block, every pair of SA entries shares at least
    #    min(lcp[k]) tokens with each other, and specific adjacent pairs
    #    share exactly lcp[k] tokens.  To capture the maximum match
    #    length for each owner-set we track per-pair lcp values rather
    #    than just the block minimum.
    #
    #    For correctness:
    #    - A suffix sa[i] is "owned" by function F if pos_owner[sa[i]] == F
    #      (i.e. the suffix starts at a real-token position inside F).
    #    - Sentinels have pos_owner == None and cannot be suffix-array
    #      anchors for valid clone positions (we skip them below).
    #    - A candidate clone of length L starting at position p is valid
    #      only if p + L - 1 < n and all positions p..p+L-1 are owned by
    #      the same function (no sentinel in the run of L tokens).
    # ---------------------------------------------------------------

    # Precompute function-run lengths so we can check sentinel-free containment.
    # func_run_end[nid] = exclusive end position of that function's token run.
    func_run_start: dict[str, int] = {}
    func_run_end: dict[str, int] = {}
    for i, owner in enumerate(pos_owner):
        if owner is not None:
            if owner not in func_run_start:
                func_run_start[owner] = i
            func_run_end[owner] = i + 1  # inclusive end + 1

    def _is_valid_start(pos: int, length: int) -> str | None:
        """Return the owning node_id if pos..pos+length-1 is fully within
        one function's token run (no sentinels), else None."""
        owner = pos_owner[pos]
        if owner is None:
            return None
        end = pos + length
        run_end = func_run_end.get(owner, 0)
        if end > run_end:
            return None  # run crosses sentinel boundary
        return owner

    # Map from frozenset(node_ids) → best token_len so far.
    # We only keep the maximum token_len for each distinct set.
    best_by_set: dict[frozenset[str], int] = {}

    # Pass 1 — process each consecutive pair (sa[i-1], sa[i]) with
    # lcp[i] >= min_tokens.  For such pairs the two starting positions
    # share exactly lcp[i] tokens.
    #
    # To find multi-function groups (>2 owners) we also need to process
    # entire blocks together.  We do a two-pass: first collect all
    # per-pair candidates, then also collect block-level candidates.

    i = 1
    while i < n:
        if lcp[i] < min_tokens:
            i += 1
            continue

        # Found the start of a block.  Walk the block collecting:
        #   - all SA positions in the block (for multi-owner detection)
        #   - per-pair lcp values (for maximum match-length per owner pair)
        block_start = i
        j = i
        while j < n and lcp[j] >= min_tokens:
            j += 1
        block_end = j  # exclusive

        # --- Sub-pass A: per-pair candidates ---
        # Each consecutive pair (sa[k-1], sa[k]) shares exactly lcp[k] tokens.
        for k in range(block_start, block_end):
            pair_len = lcp[k]
            pos_a = sa[k - 1]
            pos_b = sa[k]
            owner_a = _is_valid_start(pos_a, pair_len)
            owner_b = _is_valid_start(pos_b, pair_len)
            if owner_a is not None and owner_b is not None and owner_a != owner_b:
                key = frozenset({owner_a, owner_b})
                if pair_len > best_by_set.get(key, 0):
                    best_by_set[key] = pair_len

        # --- Sub-pass B: block-level multi-owner detection ---
        # For each contiguous SA sub-range [lo, hi] (lo >= block_start-1,
        # hi <= block_end-1) the range-min of lcp[lo+1..hi] is the length
        # shared by every suffix in that sub-range.  We want: for each
        # distinct owner-set that appears in SOME sub-range, record the
        # MAXIMUM range-min achievable — i.e. the tightest window that
        # still covers all those owners.
        #
        # Strategy: use a sliding minimum window over SA indices.  Walk the
        # block SA positions (indices block_start-1 .. block_end-1) with a
        # two-pointer.  The LCP value for a window [lo..hi] is
        # min(lcp[lo+1], …, lcp[hi]).  We track this with a running minimum
        # that resets when the left pointer advances.
        #
        # For efficiency we collect, per owner, the set of SA indices where
        # it appears, then use the minimum-coverage-window approach.
        block_sa_indices = list(range(block_start - 1, block_end))
        # Map from SA index → owner (or None for sentinels/invalid).
        # We reuse min_lcp_in_block only as a fallback.
        min_lcp_in_block = min(lcp[k] for k in range(block_start, block_end))

        # Collect all valid (owner, sa_index) in the block.
        # For the multi-owner case we need the length for _is_valid_start,
        # so we first gather owners at the block's min LCP (conservative),
        # then tighten the window to maximise the shared length.
        owner_at_idx: list[str | None] = []
        for k in block_sa_indices:
            owner_at_idx.append(_is_valid_start(sa[k], min_lcp_in_block))

        # Build the set of distinct owners present in the full block.
        all_block_owners = {o for o in owner_at_idx if o is not None}
        if len(all_block_owners) < 2:
            i = block_end
            continue

        # Emit the full owner-set at the block minimum (lower bound).
        full_owner_key = frozenset(all_block_owners)
        if min_lcp_in_block > best_by_set.get(full_owner_key, 0):
            best_by_set[full_owner_key] = min_lcp_in_block

        # Now find the TRUE maximum shared length for each subset of owners
        # using a sliding-window minimum.  We slide over the LCP values
        # associated with consecutive SA index pairs.
        #
        # For each contiguous sub-range [lo..hi] of block_sa_indices,
        # the range-min is min(lcp[block_sa_indices[t]+1] for t in 1..hi-lo).
        # We use a two-pointer: grow right, then shrink left until we no
        # longer cover all owners, then emit.
        #
        # Since we want "all owners in window" we track a count of how many
        # distinct owners are covered and a per-owner count.
        #
        # Limitation: here we emit the max-window length for SUBSET groups
        # only if the subset appears in a tighter window than the full block.
        # We skip this extra scan when the block has only 2 owners (pair
        # already handled by sub-pass A).
        if len(all_block_owners) >= 3:
            m = len(block_sa_indices)
            # lcp_val[t] = lcp value BETWEEN block_sa_indices[t-1] and
            # block_sa_indices[t], i.e. lcp[block_sa_indices[t]] (since
            # SA indices are consecutive integers in the block).
            lcp_vals = [lcp[block_sa_indices[t]] for t in range(1, m)]

            # Use two pointers to find the tightest SA sub-window that
            # still covers all owners, then compute the range-min of LCP
            # values within that window.  The maximum such range-min over
            # all valid windows is the TRUE shared length for all owners.
            # Range-min is recomputed on each shrink step; blocks are
            # small in practice so the O(m²) worst-case is acceptable.
            owner_list = list(all_block_owners)
            owner_count: dict[str, int] = {o: 0 for o in owner_list}
            covered = 0
            needed = len(owner_list)
            lo = 0
            best_window_lcp = 0
            for hi in range(m):
                o = owner_at_idx[hi]
                if o is not None:
                    if owner_count[o] == 0:
                        covered += 1
                    owner_count[o] += 1
                # While all owners covered, try shrinking from the left and
                # record the range-min for each valid window.
                while covered >= needed and lo <= hi:
                    rmin = (
                        min(lcp_vals[lo:hi])
                        if lo < hi
                        else (lcp_vals[lo] if lo < len(lcp_vals) else min_lcp_in_block)
                    )
                    if rmin > best_window_lcp:
                        best_window_lcp = rmin
                    # Shrink left pointer.
                    o_lo = owner_at_idx[lo]
                    if o_lo is not None:
                        owner_count[o_lo] -= 1
                        if owner_count[o_lo] == 0:
                            covered -= 1
                    lo += 1

            if best_window_lcp > best_by_set.get(full_owner_key, 0):
                best_by_set[full_owner_key] = int(best_window_lcp)

        i = block_end  # advance past the processed block

    if not best_by_set:
        return []

    # ---------------------------------------------------------------
    # 4b. Subset suppression.
    #
    # When a set S has token_len T, and there exists a strict superset S'
    # with token_len T' >= T, then S is redundant (S' already reports that
    # all functions in S share a run of length >= T, with additional
    # functions also sharing it).  Remove the subset entry.
    #
    # This ensures that when 3+ functions all share the same block, we emit
    # one group with all three instances rather than one per pair.
    # ---------------------------------------------------------------
    sets_to_remove: set[frozenset[str]] = set()
    all_sets = list(best_by_set.keys())
    for s in all_sets:
        t_s = best_by_set[s]
        for s2 in all_sets:
            if s2 is s:
                continue
            if s < s2 and best_by_set[s2] >= t_s:
                sets_to_remove.add(s)
                break
    for s in sets_to_remove:
        del best_by_set[s]

    if not best_by_set:
        return []

    # ---------------------------------------------------------------
    # 5. Build CloneGroup objects from the candidates.
    # ---------------------------------------------------------------
    groups: list[CloneGroup] = []
    for node_id_set, token_len in best_by_set.items():
        sorted_ids = sorted(node_id_set)
        group_id = "clone:" + mode + ":" + str(token_len) + ":" + "|".join(sorted_ids)
        instances: list[CloneInstance] = []
        for nid in sorted_ids:
            node = nodes_by_id[nid]
            instances.append(
                CloneInstance(
                    node_id=nid,
                    file=node.file or "",
                    line_start=node.line_start or 0,
                    line_end=node.line_end or 0,
                )
            )
        groups.append(
            CloneGroup(
                id=group_id,
                token_len=token_len,
                mode=mode,  # type: ignore[arg-type]
                instances=instances,
            )
        )

    # ---------------------------------------------------------------
    # 6. Compute family_id: groups whose instance FILE SETS are identical
    #    form a family.  family_id is set on groups with >= 2 groups in the
    #    family (singletons get None).
    # ---------------------------------------------------------------
    # Group by sorted-file-set.
    by_file_set: dict[tuple[str, ...], list[CloneGroup]] = defaultdict(list)
    for g in groups:
        files_key = tuple(sorted({inst.file for inst in g.instances}))
        by_file_set[files_key].append(g)

    for files_key, family_groups in by_file_set.items():
        if len(family_groups) >= 2:
            family_id = "family:" + "|".join(files_key)
            for g in family_groups:
                g.family_id = family_id

    # ---------------------------------------------------------------
    # 7. Return sorted by id for determinism.
    # ---------------------------------------------------------------
    groups.sort(key=lambda g: g.id)
    return groups


__all__ = ["compute_clones", "normalize_tokens"]
