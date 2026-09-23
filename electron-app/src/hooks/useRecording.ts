import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api/client';
import type { EnvConfig, TempoStatus } from '../api/contracts';
import { getRuntimePlatform } from '../platform/shell';

type ContextFormReason = 'edit' | 'gate';
type RecordingAction = 'preparing' | 'starting' | 'stopping';

/** Shared recording controls and settings, retained when switching views. */
export default function useRecording(
  status: TempoStatus,
  connected: boolean,
  onStatus: (status: TempoStatus) => void,
) {
  const [env, setEnv] = useState<EnvConfig | null>(null);
  const [model, setModel] = useState('gemini-3.8-flash');
  const [apiKey, setApiKey] = useState('');
  const [configLoading, setConfigLoading] = useState(true);
  const [busy, setBusy] = useState<RecordingAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [contextAnswered, setContextAnswered] = useState<number | null>(null);
  const [contextAsked, setContextAsked] = useState(false);
  const [contextForm, setContextForm] = useState<ContextFormReason | null>(null);
  const pending = useRef(false);
  const dismissContextForm = useCallback(() => setContextForm(null), []);
  const editContext = useCallback(() => setContextForm('edit'), []);

  useEffect(() => {
    const controller = new AbortController();
    api.getEnvConfig(controller.signal).then((config) => {
      if (controller.signal.aborted) return;
      setEnv(config);
      setModel(config.model_name);
    }).catch(() => undefined).finally(() => {
      if (!controller.signal.aborted) setConfigLoading(false);
    });
    api.getOnboarding(controller.signal).then((data) => {
      if (controller.signal.aborted) return;
      setContextAnswered(Object.values(data.responses).filter((value) => value.trim()).length);
      setContextAsked(data.completed || data.dismissed);
    }).catch(() => undefined);
    return () => controller.abort();
  }, []);

  async function refreshContextCount() {
    try {
      const data = await api.getOnboarding();
      setContextAnswered(Object.values(data.responses).filter((value) => value.trim()).length);
      setContextAsked(data.completed || data.dismissed);
    } catch {
      // The counter does not affect an explicitly requested recording.
    }
  }

  async function changeRecording(start: boolean) {
    setBusy(start ? 'starting' : 'stopping');
    if (start) {
      await api.start({
        model_name: model.trim() || undefined,
        platform: getRuntimePlatform(),
        api_key: apiKey.trim() || undefined,
        debug: false,
      });
      setApiKey('');
    } else {
      await api.stop();
    }
    onStatus(await api.getStatus());
  }

  async function toggleRecording() {
    if (pending.current || !connected || configLoading || contextForm) return;
    pending.current = true;
    setBusy(status.running ? 'stopping' : 'preparing');
    setError(null);
    try {
      if (!status.running) {
        let asked = contextAsked;
        if (contextAnswered === null) {
          try {
            const data = await api.getOnboarding();
            setContextAnswered(Object.values(data.responses).filter((value) => value.trim()).length);
            asked = data.completed || data.dismissed;
            setContextAsked(asked);
          } catch {
            asked = true; // Preserve recording access when optional context is unavailable.
          }
        }
        if (!asked) {
          setContextForm('gate');
          return;
        }
      }
      await changeRecording(!status.running);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Recording control failed');
    } finally {
      pending.current = false;
      setBusy(null);
    }
  }

  async function completeContextForm() {
    if (!contextForm || pending.current) return;
    const start = contextForm === 'gate';
    setContextForm(null);
    if (!start) {
      void refreshContextCount();
      return;
    }
    pending.current = true;
    setError(null);
    setContextAsked(true);
    try {
      await changeRecording(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Recording control failed');
    } finally {
      pending.current = false;
      setBusy(null);
      void refreshContextCount();
    }
  }

  return {
    env, model, setModel, apiKey, setApiKey, configLoading, busy, error,
    contextAnswered, contextForm, editContext, dismissContextForm,
    completeContextForm, toggleRecording,
  };
}

export type RecordingController = ReturnType<typeof useRecording>;
