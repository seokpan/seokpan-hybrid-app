import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { summarizeVotes } from "./votes";
import { Board } from "./Board";

afterEach(cleanup);
describe("server vote tally presentation", () => {
  it("uses eligible voters including non-voters, retains equal ranks and does not select a move", () => {
    const input = [
      { coordinate: "H8", count: 2 },
      { coordinate: "G7", count: 2 },
      { coordinate: "A1", count: 1 },
    ];
    const before = structuredClone(input);
    const result = summarizeVotes(input, 10);
    expect(result.total).toBe(5);
    expect(result.rows.map((v) => [v.coordinate, v.rank, v.label])).toEqual([
      ["G7", 1, "20.0%"],
      ["H8", 1, "20.0%"],
      ["A1", 3, "10.0%"],
    ]);
    expect(input).toEqual(before);
    expect(summarizeVotes([...input].reverse(), 10)).toEqual(result);
  });
  it("has no percentage for zero votes and supports fractions", () => {
    expect(summarizeVotes([], 0)).toEqual({ total: 0, rows: [] });
    expect(summarizeVotes([], 3)).toEqual({ total: 0, rows: [] });
    expect(
      summarizeVotes(
        [
          { coordinate: "A1", count: 1 },
          { coordinate: "B1", count: 2 },
        ],
        3,
      ).rows.map((v) => v.label),
    ).toEqual(["66.7%", "33.3%"]);
  });
  it("shows all candidates, distinguishes the viewer and removes old turn badges", () => {
    const rows = summarizeVotes(
      [
        { coordinate: "H8", count: 1 },
        { coordinate: "G7", count: 1 },
      ],
      2,
    ).rows;
    const { rerender } = render(<Board cells={[]} chosen="H8" votes={rows} />);
    expect(
      screen.getByRole("button", { name: "H8 빈 자리, 내 투표, 1표 50.0%" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "G7 빈 자리, 1표 50.0%" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /흑돌|백돌/ })).not.toBeInTheDocument();
    rerender(<Board cells={[{ coordinate: "G7", stone: "BLACK" }]} />);
    expect(screen.getByRole("button", { name: "G7 흑돌" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /%|내 투표/ })).not.toBeInTheDocument();
  });
  it("recalculates after vote removal and disconnection without choosing a winner", () => {
    const before = summarizeVotes(
      [
        { coordinate: "H8", count: 1 },
        { coordinate: "G7", count: 1 },
      ],
      3,
    );
    expect(before.rows.map((v) => v.label)).toEqual(["33.3%", "33.3%"]);
    const after = summarizeVotes([{ coordinate: "G7", count: 1 }], 2);
    expect(after.rows[0].label).toBe("50.0%");
    expect(summarizeVotes([{ coordinate: "G7", count: 1 }], 3).rows[0].label).toBe("33.3%");
    expect(summarizeVotes([], 1).rows).toEqual([]);
  });
});
