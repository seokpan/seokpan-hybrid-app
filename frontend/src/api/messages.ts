import { ApiFailure } from "./client";

const messages: Record<string, string> = {
  STATISTICS_UNAVAILABLE: "전적을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.",
  INVALID_STATISTICS_RESPONSE: "전적 응답을 확인할 수 없습니다. 다시 조회해 주세요.",
  AUTH_INVALID_CREDENTIALS: "아이디 또는 비밀번호를 확인해 주세요.",
  AUTH_REQUIRED: "로그인이 만료되었습니다. 다시 접속해 주세요.",
  LOGIN_ID_ALREADY_EXISTS: "이미 사용 중인 아이디입니다.",
  NICKNAME_ALREADY_EXISTS: "이미 사용 중인 닉네임입니다.",
  INVALID_LOGIN_ID: "아이디는 영문 소문자·숫자·밑줄로 4~20자 입력해 주세요.",
  INVALID_NICKNAME: "닉네임은 한글·영문·숫자·밑줄로 2~12자 입력해 주세요.",
  INVALID_PASSWORD: "비밀번호는 8~64자 입력해 주세요.",
  CSRF_INVALID: "접속 정보가 변경되었습니다. 현재 로그인 상태를 확인한 뒤 다시 조작해 주세요.",
  ORIGIN_NOT_ALLOWED: "허용된 서비스 주소로 접속해 주세요.",
  STALE_STATE: "상태가 변경되었습니다. 최신 상태를 확인한 뒤 다시 선택해 주세요.",
  STALE_GAME: "이미 다른 판으로 넘어갔습니다. 현재 게임을 확인해 주세요.",
  STALE_TURN: "투표 차례가 바뀌었습니다. 현재 차례를 확인해 주세요.",
  TURN_DEADLINE_REACHED: "이번 투표 시간이 끝났습니다. 서버의 마감 결과를 기다려 주세요.",
  TURN_NOT_VOTING: "현재는 투표를 받는 시간이 아닙니다.",
  PLAYER_REQUIRED: "이번 판의 관전자는 투표할 수 없습니다.",
  CURRENT_TEAM_REQUIRED: "현재 차례인 팀만 투표할 수 있습니다.",
  POSITION_OCCUPIED: "이미 돌이 놓인 자리입니다. 다른 자리를 선택해 주세요.",
  BLACK_DOUBLE_THREE: "흑 3-3 금수입니다. 다른 자리를 선택해 주세요.",
  BLACK_DOUBLE_FOUR: "흑 4-4 금수입니다. 다른 자리를 선택해 주세요.",
  BLACK_OVERLINE: "흑 장목 금수입니다. 다른 자리를 선택해 주세요.",
  MINIMUM_READY_NOT_MET: "최소 Ready 인원이 아직 모이지 않았습니다.",
  BOTH_TEAMS_REQUIRED: "흑팀과 백팀에 각각 Ready 참가자가 필요합니다.",
  ROOM_PASSWORD_INVALID: "방 비밀번호를 확인해 주세요.",
  ROOM_CAPACITY_REACHED: "방 정원이 찼습니다. 다른 방을 선택해 주세요.",
  OWNER_REQUIRED: "현재 방장만 할 수 있는 작업입니다.",
  ROOM_NOT_WAITING: "대기 중인 방에서만 할 수 있는 작업입니다.",
  CANNOT_KICK_SELF: "본인을 강퇴할 수 없습니다. 방 나가기를 이용해 주세요.",
  PARTICIPANT_NOT_FOUND: "이미 방을 떠난 참가자입니다. 최신 목록을 확인해 주세요.",
};

export function failureMessage(error: unknown): string {
  if (!(error instanceof ApiFailure))
    return "요청을 처리하지 못했습니다. 잠시 후 다시 확인해 주세요.";
  if (error.kind === "timeout" || error.kind === "network" || error.kind === "aborted") {
    return "서버 응답을 확인하지 못했습니다. 요청이 처리되었을 수 있으니 현재 상태를 확인해 주세요.";
  }
  if (error.kind === "invalid-response")
    return "서버 응답을 읽지 못했습니다. 상태를 다시 확인해 주세요.";
  return (
    messages[error.code] ??
    (error.status === 503
      ? "서버가 잠시 요청을 처리할 수 없습니다. 잠시 후 다시 확인해 주세요."
      : "요청을 처리하지 못했습니다. 입력과 현재 상태를 확인해 주세요.")
  );
}
