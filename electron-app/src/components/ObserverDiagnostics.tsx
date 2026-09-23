import type { ObserverDiagnostics as Diagnostics } from '../api/contracts';

type ObserverDiagnosticsProps = {
  diagnostics: Diagnostics | null;
  unavailable: boolean;
};

export default function ObserverDiagnostics({ diagnostics, unavailable }: ObserverDiagnosticsProps) {
  const active = diagnostics && 'verdict' in diagnostics ? diagnostics : null;
  const verdict = unavailable
    ? 'Capture status is unavailable. Retrying…'
    : active?.verdict ?? (diagnostics && 'detail' in diagnostics
      ? diagnostics.detail
      : 'Checking capture…');
  const healthy = !unavailable && active?.health.state === 'healthy';
  const quiet = !unavailable && (
    !diagnostics || (active !== null && !['stalled', 'error'].includes(active.health.state))
  );

  return (
    <div className={`capture-status ${quiet ? 'quiet' : 'notice'}`} role="status" aria-live="polite">
      <p>
        {verdict}
        {healthy && active && (
          <> {active.screenshots_captured.toLocaleString()} saved {active.screenshots_captured === 1 ? 'screenshot' : 'screenshots'}.</>
        )}
      </p>
      {!unavailable && verdict.includes('Accessibility') && (
        <a href="#desktop-permissions">Check capture permissions</a>
      )}
    </div>
  );
}
