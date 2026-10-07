import { useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import styles from "./game.module.css";

export function HoverPanel({
  title,
  summary,
  children,
  upward = false,
}: {
  title: string;
  summary: ReactNode;
  children: ReactNode;
  upward?: boolean;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const open = !dismissed && (hovered || focused || pinned);
  return (
    <div
      className={styles.hoverPanel}
      data-open={open}
      data-upward={upward}
      onMouseEnter={() => {
        setHovered(true);
        setDismissed(false);
      }}
      onMouseLeave={() => {
        setHovered(false);
        if (!focused) setDismissed(false);
      }}
      onFocusCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setDismissed(false);
        setFocused(true);
      }}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          setFocused(false);
          setPinned(false);
          setDismissed(false);
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          setPinned(false);
          trigger.current?.focus();
          setDismissed(true);
        }
      }}
    >
      <button
        ref={trigger}
        type="button"
        className={styles.hoverSummary}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          if (pinned) {
            setPinned(false);
            setDismissed(true);
          } else {
            setPinned(true);
            setDismissed(false);
          }
        }}
      >
        <span>
          <strong>{title}</strong>
          <span className={styles.hoverMeta}>{summary}</span>
        </span>
        <span className={styles.hoverHint} aria-hidden="true">
          {open ? "접기 ▴" : "상세 ▾"}
        </span>
      </button>
      <div id={id} className={styles.hoverDetails}>
        {children}
      </div>
    </div>
  );
}
