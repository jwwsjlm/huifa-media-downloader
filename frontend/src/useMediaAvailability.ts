import { useCallback, useEffect, useRef, useState } from 'react';
import { host } from './transport';

export type MediaAvailability = 'available' | 'missing' | 'unknown';
type Result = { id: string; status: MediaAvailability };
const BATCH_SIZE = 128;
const visible = () => document.visibilityState !== 'hidden';

export function useMediaAvailability(ids: string[], enabled = true) {
  const key = JSON.stringify([...new Set(ids.filter(Boolean))].sort());
  const [availability, setAvailability] = useState<Record<string, MediaAvailability>>({});
  const mounted = useRef(false);
  const target = useRef({ ids: [] as string[], epoch: 0, enabled: false });
  const request = useRef<Promise<void> | null>(null);
  const pending = useRef(false);
  const refresh = useCallback(function refresh() {
    if (!mounted.current || !target.current.enabled || !visible() || !target.current.ids.length) return;
    pending.current = true;
    if (request.current) return;
    const run = async () => {
      while (pending.current && mounted.current && target.current.enabled && visible()) {
        pending.current = false;
        const current = target.current;
        for (let offset = 0; offset < current.ids.length; offset += BATCH_SIZE) {
          const batch = current.ids.slice(offset, offset + BATCH_SIZE);
          let result: Result[] = [];
          try {
            const rows = await host.command<Result[]>('media.availability', { ids: batch });
            if (Array.isArray(rows)) result = rows;
          } catch { /* An unavailable service cannot establish that a file is missing. */ }
          if (!mounted.current || !target.current.enabled || current.epoch !== target.current.epoch) break;
          const values: Record<string, MediaAvailability> = Object.fromEntries(batch.map(id => [id, 'unknown']));
          for (const row of result) {
            if (row && Object.hasOwn(values, row.id) && ['available', 'missing', 'unknown'].includes(row.status)) values[row.id] = row.status;
          }
          setAvailability(previous => Object.entries(values).some(([id, status]) => previous[id] !== status) ? { ...previous, ...values } : previous);
          if (!visible()) break;
        }
      }
    };
    request.current = run().finally(() => {
      request.current = null;
      if (pending.current) refresh();
    });
  }, []);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; target.current = { ids: [], epoch: target.current.epoch + 1, enabled: false }; pending.current = false; };
  }, []);
  useEffect(() => {
    target.current = { ids: JSON.parse(key) as string[], epoch: target.current.epoch + 1, enabled };
    refresh();
    return () => { target.current = { ...target.current, epoch: target.current.epoch + 1, enabled: false }; pending.current = false; };
  }, [key, enabled, refresh]);
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
  }, [enabled, refresh]);
  return { availability, refresh };
}
