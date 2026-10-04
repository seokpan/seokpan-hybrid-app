import { useEffect, useRef } from "react";
import styles from "../styles/screens.module.css";

export function KickDialog({
  name,
  busy,
  close,
  confirm,
  trigger,
}: {
  name: string;
  busy: boolean;
  close: () => void;
  confirm: () => void;
  trigger: HTMLButtonElement;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const node = dialog.current!;
    node.showModal();
    return () => {
      node.close();
      if (trigger.isConnected && !trigger.disabled) trigger.focus();
    };
  }, [trigger]);
  return (
    <dialog
      ref={dialog}
      className={`${styles.card} ${styles.authDialog}`}
      aria-labelledby="kick-title"
      aria-describedby="kick-description"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) close();
      }}
    >
      <h2 id="kick-title">참가자 강퇴</h2>
      <p id="kick-description">{name} 님을 방에서 내보낼까요? 해당 참가자는 로비로 이동합니다.</p>
      <p className={styles.muted}>계정을 차단하거나 로그아웃시키는 기능은 아닙니다.</p>
      <button autoFocus className={styles.secondaryButton} disabled={busy} onClick={close}>
        취소
      </button>{" "}
      <button className={styles.primaryButton} disabled={busy} onClick={confirm}>
        {busy ? "처리 중…" : "강퇴 확인"}
      </button>
    </dialog>
  );
}
