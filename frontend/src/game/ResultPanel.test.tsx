import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResultPanel } from "./ResultPanel";
import { parseResult } from "./model";
import { resultFixture } from "./fixtures.test-support";

afterEach(cleanup);
const result = parseResult(resultFixture(), "r1", "g1", true);

describe("result presentation", () => {
  it.each([
    ["DRAW", "무승부"],
    ["JOINT_LOSS", "양 팀 공동 패배"],
    ["SYSTEM_INVALID", "경기 무효"],
  ] as const)("does not display a winning stone for %s", (reason, title) => {
    render(
      <ResultPanel
        result={{
          ...result,
          end_reason: reason,
          winner: "EMPTY",
          winning_line: null,
          game_status: reason === "SYSTEM_INVALID" ? "SYSTEM_INVALID" : "FINISHED",
          stats_eligible: reason !== "SYSTEM_INVALID",
          my_rating: null,
        }}
        close={vi.fn()}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "경기 결과" });
    expect(within(dialog).getByText(title)).toBeInTheDocument();
    expect(dialog.querySelector("[data-team]")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "내 Rating 변동" })).not.toBeInTheDocument();
  });

  it("opens the result dialog and presents the final counts and personal Rating", () => {
    render(<ResultPanel result={result} close={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "경기 결과" });
    expect(dialog).toHaveAttribute("open");
    expect(within(dialog).getByText("흑팀 승리")).toBeInTheDocument();
    expect(within(dialog).getByText("공식 착수").nextElementSibling).toHaveTextContent("5수");
    expect(within(dialog).getByText("마지막 투표 기회").nextElementSibling).toHaveTextContent(
      "9번째",
    );
    expect(within(dialog).getByRole("region", { name: "내 Rating 변동" })).toHaveTextContent(
      "1000 → 1016",
    );
    expect(within(dialog).getByText("+16")).toBeInTheDocument();
  });

  it("dismisses without leaving the final board and restores focus for reopening", () => {
    const close = vi.fn();
    render(<ResultPanel result={result} close={close} />);
    const trigger = screen.getByRole("button", { name: "결과 보기" });
    fireEvent(
      screen.getByRole("dialog", { name: "경기 결과" }),
      new Event("cancel", { cancelable: true }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(close).not.toHaveBeenCalled();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "보드 계속 보기" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "결과 닫고 대기방 보기" }));
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("shows why no Rating is available without inventing a personal score", () => {
    const { rerender } = render(
      <ResultPanel
        result={{ ...result, stats_eligible: false, my_rating: null }}
        close={vi.fn()}
      />,
    );
    expect(
      within(screen.getByRole("dialog")).getByText("전적과 Rating에 반영하지 않습니다."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "내 Rating 변동" })).not.toBeInTheDocument();
    rerender(<ResultPanel result={{ ...result, my_rating: null }} close={vi.fn()} />);
    expect(
      within(screen.getByRole("dialog")).getByText(
        "이 결과에는 본인의 Rating 변동 내역이 없습니다.",
      ),
    ).toBeInTheDocument();
  });
});
