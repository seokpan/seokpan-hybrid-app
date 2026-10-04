import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiFailure } from "../api/client";
import { useSession } from "../session/context";
import { normalizeChat, parseReceipt, validChat } from "./model";
import { ChatStream } from "./stream";
import styles from "./chat.module.css";
import screens from "../styles/screens.module.css";

export function ChatPanel({
  roomId = null,
  enabled,
  active = true,
}: {
  roomId?: string | null;
  enabled: boolean;
  active?: boolean;
}) {
  const auth = useSession();
  const title = roomId === null ? "로비 채팅" : "방 채팅";
  if (!enabled || auth.view.phase !== "ready")
    return (
      <section className={styles.panel} aria-label={title}>
        <h2>{title}</h2>
        <p role="status">접속 상태 확인 후 채팅을 이용할 수 있습니다.</p>
      </section>
    );
  const identity = auth.view.identity;
  return (
    <ConnectedChat
      key={JSON.stringify([
        identity.actor_type,
        identity.actor_id,
        identity.participant_id,
        roomId,
      ])}
      roomId={roomId}
      title={title}
      active={active}
    />
  );
}
function ConnectedChat({
  roomId,
  title,
  active,
}: {
  roomId: string | null;
  title: string;
  active: boolean;
}) {
  const { api, socketFactory, busy } = useSession();
  const [stream] = useState(() => new ChatStream(roomId, socketFactory));
  const view = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [unread, setUnread] = useState(false);
  const request = useRef<AbortController | null>(null);
  const composing = useRef(false),
    follow = useRef(true);
  const log = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    stream.start();
    return () => {
      stream.stop();
      request.current?.abort();
      request.current = null;
    };
  }, [stream]);
  useEffect(() => {
    if (view.phase !== "ready") {
      if (request.current) {
        request.current.abort();
        request.current = null;
        setPending(false);
        setFeedback("전송 결과를 확인할 수 없습니다. 대화를 확인한 뒤 다시 보내 주세요.");
      }
    }
  }, [view.phase]);
  useLayoutEffect(() => {
    if (active && follow.current && log.current) {
      log.current.scrollTop = log.current.scrollHeight;
      setUnread(false);
    } else if (view.messages.length) setUnread(true);
  }, [view.messages, active]);
  const text = normalizeChat(draft),
    length = [...text].length;
  const canSend = active && !busy && !pending && view.phase === "ready" && validChat(text);
  async function send() {
    if (!canSend || request.current || composing.current) return;
    const controller = new AbortController();
    request.current = controller;
    setPending(true);
    setFeedback("");
    try {
      const body = { request_id: crypto.randomUUID(), text };
      const receipt =
        roomId === null
          ? await api.request("/api/v1/chat/lobby", "post", { body, signal: controller.signal })
          : await api.request("/api/v1/chat/rooms/{room_id}", "post", {
              path: { room_id: roomId },
              body,
              signal: controller.signal,
            });
      parseReceipt(receipt);
      if (request.current !== controller) return;
      setDraft("");
      setFeedback("전송했습니다.");
    } catch (error) {
      if (request.current !== controller) return;
      setFeedback(
        error instanceof ApiFailure &&
          error.kind === "http" &&
          error.status !== null &&
          error.status < 500
          ? "전송하지 못했습니다. 입력 내용과 로그인·방 참여 상태를 확인해 주세요."
          : "전송 결과를 확인할 수 없습니다. 대화를 확인한 뒤 다시 보내 주세요. 자동으로 재전송하지 않습니다.",
      );
    } finally {
      if (request.current === controller) {
        request.current = null;
        setPending(false);
      }
    }
  }
  return (
    <section className={styles.panel} aria-label={title}>
      <h2>{title}</h2>
      <p className={styles.hint}>{view.notice}</p>
      {view.phase === "closed" && (
        <button
          type="button"
          className={screens.secondaryButton}
          disabled={!active || busy}
          onClick={() => {
            setFeedback("");
            stream.start();
          }}
        >
          채팅 다시 연결
        </button>
      )}
      <div
        className={styles.log}
        ref={log}
        role="log"
        aria-label={`${title} 메시지`}
        aria-live={active ? "polite" : "off"}
        aria-relevant="additions"
        tabIndex={0}
        onScroll={() => {
          const node = log.current!;
          follow.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
          if (follow.current) setUnread(false);
        }}
      >
        {view.messages.length === 0 ? (
          <p className={styles.empty}>연결 후 도착한 메시지가 여기에 표시됩니다.</p>
        ) : (
          view.messages.map((message) => (
            <p className={styles.message} key={message.id}>
              <time dateTime={message.at}>
                {new Date(message.at).toLocaleTimeString("ko-KR", {
                  hour: "2-digit",
                  minute: "2-digit",
                  hour12: false,
                })}
              </time>{" "}
              <strong>{message.name}</strong> <span>{message.text}</span>
            </p>
          ))
        )}
      </div>
      <div className={styles.newMessages}>
        {unread && (
          <button
            type="button"
            disabled={!active}
            onClick={() => {
              follow.current = true;
              if (log.current) log.current.scrollTop = log.current.scrollHeight;
              setUnread(false);
            }}
          >
            새 메시지 보기 ↓
          </button>
        )}
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label htmlFor={id}>{title} 메시지 입력</label>
        <div className={styles.composer}>
          <textarea
            id={id}
            rows={2}
            value={draft}
            readOnly={pending}
            disabled={!active || busy || view.phase !== "ready"}
            aria-describedby={`${id}-help ${id}-result`}
            onChange={(event) => setDraft(event.target.value)}
            onCompositionStart={() => {
              composing.current = true;
            }}
            onCompositionEnd={() => {
              composing.current = false;
            }}
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing &&
                event.keyCode !== 229 &&
                !composing.current
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <button type="submit" className={screens.primaryButton} disabled={!canSend}>
            {pending ? "전송 중" : "전송"}
          </button>
        </div>
        <p id={`${id}-help`} className={styles.hint}>
          {length}/200자 · Enter 전송 · Shift+Enter 줄바꿈
        </p>
        <p id={`${id}-result`} className={styles.feedback} role="status">
          {feedback || (length > 200 ? "200자 이내로 입력해 주세요." : "\u00a0")}
        </p>
      </form>
    </section>
  );
}
