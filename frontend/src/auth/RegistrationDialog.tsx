import { useEffect, useRef } from "react";
import type { ReactNode, RefObject } from "react";
import styles from "../styles/screens.module.css";

export function RegistrationDialog({
  children,
  close,
  returnFocus,
  busy,
}: {
  children: ReactNode;
  close: () => void;
  returnFocus: RefObject<HTMLButtonElement | null>;
  busy: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current!;
    node.showModal();
    return () => {
      node.close();
      if (returnFocus.current?.isConnected) returnFocus.current.focus();
    };
  }, [returnFocus]);
  return (
    <dialog
      ref={dialog}
      className={`${styles.card} ${styles.authDialog}`}
      aria-labelledby="signup-title"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <div className={styles.dialogHeading}>
        <h2 id="signup-title">Member 회원가입</h2>
        <button
          className={styles.dialogClose}
          aria-label="회원가입 닫기"
          disabled={busy}
          onClick={close}
        >
          ×
        </button>
      </div>
      {children}
      <button className={styles.secondaryButton} disabled={busy} onClick={close}>
        로그인 화면으로
      </button>
    </dialog>
  );
}
