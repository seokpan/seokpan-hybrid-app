import type { Socket, SocketFactory } from "../realtime/stream";
import { parseChat } from "./model";
import type { ChatMessage, ChatScope } from "./model";

type ChatView = {
  phase: "connecting" | "ready" | "closed";
  messages: ChatMessage[];
  notice: string;
};
/** Transient chat only: never mutates the Room/Game stream or replays commands. */
export class ChatStream {
  #view: ChatView = { phase: "connecting", messages: [], notice: "채팅을 연결하고 있습니다." };
  #listeners = new Set<() => void>();
  #socket: Socket | null = null;
  #timer: ReturnType<typeof setTimeout> | undefined;
  constructor(
    private readonly roomId: ChatScope,
    private readonly factory: SocketFactory,
  ) {}
  getSnapshot = () => this.#view;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #publish(view: ChatView) {
    this.#view = view;
    this.#listeners.forEach((fn) => fn());
  }
  stop = () => {
    clearTimeout(this.#timer);
    this.#timer = undefined;
    const socket = this.#socket;
    this.#socket = null;
    if (socket) {
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      socket.close();
    }
  };
  #close(notice: string) {
    this.stop();
    this.#publish({ phase: "closed", messages: [], notice });
  }
  start = () => {
    this.stop();
    this.#publish({
      phase: "connecting",
      messages: [],
      notice: "연결 이후 대화만 표시합니다. 이전 대화는 보관하지 않습니다.",
    });
    // Avoid opening a disposable StrictMode connection.
    this.#timer = setTimeout(() => this.#connect(), 0);
  };
  #connect() {
    this.#timer = undefined;
    let socket: Socket;
    try {
      socket = this.factory(
        this.roomId === null ? "/ws/v1/chat/lobby" : `/ws/v1/chat/rooms/${this.roomId}`,
      );
    } catch {
      this.#close("채팅 연결을 열 수 없습니다. 다시 연결해 주세요.");
      return;
    }
    this.#socket = socket;
    this.#timer = setTimeout(
      () => this.#close("채팅 연결 응답이 없습니다. 다시 연결해 주세요."),
      15_000,
    );
    socket.onmessage = (event) => {
      if (socket !== this.#socket) return;
      try {
        const message = parseChat(event.data, this.roomId);
        if (this.#view.phase === "connecting") {
          if (message !== null) throw new Error("READY_REQUIRED");
          clearTimeout(this.#timer);
          this.#timer = undefined;
          this.#publish({ ...this.#view, phase: "ready" });
          return;
        }
        if (message === null) throw new Error("REPEATED_READY");
        const duplicate = this.#view.messages.find((item) => item.id === message.id);
        if (duplicate) {
          if (JSON.stringify(duplicate) !== JSON.stringify(message)) throw new Error("REUSED_ID");
          return;
        }
        this.#publish({ ...this.#view, messages: [...this.#view.messages.slice(-99), message] });
      } catch {
        this.#close("채팅 응답을 확인할 수 없어 채팅만 중단했습니다. 다시 연결해 주세요.");
      }
    };
    socket.onerror = () => {
      if (socket === this.#socket)
        this.#close("채팅 연결에 문제가 생겼습니다. 다시 연결해 주세요.");
    };
    socket.onclose = (event) => {
      if (socket !== this.#socket) return;
      this.#close(
        [4401, 4403].includes(event.code)
          ? "채팅 접근이 종료되었습니다. 로그인·방 참여 상태를 확인한 뒤 다시 연결해 주세요."
          : "채팅 연결이 끊겼습니다. 끊긴 동안의 대화는 복구되지 않습니다.",
      );
    };
  }
}
