import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { failureMessage } from "../api/messages";
import type { SessionIdentity } from "../session/recovery";
import styles from "../styles/screens.module.css";
import s from "./statistics.module.css";
import type { MemberStatistics } from "./model";
import { useRankings } from "./useRankings";

export function RecordSummary({ record }: { record: MemberStatistics }) {
  return (
    <>
      <p className={s.rating}>
        Rating <strong>{record.rating.toLocaleString("ko-KR")}</strong>
      </p>
      <dl className={s.record}>
        <div>
          <dt>승</dt>
          <dd>{record.wins}</dd>
        </div>
        <div>
          <dt>무</dt>
          <dd>{record.draws}</dd>
        </div>
        <div>
          <dt>패</dt>
          <dd>{record.losses}</dd>
        </div>
        <div>
          <dt>참여 경기</dt>
          <dd>{record.games_played}</dd>
        </div>
      </dl>
      {record.rank === null && <p className={styles.muted}>아직 완료한 유효 경기가 없습니다.</p>}
    </>
  );
}

export function RankingsPage({ identity }: { identity: SessionIdentity }) {
  const [offset, setOffset] = useState(0);
  const { data, pending, error, refresh } = useRankings(identity, offset);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return (
    <section className={`${styles.card} ${s.rankings}`} aria-labelledby="rankings-title">
      <div className={styles.sectionHeading}>
        <h1 id="rankings-title" ref={heading} tabIndex={-1}>
          랭킹
        </h1>
        <button className={styles.secondaryButton} disabled={pending} onClick={refresh}>
          새로고침
        </button>
      </div>
      <p className={styles.muted}>Member의 유효 경기 결과와 Rating을 기준으로 집계됩니다.</p>
      <p className={s.hint}>Guest의 전적과 Rating은 랭킹에 반영되지 않습니다.</p>
      <div className={s.feedback}>
        {pending && (
          <p role="status">
            {data ? "최신 순위를 확인하고 있습니다." : "랭킹을 불러오고 있습니다."}
          </p>
        )}
        {error != null && (
          <p role="alert" className={styles.error}>
            {failureMessage(error)} {data && "아래는 마지막으로 확인한 기록입니다."}
          </p>
        )}
      </div>
      <div className={s.tableFrame}>
        <table className={s.table} aria-label="Member 랭킹" aria-busy={pending}>
          <thead>
            <tr>
              {["순위", "닉네임", "Rating", "승", "무", "패", "참여 경기"].map((label) => (
                <th key={label} scope="col">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data?.items.map((row) => (
              <tr
                key={row.member_id}
                className={row.member_id === data.me?.member_id ? s.ownRow : undefined}
              >
                <td>
                  <span className={row.rank! <= 3 ? s.medal : undefined} data-rank={row.rank}>
                    {row.rank}
                  </span>
                </td>
                <th scope="row">
                  {row.nickname}
                  {row.member_id === data.me?.member_id && <span className={s.meTag}>나</span>}
                </th>
                <td>{row.rating.toLocaleString("ko-KR")}</td>
                <td>{row.wins}</td>
                <td>{row.draws}</td>
                <td>{row.losses}</td>
                <td>{row.games_played}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.items.length === 0 && (
          <p className={styles.empty}>
            {offset === 0
              ? "아직 랭킹에 등록된 Member가 없습니다."
              : "이 페이지에는 등록된 Member가 없습니다."}
          </p>
        )}
      </div>
      <nav className={s.pagination} aria-label="랭킹 페이지">
        <button
          className={styles.secondaryButton}
          disabled={pending || offset === 0}
          onClick={() => setOffset((value) => Math.max(0, value - 20))}
        >
          이전
        </button>
        <span>{offset / 20 + 1} 페이지</span>
        <button
          className={styles.secondaryButton}
          disabled={pending || error != null || !data?.has_more}
          onClick={() => setOffset((value) => value + 20)}
        >
          다음
        </button>
      </nav>
      {data?.me && (
        <section className={s.myRank} aria-label="내 순위와 전적">
          <div className={s.ownHeading}>
            <h2>
              내 순위 <strong>{data.me.rank === null ? "미등록" : `${data.me.rank}위`}</strong>
            </h2>
            <strong>{data.me.nickname}</strong>
          </div>
          <RecordSummary record={data.me} />
        </section>
      )}
      <details className={s.sortHelp}>
        <summary>순위 집계 기준</summary>
        <p>
          Rating → 승 수 → 승률 → 참여 경기 수는 높은 순, 닉네임은 오름차순입니다. 승률은 승 수를
          유효 경기 수로 나눈 값이며 시스템 무효 경기는 제외합니다.
        </p>
      </details>
      <Link className={`${styles.secondaryButton} ${s.back}`} to="/lobby">
        {identity.room_id ? "← 참여 중인 방으로" : "← 로비로"}
      </Link>
    </section>
  );
}
