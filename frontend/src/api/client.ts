import type { paths } from "./schema";

type Method = "get" | "post" | "put" | "delete" | "patch";
type ApiPath = Extract<keyof paths, `/api/v1/${string}`>;
type AllowedMethod<P extends ApiPath> = {
  [M in Method]: NonNullable<paths[P][M]> extends never ? never : M;
}[Method];
type Operation<P extends ApiPath, M extends AllowedMethod<P>> = NonNullable<paths[P][M]>;
type Body<O> = O extends { requestBody: { content: { "application/json": infer B } } } ? B : never;
type PathOptions<O> = O extends { parameters: { path: infer P } } ? { path: P } : { path?: never };
type BodyOptions<O> = [Body<O>] extends [never] ? { body?: never } : { body: Body<O> };
type JsonContent<R> = R extends { content: { "application/json": infer J } } ? J : void;
type Result<O> = O extends { responses: infer R }
  ? JsonContent<R[Extract<keyof R, 200 | 201 | 204>]>
  : never;
type QueryOptions<O> = O extends { parameters: { query?: infer Q } }
  ? { query?: Q }
  : { query?: never };
type Options<O> = PathOptions<O> & BodyOptions<O> & QueryOptions<O> & { signal?: AbortSignal };

export type FailureKind = "http" | "network" | "aborted" | "timeout" | "invalid-response";

export class ApiFailure extends Error {
  constructor(
    readonly kind: FailureKind,
    readonly status: number | null = null,
    readonly code = "REQUEST_FAILED",
    readonly requestId: string | null = null,
    readonly currentVersion: number | null = null,
  ) {
    // Raw server text, URL, request body and tokens never become an error message.
    super(code);
    this.name = "ApiFailure";
  }
}

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** Same-origin JSON transport. No automatic retries, including after CSRF recovery. */
export class ApiClient {
  #csrf: string | null = null;
  #credentials = 0;
  #authFailures = new Set<() => void>();
  subscribeAuthFailure(listener: () => void) {
    this.#authFailures.add(listener);
    return () => {
      this.#authFailures.delete(listener);
    };
  }

  constructor(
    private readonly fetcher: typeof fetch = (...args) => fetch(...args),
    private readonly timeoutMs = 15_000,
  ) {}

  setCsrf(value: string | null): void {
    this.#credentials++;
    this.#csrf = value;
  }

  async request<P extends ApiPath, M extends AllowedMethod<P>>(
    template: P,
    method: M,
    options: Options<Operation<P, M>>,
  ): Promise<Result<Operation<P, M>>> {
    const parameters = object(options.path);
    const path = template.replace(/\{([^}]+)\}/g, (_match, key: string) => {
      const value = parameters[key];
      if (typeof value !== "string" && typeof value !== "number") {
        throw new Error("API_PATH_PARAMETER_REQUIRED");
      }
      return encodeURIComponent(String(value));
    });
    if (!path.startsWith("/api/v1/") || /[\\?#\r\n]/.test(path)) {
      throw new Error("INVALID_API_PATH");
    }
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(object(options.query))) {
      if (value === undefined) continue;
      if (
        !["string", "number", "boolean"].includes(typeof value) ||
        (typeof value === "number" && !Number.isFinite(value))
      )
        throw new Error("INVALID_API_QUERY");
      query.set(key, String(value));
    }
    const url = query.size ? `${path}?${query.toString()}` : path;
    const headers = new Headers({ Accept: "application/json, application/problem+json" });
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (method !== "get") {
      if (this.#csrf !== null) headers.set("X-CSRF-Token", this.#csrf);
      if (template === "/api/v1/session/csrf") headers.set("X-CSRF-Bootstrap", "1");
    }
    const controller = new AbortController();
    const credentials = this.#credentials;
    const abort = () => controller.abort();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.timeoutMs);
    options.signal?.addEventListener("abort", abort, { once: true });
    if (options.signal?.aborted) controller.abort();
    try {
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      const response = await this.fetcher(url, {
        method: method.toUpperCase(),
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        credentials: "same-origin",
        mode: "same-origin",
        redirect: "error",
        cache: "no-store",
        signal: controller.signal,
      });
      if (response.status === 204 && response.ok) return undefined as Result<Operation<P, M>>;
      const contentType = response.headers.get("Content-Type")?.split(";")[0].trim();
      const isJson =
        contentType === "application/json" || contentType === "application/problem+json";
      let value: unknown;
      try {
        value = isJson ? await response.json() : undefined;
      } catch (error) {
        if (controller.signal.aborted) throw error;
        value = undefined;
      }
      if (!response.ok) {
        const problem = object(value);
        if (
          template !== "/api/v1/session/csrf" &&
          credentials === this.#credentials &&
          !controller.signal.aborted &&
          ((response.status === 401 && problem.code !== "AUTH_INVALID_CREDENTIALS") ||
            (response.status === 403 && problem.code === "CSRF_INVALID"))
        ) {
          this.#authFailures.forEach((listener) => listener());
        }
        throw new ApiFailure(
          "http",
          response.status,
          typeof problem.code === "string" && /^[A-Z][A-Z0-9_]{0,79}$/.test(problem.code)
            ? problem.code
            : "REQUEST_FAILED",
          typeof problem.request_id === "string" &&
            /^[A-Za-z0-9._-]{1,64}$/.test(problem.request_id)
            ? problem.request_id
            : null,
          typeof problem.current_version === "number" &&
            Number.isSafeInteger(problem.current_version) &&
            problem.current_version >= 1
            ? problem.current_version
            : null,
        );
      }
      if (contentType !== "application/json" || value === undefined) {
        throw new ApiFailure("invalid-response", response.status, "INVALID_API_RESPONSE");
      }
      // Generated types are compile-time checks. Consumers validate their required fields.
      return value as Result<Operation<P, M>>;
    } catch (error) {
      if (error instanceof ApiFailure) throw error;
      if (timedOut) throw new ApiFailure("timeout", null, "REQUEST_TIMEOUT");
      if (controller.signal.aborted) throw new ApiFailure("aborted", null, "REQUEST_ABORTED");
      throw new ApiFailure("network", null, "NETWORK_UNAVAILABLE");
    } finally {
      clearTimeout(timer);
      options.signal?.removeEventListener("abort", abort);
    }
  }
}
