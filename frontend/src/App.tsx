import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthPage } from "./auth/AuthPage";
import { LobbyPage } from "./lobby/LobbyPage";
import { createSessionServices, SessionProvider, useSession } from "./session/context";
import type { SessionServices } from "./session/context";
import { failureMessage } from "./api/messages";
import styles from "./styles/screens.module.css";
import { RoomPage } from "./room/RoomPage";
import { RoomProvider, useRoomConnection } from "./room/context";
import { RankingsPage } from "./statistics/RankingsPage";
import { UserMenu } from "./statistics/UserMenu";
import { GameHelp } from "./help/GameHelp";
import { OnlineCount } from "./presence/OnlineCount";

function SessionGate({
  authPage = false,
  rankings = false,
}: {
  authPage?: boolean;
  rankings?: boolean;
}) {
  const { view, busy, blocking, enteringRoom, recheck } = useSession();
  const showingAuth = useRef(false);
  const lobbyKey = useRef<string | null>(null);
  const currentLobbyKey =
    !authPage && !rankings && view.phase === "ready" && !view.identity.room_id
      ? JSON.stringify([view.identity.actor_type, view.identity.actor_id])
      : null;
  if (!busy) lobbyKey.current = currentLobbyKey;
  // Keep public credential forms mounted through their own command/recovery.
  // This never retains a Room or Lobby after identity invalidation.
  if (!busy)
    showingAuth.current =
      authPage &&
      (view.phase === "anonymous" ||
        (view.phase === "ready" && view.identity.actor_type === "GUEST"));
  if (busy && showingAuth.current) return <AuthPage />;
  // Only the initiated room-entry command retains its locked draft while Cookie
  // recovery runs. Passive invalidation, logout and other identities do not.
  if (
    busy &&
    enteringRoom &&
    lobbyKey.current &&
    !authPage &&
    !rankings &&
    (view.phase === "loading" || view.phase === "unknown" || currentLobbyKey === lobbyKey.current)
  ) {
    return <LobbyPage key={lobbyKey.current} />;
  }
  if (blocking || view.phase === "unknown" || view.phase === "loading") {
    return (
      <section className={styles.card}>
        <p role="status">접속 정보를 확인하고 있습니다.</p>
      </section>
    );
  }
  if (view.phase === "error")
    return (
      <section className={styles.card}>
        <h1>접속 상태를 확인해 주세요</h1>
        <p role="alert">{failureMessage(view.error)}</p>
        <button className={styles.primaryButton} onClick={recheck}>
          접속 상태 다시 확인
        </button>
      </section>
    );
  if (view.phase === "anonymous") return authPage ? <AuthPage /> : <Navigate to="/login" replace />;
  if (view.phase !== "ready") return null;
  if (authPage && view.identity.actor_type === "GUEST") return <AuthPage />;
  if (view.identity.room_id)
    return (
      <>
        <div className={styles.roomRoute} hidden={rankings}>
          <RoomPage
            active={!rankings}
            key={JSON.stringify([
              view.identity.actor_type,
              view.identity.actor_id,
              view.identity.room_id,
              view.identity.participant_id,
            ])}
          />
        </div>
        {rankings && (
          <RankingsPage
            key={`${view.identity.actor_type}:${view.identity.actor_id}`}
            identity={view.identity}
          />
        )}
      </>
    );
  if (rankings)
    return (
      <RankingsPage
        key={`${view.identity.actor_type}:${view.identity.actor_id}`}
        identity={view.identity}
      />
    );
  if (authPage)
    return view.identity.actor_type === "GUEST" ? <AuthPage /> : <Navigate to="/lobby" replace />;
  return <LobbyPage key={currentLobbyKey} />;
}

