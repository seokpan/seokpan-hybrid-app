/** Only an invalidation hint crosses tabs; identity, tokens and commands never do. */
export type TabChannel = Pick<BroadcastChannel, "postMessage" | "close" | "onmessage">;
export type TabChannelFactory = () => TabChannel | null;
export const browserTabChannel: TabChannelFactory = () => {
  try {
    return typeof window.BroadcastChannel === "function"
      ? new window.BroadcastChannel("seokpan-session-v1")
      : null;
  } catch {
    return null;
  }
};

export class SessionActivity {
  #channel: TabChannel | null = null;
  #timer: ReturnType<typeof setTimeout> | undefined;
  #started = false;
  constructor(
    private readonly invalidate: (preserveView?: boolean) => void,
    private readonly recover: () => void,
    private readonly factory: TabChannelFactory,
    private readonly page = document,
    private readonly browser = window,
    private readonly allowRefocus: () => boolean = () => true,
  ) {}
  start() {
    if (this.#started) return;
    this.#started = true;
    try {
      this.#channel = this.factory();
    } catch {
      this.#channel = null;
    }
    if (this.#channel)
      this.#channel.onmessage = (event) => {
        const data: unknown = event.data;
        if (
          data &&
          typeof data === "object" &&
          Object.keys(data).length === 1 &&
          "type" in data &&
          data.type === "session-changed"
        )
          this.recheck();
      };
    this.page.addEventListener("visibilitychange", this.refocus);
    this.browser.addEventListener("focus", this.refocus);
    this.browser.addEventListener("pageshow", this.recheck);
    this.browser.addEventListener("online", this.recheck);
  }
  refocus = () => {
    if (!this.allowRefocus()) return;
    this.#check(true);
  };
  recheck = () => {
    this.#check(false);
  };
  #check(preserveView: boolean) {
    if (!this.#started) return;
    this.invalidate(preserveView); // Disable stale commands immediately, before the next click.
    clearTimeout(this.#timer);
    this.#timer = undefined;
    if (this.page.visibilityState === "hidden") return;
    this.#timer = setTimeout(() => {
      this.#timer = undefined;
      if (this.#started) this.recover();
    }, 0);
  }
  notifyChange() {
    try {
      this.#channel?.postMessage({ type: "session-changed" });
    } catch {
      /* Focus recovery still works. */
    }
  }
  stop() {
    this.#started = false;
    clearTimeout(this.#timer);
    this.#timer = undefined;
    this.page.removeEventListener("visibilitychange", this.refocus);
    this.browser.removeEventListener("focus", this.refocus);
    this.browser.removeEventListener("pageshow", this.recheck);
    this.browser.removeEventListener("online", this.recheck);
    if (this.#channel) {
      this.#channel.onmessage = null;
      this.#channel.close();
      this.#channel = null;
    }
  }
}
