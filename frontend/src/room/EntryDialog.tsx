import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import styles from "../styles/screens.module.css";

export function EntryDialog({
  title,
  trigger,
  close,
  busy,
  children,
}: {
  title: string;
  trigger: HTMLButtonElement;
  close: () => void;
  busy: boolean;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const node = dialog.current!;
    node.showModal();
    node.querySelector<HTMLElement>("[data-entry-focus]")?.focus();
    return () => {
      node.close();
      if (trigger.isConnected && !trigger.disabled) trigger.focus({ preventScroll: true });
      else document.getElementById("lobby-title")?.focus({ preventScroll: true });
    };
  }, [trigger]);
  function dismiss() {
    if (!busy) close();
  }
  return (
    <dialog
      ref={dialog}
      className={`${styles.card} ${styles.entryDialog}`}
      aria-labelledby={titleId}
      tabIndex={-1}
      onCancel={(event) => {
        event.preventDefault();
        dismiss();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const nodes = Array.from(
          dialog.current!.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not(:disabled), select:not(:disabled)",
          ),
        );
        const first = nodes[0],
          last = nodes.at(-1);
        if (!first) {
          event.preventDefault();
          dialog.current!.focus();
          return;
        }
        if (event.shiftKey && (event.target === first || event.target === dialog.current)) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && (event.target === last || event.target === dialog.current)) {
          event.preventDefault();
          first.focus();
        }
      }}
    >
      <div className={styles.dialogHeading}>
        <h2 id={titleId}>{title}</h2>
        <button
          type="button"
          className={styles.dialogClose}
          aria-label={`${title} 닫기`}
          disabled={busy}
          onClick={dismiss}
        >
          ×
        </button>
      </div>
      {children}
      <button type="button" className={styles.secondaryButton} disabled={busy} onClick={dismiss}>
        취소
      </button>
    </dialog>
  );
}
