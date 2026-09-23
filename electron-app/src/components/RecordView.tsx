import { useEffect, useState } from 'react';
import api from '../api/client';
import type { ObserverDiagnostics as Diagnostics, TempoStatus } from '../api/contracts';
import type { RecordingController } from '../hooks/useRecording';
import DesktopPermissions from './DesktopPermissions';
import ExclusionSettingsPanel from './ExclusionSettingsPanel';
import ObserverDiagnostics from './ObserverDiagnostics';

type RecordViewProps = {
  status: TempoStatus;
  recording: RecordingController;
};

export default function RecordView({ status, recording }: RecordViewProps) {
  const { env, model, setModel, apiKey, setApiKey, configLoading, busy, contextAnswered, editContext } = recording;
  const [diagnostics, setDiagnostics] = useState<Diagnostics | null>(null);
  const [diagnosticsUnavailable, setDiagnosticsUnavailable] = useState(false);

  useEffect(() => {
    setDiagnostics(null);
    setDiagnosticsUnavailable(false);
    if (!status.running) return;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function pollDiagnostics() {
      const request = new AbortController();
      const abortRequest = () => request.abort();
      controller.signal.addEventListener('abort', abortRequest, { once: true });
      // A stuck server must not leave an old healthy verdict on screen.
      const timeout = setTimeout(abortRequest, 8_000);
      try {
        const next = await api.getObserverDiagnostics(request.signal);
        if (!controller.signal.aborted) {
          setDiagnostics(next);
          setDiagnosticsUnavailable(false);
        }
      } catch {
        if (!controller.signal.aborted) {
          setDiagnostics(null);
          setDiagnosticsUnavailable(true);
        }
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener('abort', abortRequest);
        // Schedule after completion so slow requests cannot overlap.
        if (!controller.signal.aborted) timer = setTimeout(() => void pollDiagnostics(), 10_000);
      }
    }
    void pollDiagnostics();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [status.running, status.started_at]);

  return (
    <div className="view-scroll record-view">
      <section className="record-hero">
        <div>
          <h1>Record</h1>
          <p className="hero-copy">
            {status.running
              ? 'Screenshots pause automatically for the applications and websites you exclude.'
              : 'Start recording or change your recording settings.'}
          </p>
        </div>
      </section>

      {status.running && (
        <ObserverDiagnostics diagnostics={diagnostics} unavailable={diagnosticsUnavailable} />
      )}

      <div className="record-settings">
        <section className="panel-card configuration-card about-card">
          <div className="panel-heading">
            <div>
              <h2>About you</h2>
            </div>
            {contextAnswered !== null && contextAnswered > 0 && (
              <span className="configured-chip">{contextAnswered} answered</span>
            )}
          </div>
          <p className="panel-description">
            Tell Tempo about your work, responsibilities, and goals. It uses
            your answers to interpret recorded activity.
          </p>
          <button
            type="button"
            className="secondary-button"
            onClick={editContext}
            disabled={busy !== null}
          >
            {contextAnswered ? 'Review or edit your answers' : 'Tell Tempo about yourself'}
          </button>
        </section>

        <section className="panel-card configuration-card">
          <div className="panel-heading">
            <div>
              <h2>AI model settings</h2>
            </div>
            {(env?.api_key_configured || env?.vertex_credentials_configured) && (
              <span className="configured-chip">{env.vertex_credentials_configured ? 'Service account configured' : 'API key configured'}</span>
            )}
          </div>
          <div className="configuration-fields">
            <label className="field-label">
              Model
              <input
                className="field-input"
                value={model}
                onChange={(event) => setModel(event.target.value)}
                disabled={status.running || configLoading || busy !== null}
              />
            </label>
            <label className="field-label">
              API key <span>optional if set in the environment</span>
              <input
                className="field-input"
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder={(env?.api_key_configured || env?.vertex_credentials_configured) ? 'Configured — leave blank to use it' : 'Used only for this recording session'}
                autoComplete="off"
                disabled={status.running || configLoading || busy !== null}
              />
            </label>
          </div>
          <div id="desktop-permissions" tabIndex={-1} aria-label="Capture permissions">
            <DesktopPermissions />
          </div>
        </section>
      </div>

      <ExclusionSettingsPanel />
    </div>
  );
}
