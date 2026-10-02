import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, expect, it } from "vitest";
import { GameHelp } from "./GameHelp";

afterEach(cleanup);
it("offers five steps first and keeps detailed rules in a separate disclosure", () => {
  render(<GameHelp />);
  fireEvent.click(screen.getByRole("button", { name: "게임 방법" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("list", { name: "게임 진행 순서" }).children).toHaveLength(5);
  expect(dialog).toHaveTextContent("이 안내를 열어도 게임과 투표 시간은 계속 진행됩니다.");
  const details = dialog.querySelector("details")!;
  expect(details.open).toBe(false);
  fireEvent.click(within(dialog).getByText("자세한 규칙·재접속 안내"));
  expect(details.open).toBe(true);
  for (const rule of [
    "3-3·4-4·장목",
    "현재 투표 가능한 인원 기준",
    "양 팀 연속 0표는 공동 패배",
    "무승부와 양 팀 공동 패배는 서로 다른 결과",
    "이전 표는 자동 복원되지 않습니다",
    "직접 방 나가기를 선택하면 이탈이 즉시 확정",
    "10초 동안 기존 방장과 Ready 상태를 유지",
    "10초가 지나 이탈이 확정되면",
    "경기 무효는 정상 패배가 아닙니다",
  ])
    expect(dialog).toHaveTextContent(rule);
});
it.each(["게임 방법 닫기", "확인하고 닫기"])(
  "closes using %s and restores the menu trigger",
  (name) => {
    render(
      <StrictMode>
        <GameHelp />
      </StrictMode>,
    );
    const trigger = screen.getByRole("button", { name: "게임 방법" });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  },
);
it("handles native cancellation and reopens with details collapsed", () => {
  render(<GameHelp />);
  const trigger = screen.getByRole("button", { name: "게임 방법" });
  fireEvent.click(trigger);
  fireEvent.click(screen.getByText("자세한 규칙·재접속 안내"));
  fireEvent(screen.getByRole("dialog"), new Event("cancel", { cancelable: true }));
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(trigger).toHaveFocus();
  fireEvent.click(trigger);
  expect(screen.getByRole("dialog").querySelector("details")!.open).toBe(false);
});
it("cycles Tab between the dialog edges", () => {
  render(<GameHelp />);
  fireEvent.click(screen.getByRole("button", { name: "게임 방법" }));
  const first = screen.getByRole("button", { name: "게임 방법 닫기" }),
    last = screen.getByRole("button", { name: "확인하고 닫기" });
  first.focus();
  fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
  expect(last).toHaveFocus();
  fireEvent.keyDown(last, { key: "Tab" });
  expect(first).toHaveFocus();
});
