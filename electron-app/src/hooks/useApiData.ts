import { useCallback, useEffect, useRef, useState, type DependencyList } from 'react';

export type UseApiDataResult<T> = {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refetch: () => void;
};

/**
 * Shared loading/data/error/cleanup hook.
 *
 * Replaces the ad-hoc `cancelled` flag pattern repeated across components:
 *   - Calls `fetcher` on mount and whenever `deps` change
 *   - Tracks an internal "request id" so a stale resolution from a previous
 *     fetch can never overwrite newer state
 *   - On error, leaves the previous `data` intact (UIs can keep showing the
 *     last good payload while a refetch fails)
 *   - `refetch()` runs an additional fetch with the current `fetcher`
 *   - SSR-safe: no top-level `window` access, no effects during render
 */
export function useApiData<T>(
  fetcher: () => Promise<T>,
  deps: DependencyList = []
): UseApiDataResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);

  // Latest fetcher held in a ref so refetch() always uses the current one
  // without forcing it into the dep array.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  // Monotonic request id; the most recent value wins.
  const reqIdRef = useRef(0);
  // Bumped manually by refetch() to retrigger the effect.
  const [refetchTick, setRefetchTick] = useState(0);

  useEffect(() => {
    const reqId = ++reqIdRef.current;
    let active = true;

    setLoading(true);
    setError(null);

    fetcherRef
      .current()
      .then((result) => {
        if (!active || reqId !== reqIdRef.current) return;
        setData(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (!active || reqId !== reqIdRef.current) return;
        setError(err instanceof Error ? err : new Error(String(err)));
        setLoading(false);
      });

    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, refetchTick]);

  const refetch = useCallback(() => {
    setRefetchTick((n) => n + 1);
  }, []);

  return { data, loading, error, refetch };
}

export default useApiData;
