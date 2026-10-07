import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { HoverPanel } from "./HoverPanel";

afterEach(cleanup);

function mount() {
  render(
    <>
      <HoverPanel title="준비 현황" summary="Ready 4명">
        <button>팀 선택</button>
      </HoverPanel>
      <button>패널 밖</button>
    </>,
  );
  return screen.getByRole("button", { name: /준비 현황/ });
}

describe("expandable workspace panels", () => {
  it("opens on hover and closes after the pointer leaves", () => {
    const trigger = mount();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.mouseEnter(trigger.parentElement!);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    fireEvent.mouseLeave(trigger.parentElement!);
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps details open while focus moves inside and closes when focus leaves", () => {
    const trigger = mount();
    const action = screen.getByRole("button", { name: "팀 선택" });
    fireEvent.focus(trigger);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    fireEvent.blur(trigger, { relatedTarget: action });
    fireEvent.focus(action, { relatedTarget: trigger });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    const modal = render(
      <dialog open>
        <button>확인 모달 취소</button>
      </dialog>,
    );
    const cancel = screen.getByRole("button", { name: "확인 모달 취소" });
    fireEvent.blur(action, { relatedTarget: cancel });
    fireEvent.mouseLeave(trigger.parentElement!);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    fireEvent.focus(action, { relatedTarget: cancel });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    modal.unmount();
    fireEvent.blur(action, { relatedTarget: screen.getByRole("button", { name: "패널 밖" }) });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
  });

  it("supports click pinning and Escape dismissal without reopening on focus return", () => {
    const trigger = mount();
    fireEvent.click(trigger);
    fireEvent.mouseLeave(trigger.parentElement!);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(screen.getByRole("button", { name: "팀 선택" }), { key: "Escape" });
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    fireEvent.mouseLeave(trigger.parentElement!);
    fireEvent.mouseEnter(trigger.parentElement!);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });
});
