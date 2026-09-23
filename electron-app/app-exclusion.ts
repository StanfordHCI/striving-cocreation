// Electron retains only temporary recording UI state. Active-app and browser
// exclusion policy is owned by the Python screen observer.
import { requestJson } from './backend-api';
import type { RecordingState } from './shared/ipc';

let isPausedManually = false;
let isRecording = false;

export async function setManualPause(paused: boolean): Promise<void> {
  const previous = isPausedManually;
  isPausedManually = paused;
  try {
    await requestJson('POST', paused ? '/api/stop' : '/api/start', paused ? undefined : {});
  } catch (error) {
    isPausedManually = previous;
    throw error;
  }
}

export function setRecording(recording: boolean): void {
  isRecording = recording;
  if (!recording) isPausedManually = false;
}

export function getRecordingState(): RecordingState {
  return {
    isRecording,
    isPausedManually,
    isPausedByAppExclusion: false,
    isPausedByUrlExclusion: false,
  };
}

// Kept as no-ops until the Phase 6 widget removal so existing window lifecycle
// calls remain harmless and do not own timers.
export function startActiveAppPolling(_window?: unknown): void {}
export function stopActiveAppPolling(): void {}
export function setMainWindow(_window?: unknown): void {}
