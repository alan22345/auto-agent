import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { GraphCompletionBadge } from "@/components/code-graph/graph-completion-badge";

describe("GraphCompletionBadge", () => {
  it("shows Complete label when is_complete=true", () => {
    render(
      <GraphCompletionBadge
        progress={{
          is_complete: true,
          processed: 200,
          total: 200,
          last_file: null,
          status: "unchanged",
        }}
      />,
    );
    expect(screen.getByText(/Complete/)).toBeInTheDocument();
    expect(screen.getByText(/200 files/)).toBeInTheDocument();
  });

  it("shows Analyzing while running", () => {
    render(
      <GraphCompletionBadge
        progress={{
          is_complete: false,
          processed: 50,
          total: 200,
          last_file: "app/foo.tsx",
          status: "running",
        }}
      />,
    );
    expect(screen.getByText(/Analyzing/)).toBeInTheDocument();
    expect(screen.getByText(/50 \/ 200/)).toBeInTheDocument();
    expect(screen.getByText(/app\/foo\.tsx/)).toBeInTheDocument();
  });

  it("shows Partial when idle and incomplete", () => {
    render(
      <GraphCompletionBadge
        progress={{
          is_complete: false,
          processed: 100,
          total: 200,
          last_file: "app/bar.tsx",
          status: "idle",
        }}
      />,
    );
    expect(screen.getByText(/Partial/)).toBeInTheDocument();
    expect(screen.getByText(/100 \/ 200/)).toBeInTheDocument();
  });
});
