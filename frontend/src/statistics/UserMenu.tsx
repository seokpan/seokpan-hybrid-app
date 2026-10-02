import { useEffect, useId, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { failureMessage } from "../api/messages";
import { useSession } from "../session/context";
import type { SessionIdentity } from "../session/recovery";
import { RecordSummary } from "./RankingsPage";
import { useRankings } from "./useRankings";
import s from "./statistics.module.css";

function MemberRecord({ identity }: { identity: SessionIdentity }) {
  const { data, pending, error, refresh } = useRankings(identity, 0, 1);
  return (
    <>
      {pending && <p role="status">전적을 불러오고 있습니다.</p>}
      {error != null && (
        <p role="alert">
          {failureMessage(error)} {data && "마지막으로 확인한 기록입니다."}
        </p>
      )}
      {data?.me && <RecordSummary record={data.me} />}
      <button disabled={pending} onClick={refresh}>
        {error != null ? "전적 다시 조회" : "전적 새로고침"}
      </button>
    </>
  );
}
export function UserMenu({ identity }: { identity: SessionIdentity }) {
  const { busy, logout } = useSession();
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const location = useLocation();
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div
      ref={container}
      className={s.userMenu}
      onBlur={(event) => {
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node))
          setOpen(false);
      }}
    >
      <button
        ref={trigger}
        aria-label="사용자 메뉴"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((value) => !value)}
      >
        <span aria-hidden="true">◉ </span>
        {identity.display_name} · {identity.actor_type === "MEMBER" ? "Member" : "Guest"}
      </button>
      {open && (
        <section id={id} aria-label="사용자 정보" className={s.popover}>
          <h2>
            {identity.display_name}{" "}
            <small>{identity.actor_type === "MEMBER" ? "Member" : "Guest"}</small>
          </h2>
          {identity.actor_type === "MEMBER" ? (
            <MemberRecord identity={identity} />
          ) : (
            <p>Guest의 개인 전적과 Rating은 저장되지 않습니다.</p>
          )}
          {identity.room_id && (
            <p id={`${id}-logout-impact`}>
              로그아웃하면 참여 중인 방에서도 나갑니다. 방장이라면 접속 중인 Member에게 권한이
              넘어가며, 승계할 Member가 없으면 방이 종료될 수 있습니다. 게임 중이라면 이탈 처리로
              결과에 영향을 줄 수 있습니다.
            </p>
          )}
          <button
            className={s.logout}
            disabled={busy}
            aria-describedby={identity.room_id ? `${id}-logout-impact` : undefined}
            onClick={() => void logout()}
          >
            로그아웃
          </button>
        </section>
      )}
    </div>
  );
}
