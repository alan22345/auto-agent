'use client';
// ADR-016 Phase 7 §11 — debounced search input for the graph canvas.
//
// The input owns its own keystroke-level state so typing feels
// instantaneous, and emits a debounced ``onChange`` to the parent so
// the cytoscape side effect that fades non-matching nodes only runs
// once every 300ms of quiet keyboard.
//
// The component is intentionally dumb — no canvas knowledge, no styles
// beyond a tiny icon row. The page lifts the query into a stateful
// prop and passes it to the canvas.

import { Search } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Input } from '@/components/ui/input';

interface Props {
  /** Called with the trimmed-but-otherwise-verbatim search query after
   * a 300ms quiet window. Empty string when the input is cleared. */
  onChange: (value: string) => void;
  /** Optional initial value (e.g. when restoring from a URL param).
   * Does NOT trigger the debounced ``onChange`` on mount — the parent
   * is assumed to already hold this value. */
  initialValue?: string;
  /** Optional placeholder text. */
  placeholder?: string;
  /** Optional className passthrough for layout. */
  className?: string;
}

export const SEARCH_DEBOUNCE_MS = 300;

export function SearchInput({
  onChange,
  initialValue = '',
  placeholder = 'Search nodes…',
  className,
}: Props) {
  const [value, setValue] = useState(initialValue);
  // Track whether the latest change came from the user (vs the mount
  // / initialValue path) so we don't double-fire on mount.
  const userTouchedRef = useRef(false);

  useEffect(() => {
    if (!userTouchedRef.current) return;
    const handle = setTimeout(() => {
      onChange(value);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [value, onChange]);

  return (
    <div
      className={`relative inline-flex items-center ${className ?? ''}`}
      data-testid="graph-search-input-wrapper"
    >
      <Search
        size={14}
        className="pointer-events-none absolute left-2 text-muted-foreground"
      />
      <Input
        type="search"
        data-testid="graph-search-input"
        aria-label="Search graph nodes by name"
        placeholder={placeholder}
        value={value}
        onChange={(e) => {
          userTouchedRef.current = true;
          setValue(e.target.value);
        }}
        className="h-9 w-56 pl-7"
      />
    </div>
  );
}
