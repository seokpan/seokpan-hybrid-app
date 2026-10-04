import { useEffect, useId, useRef, useState } from "react";
import screens from "../styles/screens.module.css";
import styles from "./help.module.css";

/** Read-only help. Opening it must not change participation, timers or sockets. */
export function GameHelp() {
  const [trigger, setTrigger] = useState<HTMLButtonElement | null>(null);
  return (
    <>
      <button type="button" onClick={(event) => setTrigger(event.currentTarget)}>
        게임 방법
      </button>
      {trigger && <HelpDialog trigger={trigger} close={() => setTrigger(null)} />}
    </>
  );
}
function HelpDialog({ trigger, close }: { trigger: HTMLButtonElement; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const first = useRef<HTMLButtonElement>(null),
    last = useRef<HTMLButtonElement>(null);
  const titleId = useId();
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
      className={`${screens.card} ${styles.dialog}`}
      aria-labelledby={titleId}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        if (event.shiftKey && event.target === first.current) {
          event.preventDefault();
          last.current?.focus();
        } else if (!event.shiftKey && event.target === last.current) {
          event.preventDefault();
          first.current?.focus();
        }
      }}
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <div className={screens.dialogHeading}>
        <h2 id={titleId}>함께 두는 오목, 게임 방법</h2>
        <button
          type="button"
          ref={first}
          autoFocus
          className={screens.dialogClose}
          aria-label="게임 방법 닫기"
          onClick={close}
        >
          ×
        </button>
      </div>
      <p>한 사람이 바로 착수하는 게임이 아닙니다. 같은 팀의 표를 모아 한 수를 결정합니다.</p>
      <ol className={styles.steps} aria-label="게임 진행 순서">
        {[
          ["방 입장", "원하는 방에 입장합니다. 방 생성은 Member만 가능합니다."],
          [
            "팀 선택·Ready",
            "흑팀 또는 백팀을 고르고 Ready합니다. 조건이 갖춰지면 방장이 시작합니다.",
          ],
          [
            "위치 투표",
            "내 팀 차례에 원하는 빈자리를 선택합니다. 마감 전 변경·취소할 수 있습니다.",
          ],
          [
            "최다 득표 착수",
            "가장 많은 표를 받은 자리에 돌을 놓습니다. 동률이면 서버가 무작위로 정합니다.",
          ],
          [
            "결과·다음 판",
            "승리·무승부 등 결과를 확인합니다. 방이 유지되면 다시 Ready하여 다음 판을 준비합니다.",
          ],
        ].map(([title, text], index) => (
          <li key={title}>
            <span aria-hidden="true">{index + 1}</span>
            <h3>{title}</h3>
            <p>{text}</p>
          </li>
        ))}
      </ol>
      <p>진행 중인 판에 입장하면 관전하며, 다음 판부터 참여할 수 있습니다.</p>
      <p className={screens.notice}>이 안내를 열어도 게임과 투표 시간은 계속 진행됩니다.</p>
      <details className={styles.rules}>
        <summary>자세한 규칙·재접속 안내</summary>
        <section>
          <h3>1. 방에 들어가 준비하기</h3>
          <p>
            Member만 방을 만들 수 있고, Member와 Guest 모두 방에 입장할 수 있습니다. 비공개 방에는
            방 비밀번호가 필요합니다.
          </p>
          <p>
            흑팀 또는 백팀을 선택하고 Ready를 누르세요. 최소 Ready 인원을 채우고 양 팀에 각각
            Ready한 사람이 1명 이상 있으면 방장이 시작할 수 있습니다.
          </p>
          <p>
            시작할 때 Ready하지 않았거나 게임 중 들어온 사람은 이번 판의 관전자입니다. 대화와 관전은
            가능하지만 투표·이번 판 전적 반영에서는 제외됩니다. 다음 판에는 팀을 선택하고 Ready하여
            참여할 수 있습니다.
          </p>
          <p>
            팀을 바꾸면 본인의 Ready가 해제됩니다. 방장 변경·투표 시간 변경·게임 종료 때는 모두의
            Ready가 해제됩니다.
          </p>
        </section>
        <section>
          <h3>2. 투표로 한 수 정하기</h3>
          <p>
            흑팀이 먼저 시작합니다. 현재 차례 팀의 연결된 참가자만 제한 시간 안에 투표할 수
            있습니다. 투표 시간은 방에서 5·10·15·30초 중 정하며 기본은 15초입니다.
          </p>
          <p>
            빈 자리를 선택하면 투표합니다. 다른 자리를 선택하면 표가 바뀌고, 투표 취소로 철회할 수
            있습니다. 한 사람의 마지막 유효표 1개만 집계합니다.
          </p>
          <p>
            득표율은 현재 투표 가능한 인원 기준입니다. 미투표자가 있으면 합계가 100%보다 작습니다.
            후보 표시는 아직 확정된 돌이 아닙니다.
          </p>
          <p>
            가장 많은 표를 받은 자리에 서버가 착수하며 동률은 서버가 무작위 선택합니다. 화면의 0초
            표시는 마감·승패 확정이 아닙니다.
          </p>
          <p>
            0표는 돌 없이 차례를 넘기는 Pass입니다. 공식 착수 수는 늘지 않지만 투표 차례는
            넘어갑니다. 양 팀 연속 0표는 공동 패배입니다.
          </p>
        </section>
        <section>
          <h3>3. 보드와 금수</h3>
          <p>
            보드는 15×15이며 열 A–O, 행 1–15로 표시합니다. 흑은 정확히 5목, 백은 5목 이상이면
            승리합니다. 흑의 3-3·4-4·장목은 금수입니다. ×는 서버에서 알려준 흑 금수이며 그 자리에
            투표할 수 없습니다.
          </p>
          <p>
            숫자는 후보 득표율, 청록 테두리는 내 표, 진한 표시는 최다 득표 후보입니다. 돌 안의
            네모는 마지막 착수를 표시합니다.
          </p>
          <p>
            방향키로 좌표를 이동할 수 있습니다. 투표 가능한 차례의 참가자만 Enter 또는 Space로
            투표할 수 있습니다. 관전 중에는 좌표를 살펴볼 수 있지만 투표 입력은 받지 않습니다.
          </p>
        </section>
        <section>
          <h3>4. 종료와 다음 판</h3>
          <p>
            승리 조건을 만족한 팀이 이깁니다. 어느 팀도 승리하지 못한 채 보드가 가득 차면
            무승부입니다. 무승부와 양 팀 공동 패배는 서로 다른 결과입니다.
          </p>
          <p>
            한 팀의 이번 판 참가자 전원이 이탈 확정되면 그 팀은 몰수패합니다. 양 팀 모두 전원이 이탈
            확정되면 공동 패배입니다. 잠깐 연결이 끊긴 것만으로 이탈을 확정하지 않습니다.
          </p>
          <p>
            방이 유지되면 게임 종료 후 대기 상태로 돌아갑니다. 결과를 닫고 다시 Ready하면 다음 판을
            준비할 수 있습니다. Member의 유효 경기 전적·Rating만 저장하며 Guest의 개인 전적은
            저장하지 않습니다.
          </p>
          <p>
            경기 무효는 정상 패배가 아닙니다. 시스템 문제로 정상 진행·복구가 불가능하여 무효 종료된
            판은 전적·Rating에 반영하지 않습니다.
          </p>
        </section>
        <section>
          <h3>5. 연결이 끊겼을 때</h3>
          <p>
            연결 단절이 감지되면 마감 전 제출한 표는 제거됩니다. 방이 유지되고 10초 안에 같은
            사용자로 돌아오면 참여 상태를 복구할 수 있지만, 이전 표는 자동 복원되지 않습니다. 현재
            차례에 투표할 수 있다면 다시 제출해 주세요. 재접속을 기다리는 동안에도 게임 시간은
            흐릅니다.
          </p>
          <p>
            방장이 직접 방 나가기를 선택하면 이탈이 즉시 확정됩니다. 이 경우 접속 중인 Member 중
            먼저 입장한 사람이 방장을 이어받고 모든 Ready가 해제됩니다. 이어받을 Member가 없으면
            방이 종료됩니다.
          </p>
          <p>
            방장의 연결만 끊긴 경우에는 10초 동안 기존 방장과 Ready 상태를 유지합니다. 그 안에 같은
            사용자로 다시 연결하면 방장 권한도 유지됩니다. 10초가 지나 이탈이 확정되면 접속 중인
            Member에게 방장이 넘어가고 모든 Ready가 해제됩니다. 이탈 확정 뒤 돌아온 이전 방장에게
            방장 권한이 자동으로 돌아가지는 않습니다.
          </p>
          <p>
            서버 장애는 개인의 무투표나 이탈로 처리하지 않습니다. 화면에서 결과를 추정하지 말고
            서버의 복구·종료 안내를 확인해 주세요.
          </p>
        </section>
      </details>
      <button type="button" ref={last} className={screens.primaryButton} onClick={close}>
        확인하고 닫기
      </button>
    </dialog>
  );
}
