import { useEffect, useId, useRef, useState } from "react";
import type { RefObject } from "react";
import { createPortal } from "react-dom";
import { StoneMark } from "../brand/StoneMark";
import type { Result } from "./model";
import styles from "./game.module.css";

export function resultTone(endReason: Result["end_reason"]) {
  if (endReason === "BLACK_WIN" || endReason === "WHITE_WIN" || endReason === "FORFEIT")
    return "win";
  if (endReason === "DRAW") return "draw";
  if (endReason === "SYSTEM_INVALID") return "invalid";
  return "loss";
}

export const resultTitle: Record<Result["end_reason"], string> = {
  BLACK_WIN: "흑팀 승리",
  WHITE_WIN: "백팀 승리",
  DRAW: "무승부",
  FORFEIT: "이탈로 인한 몰수 종료",
  JOINT_LOSS: "양 팀 공동 패배",
  SYSTEM_INVALID: "경기 무효",
};

function deltaLabel(delta: number) {
  return `${delta >= 0 ? "+" : ""}${delta}`;
}

export function ResultPanel({ result, close }: { result: Result; close: () => void }) {
  const [open, setOpen] = useState(true);
  const trigger = useRef<HTMLButtonElement>(null);
  const rating = result.my_rating;
  return (
    <>
      <aside className={styles.resultSummary} aria-label="결과 정보">
        <div>
          <strong>경기 결과</strong>
          <p>
            {rating
              ? `Rating ${rating.rating_after} · ${deltaLabel(rating.rating_delta)}`
              : result.stats_eligible
                ? "개인 Rating 내역 없음"
                : "전적 반영 없음"}
          </p>
        </div>
        <button ref={trigger} type="button" onClick={() => setOpen(true)}>
          결과 보기
        </button>
      </aside>
      {open && (
        <ResultDialog
          result={result}
          returnFocus={trigger}
          dismiss={() => setOpen(false)}
          close={close}
        />
      )}
    </>
  );
}

function ResultDialog({
  result,
  returnFocus,
  dismiss,
  close,
}: {
  result: Result;
  returnFocus: RefObject<HTMLButtonElement | null>;
  dismiss: () => void;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const noteId = useId();
  const rating = result.my_rating;
  useEffect(() => {
    const node = dialog.current!;
    node.showModal();
    return () => {
      node.close();
      if (returnFocus.current?.isConnected) returnFocus.current.focus();
    };
  }, [returnFocus]);
  return createPortal(
    <dialog
      ref={dialog}
      className={styles.resultDialog}
      aria-labelledby={titleId}
      aria-describedby={noteId}
      onCancel={(event) => {
        event.preventDefault();
        dismiss();
      }}
      onClick={(event) => {
        if (event.target !== event.currentTarget) return;
        const bounds = event.currentTarget.getBoundingClientRect();
        if (
          event.clientX < bounds.left ||
          event.clientX > bounds.right ||
          event.clientY < bounds.top ||
          event.clientY > bounds.bottom
        )
          dismiss();
      }}
    >
      <div className={styles.resultDialogHeading}>
        <h2 id={titleId}>경기 결과</h2>
        <button
          type="button"
          className={styles.resultDismiss}
          aria-label="결과 모달 닫기"
          onClick={dismiss}
        >
          ×
        </button>
      </div>
      <div className={styles.resultOutcome} data-tone={resultTone(result.end_reason)}>
        {result.winner === "BLACK" || result.winner === "WHITE" ? (
          <span className={styles.resultStone} data-team={result.winner} aria-hidden="true" />
        ) : (
          <StoneMark />
        )}
        <p>{resultTitle[result.end_reason]}</p>
      </div>
      {result.end_reason === "FORFEIT" && (
        <p className={styles.resultReason}>{result.winner === "BLACK" ? "흑팀" : "백팀"} 승리</p>
      )}
      <dl className={styles.resultMetrics}>
        <div>
          <dt>공식 착수</dt>
          <dd>
            {result.move_no}
            <span>수</span>
          </dd>
        </div>
        <div>
          <dt>마지막 투표 기회</dt>
          <dd>
            {result.turn_no}
            <span>번째</span>
          </dd>
        </div>
      </dl>
      {rating ? (
        <section className={styles.resultRating} aria-label="내 Rating 변동">
          <div>
            <h3>내 Rating</h3>
            <p>
              {rating.rating_before}
              <span> → </span>
              <strong>{rating.rating_after}</strong>
            </p>
          </div>
          <span
            className={styles.ratingDelta}
            data-sign={
              rating.rating_delta > 0 ? "positive" : rating.rating_delta < 0 ? "negative" : "zero"
            }
          >
            {deltaLabel(rating.rating_delta)}
          </span>
        </section>
      ) : (
        <p className={styles.resultStats}>
          {result.stats_eligible
            ? "이 결과에는 본인의 Rating 변동 내역이 없습니다."
            : "전적과 Rating에 반영하지 않습니다."}
        </p>
      )}
      <p id={noteId} className={styles.resultNext}>
        다음 판에는 다시 팀·Ready를 확인해 주세요.
      </p>
      <div className={styles.resultActions}>
        <button type="button" autoFocus onClick={dismiss}>
          보드 계속 보기
        </button>
        <button type="button" className={styles.resultReturn} onClick={close}>
          결과 닫고 대기방 보기
        </button>
      </div>
    </dialog>,
    document.body,
  );
}