function Shell() {
  const location = useLocation();
  const { view, busy, blocking, notice, clearNotice } = useSession();
  const { notice: roomNotice, clearNotice: clearRoomNotice } = useRoomConnection();
  const readyIdentity = view.phase === "ready" ? view.identity : null;
  const inRoom = !!readyIdentity?.room_id;
  const roomOrLobbyCurrent =
    location.pathname === "/" || location.pathname === "/lobby" ? "page" : undefined;
  const roomWorkspace = inRoom && location.pathname !== "/rankings";
  const previousPath = useRef(location.pathname);
  useEffect(() => {
    if (previousPath.current === location.pathname) return;
    const from = previousPath.current;
    previousPath.current = location.pathname;
    // A login result belongs to its automatic lobby redirect as well.
    if (!(from === "/login" && location.pathname === "/lobby")) clearNotice();
    clearRoomNotice();
  }, [location.pathname, clearNotice, clearRoomNotice]);
  useEffect(() => {
    if (busy && !(view.phase === "ready" && view.checking)) clearRoomNotice();
  }, [busy, view, clearRoomNotice]);
  return (
    <div className={roomWorkspace ? styles.appWorkspace : styles.appDocument}>
      <a href="#main-content" className={styles.skipLink}>
        본문으로 바로가기
      </a>
      <header className={styles.header}>
        <div className={styles.headerInner}>
          <Link to="/" className={styles.brand} aria-label="石나가는 판단 홈">
            <span aria-hidden="true">● ○</span>
            <span>石나가는 판단</span>
          </Link>
          {view.phase === "ready" && !blocking && (
            <div className={styles.headerControls}>
              <nav className={styles.primaryNav} aria-label="주요 메뉴">
                <Link to="/lobby" aria-current={roomOrLobbyCurrent}>
                  {inRoom ? "게임방" : "로비"}
                </Link>
                <NavLink to="/rankings">랭킹</NavLink>
              </nav>
              <div className={styles.utilityGroup} role="group" aria-label="서비스 상태와 도움말">
                {!(location.pathname === "/login" && view.identity.actor_type === "GUEST") && (
                  <OnlineCount
                    key={`presence:${view.identity.actor_type}:${view.identity.actor_id}`}
                  />
                )}
                <GameHelp
                  key={`${location.pathname}:${view.identity.actor_type}:${view.identity.actor_id}`}
                />
              </div>
              <div className={styles.accountGroup} role="group" aria-label="계정">
                {view.identity.actor_type === "GUEST" && location.pathname !== "/login" && (
                  <Link to="/login" className={styles.memberUpgrade}>
                    Member 로그인
                  </Link>
                )}
                <UserMenu
                  key={`${view.identity.actor_type}:${view.identity.actor_id}`}
                  identity={view.identity}
                />
              </div>
            </div>
          )}
        </div>
      </header>
      <main
        id="main-content"
        className={`${styles.shell} ${roomWorkspace ? styles.roomShell : ""}`}
        data-room-workspace={roomWorkspace ? "true" : undefined}
        aria-busy={view.phase === "ready" && !!view.checking}
      >
        {notice && (
          <div className={styles.notice}>
            <p role="status">{notice}</p>
            <button type="button" onClick={clearNotice}>
              안내 닫기
            </button>
          </div>
        )}
        {roomNotice && (
          <div className={styles.notice}>
            <p role="status">{roomNotice}</p>
            <button type="button" onClick={clearRoomNotice}>
              방 안내 닫기
            </button>
          </div>
        )}
        <Routes>
          <Route path="/" element={<SessionGate />} />
          <Route path="/login" element={<SessionGate authPage />} />
          <Route path="/lobby" element={<SessionGate />} />
          <Route path="/rankings" element={<SessionGate rankings />} />
          <Route
            path="*"
            element={
              <section className={styles.card}>
                <h1>페이지를 찾을 수 없습니다.</h1>
                <Link to="/">시작 화면으로 돌아가기</Link>
              </section>
            }
          />
        </Routes>
      </main>
      <footer className={`${styles.footer} ${roomWorkspace ? styles.roomFooter : ""}`}>
        SEOKPAN · 함께 투표하고, 하나의 수를 결정합니다.
      </footer>
    </div>
  );
}

export function App({ services: supplied }: { services?: SessionServices }) {
  const [services] = useState(() => supplied ?? createSessionServices());
  return (
    <SessionProvider services={services}>
      <RoomProvider services={services}>
        <Shell />
      </RoomProvider>
    </SessionProvider>
  );
}
