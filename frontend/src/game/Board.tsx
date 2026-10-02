import { useLayoutEffect, useRef, useState } from "react";
import type { Game } from "./model";
import styles from "./game.module.css";
import type { VoteSummary } from "./votes";

export function Board({
  cells,
  forbidden = [],
  chosen = null,
  winning = [],
  votes = [],
  canVote = false,
  onVote,
  focusScope,
  lastMove = null,
}: {
  cells: Game["board"];
  forbidden?: string[];
  chosen?: string | null;
  winning?: string[];
  canVote?: boolean;
  onVote?: (coordinate: string) => void;
  focusScope?: string;
  votes?: VoteSummary["rows"];
  lastMove?: Game["last_move"];
}) {
  const [focus, setFocus] = useState(112);
  const [expanded, setExpanded] = useState(false);
  const [previousScope, setPreviousScope] = useState(focusScope);
  if (previousScope !== focusScope) {
    setPreviousScope(focusScope);
    setFocus(112);
  }
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const committedScope = useRef(focusScope);
  useLayoutEffect(() => {
    if (committedScope.current !== focusScope) {
      // Keeping the grid must not keep a previous game's command focus active.
      refs.current.find((button) => button === document.activeElement)?.blur();
      committedScope.current = focusScope;
    }
  }, [focusScope]);
  const stones = new Map(cells.map((c) => [c.coordinate, c.stone]));
  const tally = new Map(votes.map((v) => [v.coordinate, v]));
  return (
    <div className={styles.boardFrame}>
      <div className={styles.boardTools}>
        <span>{expanded ? "좌우로 밀어 보드를 확인하세요." : "보드 전체 보기"}</span>
        <button
          type="button"
          aria-pressed={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "보드 전체 보기" : "보드 확대"}
        </button>
      </div>
      <div className={styles.boardScroll}>
        <div
          className={`${styles.board} ${expanded ? styles.expanded : ""}`}
          role="grid"
          aria-label="15×15 오목판"
          aria-rowcount={15}
          aria-colcount={15}
        >
          <div className={styles.columns} aria-hidden="true">
            <span />
            {Array.from("ABCDEFGHIJKLMNO", (c) => (
              <span key={c}>{c}</span>
            ))}
          </div>
          {Array.from({ length: 15 }, (_, row) => (
            <div role="row" className={styles.row} key={row}>
              <span className={styles.rowLabel} aria-hidden="true">
                {row + 1}
              </span>
              {Array.from({ length: 15 }, (_, col) => {
                const index = row * 15 + col,
                  coord = `${String.fromCharCode(65 + col)}${row + 1}`,
                  stone = stones.get(coord);
                const blocked = !canVote || !!stone || forbidden.includes(coord);
                const vote = !stone ? tally.get(coord) : undefined;
                const last = !!stone && lastMove?.coordinate === coord && lastMove.team === stone;
                const won = !!stone && winning.includes(coord);
                const dimmed = !!stone && winning.length > 0 && !won;
                return (
                  <span role="gridcell" key={coord}>
                    <button
                      ref={(node) => {
                        refs.current[index] = node;
                      }}
                      tabIndex={focus === index ? 0 : -1}
                      data-command-focus={
                        focusScope ? JSON.stringify([focusScope, coord]) : undefined
                      }
                      className={`${styles.cell} ${won ? styles.winning : ""}`}
                      aria-label={`${coord} ${
                        stone === "BLACK"
                          ? "흑돌"
                          : stone === "WHITE"
                            ? "백돌"
                            : forbidden.includes(coord)
                              ? "흑 금수"
                              : "빈 자리"
                      }${last ? ", 마지막 착수" : ""}${
                        chosen === coord ? ", 내 투표" : ""
                      }${vote ? `, ${vote.count}표 ${vote.label}` : ""}`}
                      aria-disabled={blocked}
                      onFocus={() => setFocus(index)}
                      onMouseDown={(event) => {
                        if (event.detail > 0) event.preventDefault();
                      }}
                      onClick={() => {
                        if (!blocked) onVote?.(coord);
                      }}
                      onKeyDown={(e) => {
                        let next: number;
                        if (e.key === "ArrowLeft") next = row * 15 + Math.max(0, col - 1);
                        else if (e.key === "ArrowRight") next = row * 15 + Math.min(14, col + 1);
                        else if (e.key === "ArrowUp") next = Math.max(0, row - 1) * 15 + col;
                        else if (e.key === "ArrowDown") next = Math.min(14, row + 1) * 15 + col;
                        else if (e.key === "Home") next = row * 15;
                        else if (e.key === "End") next = row * 15 + 14;
                        else return;
                        e.preventDefault();
                        refs.current[next]?.focus();
                      }}
                    >
                      {stone ? (
                        <span
                          aria-hidden="true"
                          className={`${
                            stone === "BLACK" ? styles.black : styles.white
                          } ${last ? styles.lastStone : ""} ${
                            won ? styles.winnerStone : ""
                          } ${dimmed ? styles.dimmedStone : ""}`}
                        >
                          {last && <span className={styles.lastMark} />}
                        </span>
                      ) : vote ? (
                        <span
                          aria-hidden="true"
                          className={`${styles.voteBadge} ${
                            vote.rank === 1 ? styles.topVote : ""
                          } ${chosen === coord ? styles.myVoteBadge : ""}`}
                        >
                          {chosen === coord ? (
                            <span className={styles.myVoteMark}>나</span>
                          ) : (
                            <>
                              <span>{vote.label.slice(0, -1)}</span>
                              <span>%</span>
                            </>
                          )}
                        </span>
                      ) : (
                        <span aria-hidden="true">
                          {forbidden.includes(coord) ? "×" : chosen === coord ? "◎" : ""}
                        </span>
                      )}
                    </button>
                  </span>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
