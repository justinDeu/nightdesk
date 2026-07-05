import { useEffect, useState } from "react";

/** A clock that ticks every `intervalMs`, for live elapsed timers. Pauses when
 *  `active` is false so idle screens don't re-render each second. */
export function useNow(active = true, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [active, intervalMs]);
  return now;
}
