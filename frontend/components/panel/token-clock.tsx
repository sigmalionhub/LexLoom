import { useEffect, useState } from 'react';

/** The demo token really does expire; `expires_in` from the login response says when. */
export function TokenClock({ expiresAt }: { expiresAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const remaining = Math.max(0, Math.round((expiresAt - now) / 1000));
  if (remaining === 0) return <span className="text-alarm">expired</span>;
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  return (
    <span className={remaining < 120 ? 'text-alarm' : undefined}>
      valid {minutes}:{String(seconds).padStart(2, '0')}
    </span>
  );
}
