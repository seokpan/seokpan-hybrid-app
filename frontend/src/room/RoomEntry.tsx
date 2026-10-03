import { useState } from "react";
import type { FormEvent } from "react";
import { useSession } from "../session/context";
import type { LobbySnapshot } from "../lobby/snapshot";
import styles from "../styles/screens.module.css";
import { validateRoomInput } from "./input";
import { EntryDialog } from "./EntryDialog";

type EntryProps = { cancel: () => void; enabled: boolean; trigger: HTMLButtonElement };

export function CreateRoomForm({ cancel, enabled, trigger }: EntryProps) {
  const auth = useSession();
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"PUBLIC" | "PRIVATE">("PUBLIC");
  const [password, setPassword] = useState("");
  const [maximum, setMaximum] = useState("100");
  const [minimum, setMinimum] = useState("4");
  const [seconds, setSeconds] = useState(15);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState(false);
  function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !enabled ||
      auth.busy ||
      auth.view.phase !== "ready" ||
      auth.view.identity.actor_type !== "MEMBER" ||
      auth.view.identity.room_id
    )
      return;
    const invalid = validateRoomInput(
      name,
      visibility,
      password,
      Number(maximum),
      Number(minimum),
      seconds,
    );
    setError(invalid);
    setSubmitted(false);
    if (invalid) return;
    const body = {
      request_id: crypto.randomUUID(),
      name: name.trim(),
      visibility,
      password: visibility === "PRIVATE" ? password : null,
      max_participants: Number(maximum),
      minimum_ready: Number(minimum),
      vote_seconds: seconds,
    };
    setPassword("");
    setSubmitted(true);
    void auth.enterRoom(() => auth.api.request("/api/v1/rooms", "post", { body }));
  }
  return (
    <EntryDialog title="방 만들기" trigger={trigger} close={cancel} busy={auth.busy}>
      <form onSubmit={submit} noValidate aria-label="방 만들기" aria-busy={auth.busy}>
        <fieldset disabled={auth.busy}>
          <label htmlFor="room-name">방 이름</label>
          <input
            id="room-name"
            data-entry-focus
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
          <small>1~30자. 앞뒤 공백은 제외합니다.</small>
          <label htmlFor="room-visibility">공개 여부</label>
          <select
            id="room-visibility"
            value={visibility}
            onChange={(e) => {
              setVisibility(e.target.value as "PUBLIC" | "PRIVATE");
              setPassword("");
            }}
          >
            <option value="PUBLIC">공개</option>
            <option value="PRIVATE">비공개</option>
          </select>
          {visibility === "PRIVATE" && (
            <>
              <label htmlFor="room-password">방 비밀번호</label>
              <input
                id="room-password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
              <small>4~20자. 전송한 비밀번호는 입력창에 남기지 않습니다.</small>
            </>
          )}
          <div className={styles.entryNumbers}>
            <div>
              <label htmlFor="room-minimum">최소 Ready 인원</label>
              <input
                id="room-minimum"
                type="number"
                min="2"
                max={maximum}
                value={minimum}
                onChange={(e) => setMinimum(e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="room-maximum">최대 입장 인원</label>
              <input
                id="room-maximum"
                type="number"
                min="2"
                max="100"
                value={maximum}
                onChange={(e) => setMaximum(e.target.value)}
              />
            </div>
          </div>
          <small>각각 2명 이상이며 최소 Ready 인원은 정원을 넘을 수 없습니다.</small>
          <label htmlFor="create-vote-seconds">투표 제한 시간</label>
          <select
            id="create-vote-seconds"
            value={seconds}
            onChange={(e) => setSeconds(Number(e.target.value))}
          >
            {[5, 10, 15, 30].map((value) => (
              <option key={value} value={value}>
                {value}초
              </option>
            ))}
          </select>
          <small>게임 시작 전 대기방에서 방장이 변경할 수 있습니다.</small>
        </fieldset>
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {submitted && !auth.busy && auth.notice && (
          <p role="alert" className={styles.error}>
            {auth.notice}
          </p>
        )}
        <p role="status">
          {auth.busy
            ? "방 생성 결과와 접속 정보를 확인하고 있습니다."
            : !enabled
              ? "목록 연결과 참여 상태를 확인한 뒤 생성할 수 있습니다."
              : "설정을 확인하고 방을 만들어 주세요."}
        </p>
        <button
          className={styles.primaryButton}
          data-command-focus="create-room"
          disabled={auth.busy || !enabled}
        >
          방 만들기
        </button>
      </form>
    </EntryDialog>
  );
}

export function JoinRoomForm({
  room,
  cancel,
  enabled,
  trigger,
  refreshList,
}: EntryProps & {
  room: LobbySnapshot["rooms"][number];
  refreshList: () => void;
}) {
  const auth = useSession();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const full = room.participant_count >= room.max_participants;
  return (
    <EntryDialog
      title={room.password_required ? "비공개 방 입장" : "방 입장"}
      trigger={trigger}
      close={cancel}
      busy={auth.busy}
    >
      <p className={styles.entryRoom}>
        방 이름 · <strong>{room.name}</strong>
      </p>
      <p>
        {room.participant_count} / {room.max_participants}명 ·{" "}
        {room.status === "PLAYING" ? "게임 중" : "대기 중"}
      </p>
      {room.status === "PLAYING" && <p>진행 중인 판에는 관전자로 입장합니다.</p>}
      <form
        aria-label="방 입장"
        noValidate
        aria-busy={auth.busy}
        onSubmit={(event) => {
          event.preventDefault();
          if (
            !enabled ||
            full ||
            auth.busy ||
            auth.view.phase !== "ready" ||
            auth.view.identity.room_id
          )
            return;
          setError("");
          setSubmitted(false);
          if (
            room.password_required &&
            (Array.from(password).length < 4 || Array.from(password).length > 20)
          ) {
            setError("방 비밀번호는 4~20자 입력해 주세요.");
            return;
          }
          const body = {
            request_id: crypto.randomUUID(),
            expected_state_version: room.state_version,
            password: room.password_required ? password : null,
          };
          setPassword("");
          setSubmitted(true);
          void auth
            .enterRoom(() =>
              auth.api.request("/api/v1/rooms/{room_id}/joins", "post", {
                path: { room_id: room.room_id },
                body,
              }),
            )
            .then((entered) => {
              if (!entered) refreshList();
            });
        }}
      >
        {room.password_required && (
          <>
            <label htmlFor="join-password">방 비밀번호</label>
            <input
              id="join-password"
              data-entry-focus
              type="password"
              autoComplete="off"
              value={password}
              disabled={auth.busy}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
            <small>4~20자. 전송 후에는 다시 입력해 주세요.</small>
          </>
        )}
        {error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {submitted && !auth.busy && auth.notice && (
          <p role="alert" className={styles.error}>
            {auth.notice}
          </p>
        )}
        <p role="status">
          {auth.busy
            ? "입장 결과와 접속 정보를 확인하고 있습니다."
            : !enabled
              ? "방이 종료되었거나 목록 연결을 확인 중입니다. 지금은 입장할 수 없습니다."
              : full
                ? "정원이 찼습니다. 빈자리가 생기면 입장할 수 있습니다."
                : "현재 방 상태를 확인하고 입장해 주세요."}
        </p>
        <button
          className={styles.primaryButton}
          data-command-focus="join-room"
          disabled={!enabled || auth.busy || full}
        >
          입장 확인
        </button>
      </form>
    </EntryDialog>
  );
}
