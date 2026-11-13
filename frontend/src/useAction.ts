import { useCallback, useRef, useState } from "react";

/**
 * Run one user action at a time: `pending` disables its button, repeated clicks
 * while it runs are ignored, and a failure is kept in `error` (with the server's
 * detail) instead of disappearing silently.
 */
export function useAction<A extends unknown[], R>(action: (...args: A) => Promise<R>) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const running = useRef(false);

  const run = useCallback(async (...args: A): Promise<R | undefined> => {
    if (running.current) return undefined;
    running.current = true;
    setPending(true);
    setError(null);
    try {
      return await action(...args);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
      return undefined;
    } finally {
      running.current = false;
      setPending(false);
    }
  }, [action]);

  return { run, pending, error };
}
