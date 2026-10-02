import { useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useSession } from "../session/context";
import { validateCredentials } from "./validation";
import styles from "../styles/screens.module.css";
import { RegistrationDialog } from "./RegistrationDialog";

function CredentialsForm({
  registering = false,
  registered,
}: {
  registering?: boolean;
  registered?: () => void;
}) {
  const auth = useSession();
  const [loginId, setLoginId] = useState("");
  const [nickname, setNickname] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const prefix = registering ? "signup" : "signin";

  function submit(event: FormEvent) {
    event.preventDefault();
    if (auth.busy) return;
    const invalid = validateCredentials(loginId, password, registering ? nickname : undefined);
    setError(invalid);
    if (invalid) return;
    const secret = password;
    setPassword("");
    if (registering)
      void auth.register(loginId, nickname.trim(), secret).then((success) => {
        if (success) registered?.();
      });
    else void auth.login(loginId, secret);
  }

  return (
    <form onSubmit={submit} noValidate>
      <label htmlFor={`${prefix}-id`}>아이디</label>
      <input
        id={`${prefix}-id`}
        name={`${prefix}-username`}
        autoComplete={`section-${prefix} username`}
        autoCapitalize="none"
        spellCheck={false}
        value={loginId}
        onChange={(event) => setLoginId(event.target.value)}
        aria-describedby={`${prefix}-login-hint`}
        disabled={auth.busy}
        required
      />
      <small id={`${prefix}-login-hint`}>영문 소문자·숫자·밑줄 4~20자</small>
      {registering && (
        <>
          <label htmlFor="nickname">닉네임</label>
          <input
            id="nickname"
            name="nickname"
            autoComplete="nickname"
            value={nickname}
            onChange={(event) => setNickname(event.target.value)}
            aria-describedby="nickname-hint"
            disabled={auth.busy}
            required
          />
          <small id="nickname-hint">한글·영문·숫자·밑줄 2~12자</small>
        </>
      )}
      <label htmlFor={`${prefix}-password`}>비밀번호</label>
      <input
        id={`${prefix}-password`}
        name={`${prefix}-password`}
        type="password"
        autoComplete={`section-${prefix} ${registering ? "new-password" : "current-password"}`}
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        aria-describedby={`${prefix}-password-hint`}
        disabled={auth.busy}
        required
      />
      <small id={`${prefix}-password-hint`}>
        8~64자. 공백·대소문자를 입력한 그대로 사용합니다.
      </small>
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {registering && auth.notice && !auth.busy && (
        <p role="alert" className={styles.error}>
          {auth.notice}
        </p>
      )}
      <button className={styles.primaryButton} type="submit" disabled={auth.busy}>
        {registering ? "가입하기" : "로그인"}
      </button>
    </form>
  );
}

export function AuthPage() {
  const auth = useSession();
  const navigate = useNavigate();
  const [registering, setRegistering] = useState(false);
  const [formVersion, setFormVersion] = useState(0);
  const registrationOpener = useRef<HTMLButtonElement>(null);
  const existingGuest = auth.view.phase === "ready" && auth.view.identity.actor_type === "GUEST";
  function toggle(value: boolean) {
    auth.clearNotice();
    setFormVersion((n) => n + 1);
    setRegistering(value);
  }
  return (
    <>
      <div inert={registering} aria-hidden={registering ? true : undefined}>
        {existingGuest && auth.view.phase === "ready" && auth.view.identity.room_id && (
          <div className={styles.notice}>
            <p>방 참여와 연결은 유지됩니다. 로그인 실패 시 기존 Guest로 계속 이용할 수 있습니다.</p>
            <Link to="/lobby">참여 중인 방으로 돌아가기</Link>
          </div>
        )}
        <section className={styles.hero} aria-labelledby="welcome-title">
          <p className="eyebrow">함께 만드는 한 수</p>
          <h1 id="welcome-title">石나가는 판단</h1>
          <p>
            함께 투표하고, <strong>하나의 수</strong>를 결정하세요.
          </p>
        </section>
        <div className={styles.authGrid}>
          <section
            className={`${styles.card} ${styles.guestCard} ${styles.authCard}`}
            aria-labelledby="guest-title"
          >
            <span className={styles.stones} aria-hidden="true">
              ● ○
            </span>
            <h2 id="guest-title">Guest로 시작</h2>
            <p>회원가입 없이 팀에 참여하고 함께 투표할 수 있습니다.</p>
            {!existingGuest && (
              <button
                className={styles.tealButton}
                disabled={auth.busy}
                onClick={() => {
                  void auth.guest().then((success) => {
                    if (success) navigate("/lobby", { replace: true });
                  });
                }}
              >
                Guest로 시작하기 →
              </button>
            )}
            <p>
              Guest의 개인 전적과 Rating은 영구 저장되지 않습니다. 방 생성은 Member만 가능합니다.
            </p>
          </section>
          <section className={`${styles.card} ${styles.authCard}`} aria-labelledby="auth-title">
            <h2 id="auth-title">Member 로그인</h2>
            <CredentialsForm key={formVersion} />
            <button
              ref={registrationOpener}
              className={styles.secondaryButton}
              disabled={auth.busy}
              onClick={() => toggle(true)}
            >
              회원가입
            </button>
          </section>
        </div>
      </div>
      {registering && (
        <RegistrationDialog
          returnFocus={registrationOpener}
          busy={auth.busy}
          close={() => {
            if (!auth.busy) toggle(false);
          }}
        >
          <CredentialsForm
            registering
            registered={() => {
              setFormVersion((n) => n + 1);
              setRegistering(false);
            }}
          />
        </RegistrationDialog>
      )}
    </>
  );
}
