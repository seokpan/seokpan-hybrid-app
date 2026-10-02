import { useEffect, useState } from "react";
import { useSession } from "../session/context";
import type { SessionIdentity } from "../session/recovery";
import { parseRankings } from "./model";
import type { Rankings } from "./model";

export function useRankings(identity: SessionIdentity, offset = 0, limit = 20) {
  const { api } = useSession();
  const scope = JSON.stringify([
    identity.actor_type,
    identity.actor_id,
    identity.display_name,
    offset,
    limit,
  ]);
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{
    scope: string;
    data: Rankings | null;
    pending: boolean;
    error: unknown;
  }>({ scope, data: null, pending: true, error: null });
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState((previous) => ({
      scope,
      data: previous.scope === scope ? previous.data : null,
      pending: true,
      error: null,
    }));
    void api
      .request("/api/v1/rankings", "get", { query: { offset, limit }, signal: controller.signal })
      .then((value) => parseRankings(value, identity, offset, limit))
      .then((data) => {
        if (active) setState({ scope, data, pending: false, error: null });
      })
      .catch((error) => {
        if (active) setState((previous) => ({ ...previous, scope, pending: false, error }));
      });
    return () => {
      active = false;
      controller.abort();
    };
    // scope contains every identity field used by the parser, without room or Session tokens.
  }, [api, scope, attempt]);
  return {
    ...(state.scope === scope ? state : { data: null, pending: true, error: null }),
    refresh: () => setAttempt((value) => value + 1),
  };
}
