import { useEffect, useState, useSyncExternalStore } from "react";
import { useSession } from "../session/context";
import { PresenceStream } from "./stream";
import styles from "./presence.module.css";

export function OnlineCount() {
  const { socketFactory } = useSession();
  const [stream] = useState(() => new PresenceStream(socketFactory));
  const view = useSyncExternalStore(stream.subscribe, stream.getSnapshot);
  useEffect(() => {
    stream.start();
    return stream.stop;
  }, [stream]);
  return (
    <span className={styles.presence} aria-label="전체 접속자">
      <span className={styles.dot} data-connected={view.phase === "ready"} aria-hidden="true" />
      <span>
        {view.phase === "ready"
          ? `접속 ${view.count!.toLocaleString("ko-KR")}명`
          : view.phase === "checking"
            ? "접속자 확인 중"
            : "접속자 확인 필요"}
      </span>
      {view.phase === "unavailable" && (
        <button type="button" onClick={stream.start} aria-label="접속자 다시 확인">
          ↻
        </button>
      )}
    </span>
  );
}
