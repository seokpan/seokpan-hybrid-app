// Keep non-component exports separate from the React Fast Refresh boundary.
export function validateRoomInput(
  name: string,
  visibility: string,
  password: string,
  maximum: number,
  minimum: number,
  seconds: number,
): string | null {
  if (Array.from(name.trim()).length < 1 || Array.from(name.trim()).length > 30)
    return "방 이름은 1~30자 입력해 주세요.";
  if (!["PUBLIC", "PRIVATE"].includes(visibility)) return "공개 여부를 선택해 주세요.";
  if (
    visibility === "PRIVATE" &&
    (Array.from(password).length < 4 || Array.from(password).length > 20)
  )
    return "방 비밀번호는 4~20자 입력해 주세요.";
  if (!Number.isInteger(maximum) || maximum < 2 || maximum > 100)
    return "최대 인원은 2~100명입니다.";
  if (!Number.isInteger(minimum) || minimum < 2 || minimum > maximum)
    return "최소 Ready는 2명 이상이며 최대 인원을 넘을 수 없습니다.";
  if (![5, 10, 15, 30].includes(seconds)) return "투표 시간을 선택해 주세요.";
  return null;
}
