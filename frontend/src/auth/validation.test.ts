import { describe, expect, it } from "vitest";
import { validateCredentials } from "./validation";

describe("Member input boundaries match Backend domain", () => {
  it.each(["abcd", "a".repeat(20), "a_12"])("accepts login %s", (id) => {
    expect(validateCredentials(id, "p".repeat(8), " 닉네임 ")).toBeNull();
  });
  it.each(["abc", "a".repeat(21), "Upper", " abcd", "abcd\n"])("rejects login %j", (id) => {
    expect(validateCredentials(id, "p".repeat(8))).not.toBeNull();
  });
  it.each(["가", "ㄱㄴ", "a".repeat(13), "a-b", "😀😀"])("rejects nickname %s", (nickname) => {
    expect(validateCredentials("abcd", "password", nickname)).not.toBeNull();
  });
  it("counts password code points and preserves whitespace", () => {
    expect(validateCredentials("abcd", "😀".repeat(7))).not.toBeNull();
    expect(validateCredentials("abcd", "😀".repeat(64))).toBeNull();
    expect(validateCredentials("abcd", "😀".repeat(65))).not.toBeNull();
    expect(validateCredentials("abcd", "        ")).toBeNull();
  });
});
