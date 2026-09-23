import { useEffect, useRef, useState } from 'react';
import {
  getDesktopPermissions,
  hasDesktopShell,
  requestDesktopPermission,
  type DesktopPermissionState,
} from '../platform/shell';

export default function DesktopPermissions() {
  const [permissions, setPermissions] = useState<DesktopPermissionState | null>(null);
  const [loading, setLoading] = useState(hasDesktopShell());
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);

  async function refresh() {
    if (mounted.current) setLoading(true);
    try {
      const next = await getDesktopPermissions();
      if (mounted.current) {
        setPermissions(next);
        setError(null);
      }
    } catch {
      if (mounted.current) setError('Desktop permission status is unavailable.');
    } finally {
      if (mounted.current) setLoading(false);
    }
  }

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; };
  }, []);
  if (!hasDesktopShell()) {
    return (
      <p className="permission-note">
        Browser mode uses the permissions of the terminal or service running Tempo.
      </p>
    );
  }
  if (loading && !permissions) return <p className="permission-note">Checking desktop permissions…</p>;
  if (error && !permissions) return <p className="permission-note">{error}</p>;

  return (
    <div className="permission-row">
      {(['screen', 'accessibility', 'automation'] as const).map((permission) => {
        const granted = Boolean(permissions?.[permission]);
        return (
          <button
            type="button"
            key={permission}
            className={granted ? 'permission-chip granted' : 'permission-chip'}
            onClick={async () => {
              try {
                if (!granted) await requestDesktopPermission(permission);
                await refresh();
              } catch {
                if (mounted.current) setError(`Unable to request ${permission} permission.`);
              }
            }}
          >
            <span>{granted ? '✓' : '○'}</span>
            {permission === 'screen' ? 'Screen recording' : permission}
          </button>
        );
      })}
    </div>
  );
}
