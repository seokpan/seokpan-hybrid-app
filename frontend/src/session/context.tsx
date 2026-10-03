import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import type { ReactNode } from "react";
import { ApiClient, ApiFailure } from "../api/client";
import { failureMessage } from "../api/messages";
import { SessionRecovery } from "./recovery";
import { browserSocket } from "../realtime/stream";
import type { SocketFactory } from "../realtime/stream";
import { browserTabChannel, SessionActivity } from "./activity";
import type { TabChannelFactory } from "./activity";
import { captureCommandFocus } from "./focus";

export function createSessionServices(
  api = new ApiClient(),
  socketFactory: SocketFactory = browserSocket,
  tabChannelFactory: TabChannelFactory = browserTabChannel,
) {
  return { api, recovery: new SessionRecovery(api), socketFactory, tabChannelFactory };
}
export type SessionServices = ReturnType<typeof createSessionServices>;

function useSessionState(services: SessionServices) {
  const view = useSyncExternalStore(services.recovery.subscribe, services.recovery.getSnapshot);
  const [busy, setBusy] = useState(false);
  const [blocking, setBlocking] = useState(false);
  const [enteringRoom, setEnteringRoom] = useState(false);
  const [notice, setNotice] = useState("");
  const focus = useRef<{
    scope: string;
    saved: NonNullable<ReturnType<typeof captureCommandFocus>>;
  } | null>(null);
  const focusScope =
    view.phase === "ready"
      ? JSON.stringify([
          view.identity.actor_type,
          view.identity.actor_id,
          view.identity.room_id,
          view.identity.participant_id,
        ])
      : "";
  useLayoutEffect(() => {
    if (busy || !focus.current) return;
    const pending = focus.current;
    focus.current = null;
    if (focusScope && pending.scope === focusScope) pending.saved.restore();
    else pending.saved.cancel();
  }, [busy, focusScope]);
  const locked = useRef(false);
  const mounted = useRef(false);
  const lifecycle = useRef(0);
  const pendingCheck = useRef(false);
  const releasePendingRecovery = useRef<(() => void) | null>(null);
  const [activity] = useState(
    () =>
      new SessionActivity(
        (preserveView) => services.recovery.reset(preserveView),
        () => {
          const ready = services.recovery.getSnapshot().phase === "ready";
          if (locked.current) {
            if (!ready) pendingCheck.current = true;
          } else void services.recovery.recover(ready);
        },
        services.tabChannelFactory,
        document,
        window,
        () => services.recovery.getSnapshot().phase !== "anonymous",
      ),
  );

  useEffect(() => {
    mounted.current = true;
    lifecycle.current += 1;
    activity.start();
    const unsubscribe = services.api.subscribeAuthFailure(() => {
      if (locked.current) pendingCheck.current = true;
      else activity.recheck();
    });
    void services.recovery.recover();
    return () => {
      mounted.current = false;
      lifecycle.current += 1;
      unsubscribe();
      activity.stop();
      pendingCheck.current = false;
      releasePendingRecovery.current?.();
      releasePendingRecovery.current = null;
      locked.current = false;
      services.recovery.reset();
      focus.current?.saved.cancel();
      focus.current = null;
    };
  }, [services, activity]);

  // Auth/room commands may change Cookie or participation even if a response is lost.
  // Never repeat the command; recheck identity without closing the room transport.
  async function execute(
    command: () => Promise<unknown>,
    successMessage = "",
    changesSession = false,
    refreshIdentity = true,
    roomEntry = false,
  ): Promise<boolean> {
    const current = services.recovery.getSnapshot();
    const phase = current.phase;
    if (
      locked.current ||
      (phase === "ready" && current.checking) ||
      (phase !== "ready" && phase !== "anonymous")
    )
      return false;
    locked.current = true;
    const releaseRecovery = services.recovery.hold();
    releasePendingRecovery.current = releaseRecovery;
    const startedIn = lifecycle.current;
    const saved = captureCommandFocus();
    focus.current = saved && focusScope ? { scope: focusScope, saved } : null;
    if (saved && !focusScope) saved.cancel();
    setBusy(true);
    setEnteringRoom(roomEntry);
    setBlocking(refreshIdentity);
    setNotice("");
    let message = successMessage;
    let failed = false;
    try {
      await command();
    } catch (error) {
      failed = true;
      message = failureMessage(error);
    } finally {
      releaseRecovery();
      if (releasePendingRecovery.current === releaseRecovery) releasePendingRecovery.current = null;
      if (mounted.current && startedIn === lifecycle.current) {
        if (changesSession) activity.notifyChange();
        // Recheck identity after every command, but retain the mounted screen
        // during a successful in-room command. Inputs remain locked throughout.
        // Uncertain outcomes and cross-tab/auth invalidation still fail closed.
        if (
          refreshIdentity ||
          failed ||
          pendingCheck.current ||
          services.recovery.getSnapshot().phase !== "ready"
        ) {
          pendingCheck.current = false;
          setBlocking(true);
          services.recovery.reset();
          await services.recovery.recover();
        } else await services.recovery.recover(true);
        if (mounted.current && startedIn === lifecycle.current) {
          setNotice(message);
          setBusy(false);
          setBlocking(false);
          setEnteringRoom(false);
        }
      }
      if (startedIn === lifecycle.current) {
        locked.current = false;
        if (mounted.current && pendingCheck.current) {
          pendingCheck.current = false;
          void services.recovery.recover();
        }
      }
    }
    return !failed && mounted.current && startedIn === lifecycle.current;
  }

  function recheck() {
    if (locked.current) return;
    services.recovery.reset();
    void services.recovery.recover();
  }

  return {
    ...services,
    view,
    busy: busy || (view.phase === "ready" && !!view.checking),
    blocking,
    enteringRoom,
    notice,
    clearNotice: () => setNotice(""),
    recheck,
    run: execute,
    enterRoom: (command: () => Promise<unknown>) => execute(command, "", false, true, true),
    mutate: (command: () => Promise<unknown>) => execute(command, "", false, false),
    login: (login_id: string, password: string) =>
      execute(
        () =>
          services.api.request("/api/v1/sessions/member", "post", { body: { login_id, password } }),
        "",
        true,
      ),
    guest: () =>
      execute(() => services.api.request("/api/v1/sessions/guest", "post", {}), "", true),
    logout: () =>
      execute(
        () => services.api.request("/api/v1/session", "delete", {}),
        "로그아웃 요청을 처리했습니다.",
        true,
      ),
    register: (login_id: string, nickname: string, password: string) =>
      execute(async () => {
        const member = await services.api.request("/api/v1/members", "post", {
          body: { login_id, nickname, password },
        });
        if (
          !member ||
          !Number.isSafeInteger(member.member_id) ||
          member.member_id < 1 ||
          member.login_id !== login_id ||
          member.nickname !== nickname.trim()
        ) {
          throw new ApiFailure("invalid-response");
        }
      }, "회원가입이 완료되었습니다. 아이디와 비밀번호로 로그인해 주세요."),
  };
}

const SessionContext = createContext<ReturnType<typeof useSessionState> | null>(null);

export function SessionProvider({
  services,
  children,
}: {
  services: SessionServices;
  children: ReactNode;
}) {
  const value = useSessionState(services);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession() {
  const value = useContext(SessionContext);
  if (!value) throw new Error("SESSION_PROVIDER_REQUIRED");
  return value;
}
