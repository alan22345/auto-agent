import { describe, it, expect } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { CollapsibleSection } from '@/components/code-graph/collapsible-section';

describe('CollapsibleSection', () => {
  it('shows the count and hides children until expanded', () => {
    render(
      <CollapsibleSection title="Cycles" count={3} testId="sec">
        <p>body content</p>
      </CollapsibleSection>,
    );
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.queryByText('body content')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('body content')).toBeInTheDocument();
  });
});
