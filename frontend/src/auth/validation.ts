// Mirrors identity/domain/member.py. Do not normalize login IDs or passwords.
export function validateCredentials(
  loginId: string,
  password: string,
  nickname?: string,
): string | null {
  if (!/^[a-z0-9_]{4,20}$/.test(loginId))
    return "아이디는 영문 소문자·숫자·밑줄로 4~20자 입력해 주세요.";
  if (nickname !== undefined && !/^[A-Za-z0-9_가-힣]{2,12}$/.test(nickname.trim())) {
    return "닉네임은 한글·영문·숫자·밑줄로 2~12자 입력해 주세요.";
  }
  // Python len counts Unicode code points, not JavaScript UTF-16 code units.
  const length = Array.from(password).length;
  if (length < 8 || length > 64) return "비밀번호는 8~64자 입력해 주세요.";
  return null;
}
