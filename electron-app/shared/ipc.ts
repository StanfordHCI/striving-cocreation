// Shared IPC channel types between main and renderer.
// Enumerated from preload.js / ipc-handlers.js.

export type ScreenPermissionStatus = 'not-determined' | 'granted' | 'denied' | 'restricted' | 'unknown';

export interface InstalledApp {
  bundle_id: string;
  name: string;
  icon_data_url: string | null;
  source: string;
}

export interface ExclusionSettings {
  apps: string[];
  domains: string[];
}

export interface RecordingState {
  isRecording: boolean;
  isPausedManually: boolean;
  isPausedByAppExclusion: boolean;
  isPausedByUrlExclusion: boolean;
}

export interface ExclusionPauseReason {
  type: 'app' | 'url';
  appName?: string;
  isSelf?: boolean;
}

export interface UnlockResult {
  success: boolean;
  error?: string;
}

export interface ReviewSaveResult {
  success: boolean;
  error?: string;
}

// ---- Channel maps ----

export interface InvokeChannels {
  // Permissions
  'screen:get-permission-status': { args: []; return: ScreenPermissionStatus };
  'screen:request-permission': { args: []; return: ScreenPermissionStatus };
  'accessibility:get-status': { args: []; return: boolean };
  'accessibility:request': { args: []; return: boolean };
  'automation:get-status': { args: []; return: boolean };
  'automation:request': { args: []; return: boolean };

  // Setup
  'setup:is-complete': { args: []; return: boolean };
  'setup:complete': { args: []; return: void };
  'app:relaunch': { args: []; return: void };

  // App exclusion
  'apps:get-installed': { args: []; return: InstalledApp[] };
  'apps:get-excluded': { args: []; return: string[] };
  'apps:set-excluded': { args: [string[]]; return: string[] };

  // URL exclusion
  'urls:get-excluded': { args: []; return: string[] };
  'urls:set-excluded': { args: [string[]]; return: string[] };
  'urls:get-favicon': { args: [string]; return: string | null };


  // Recording control
  'recording:toggle-pause': { args: []; return: RecordingState };
  'recording:get-state': { args: []; return: RecordingState };
  'recording:notify-started': { args: []; return: void };
  'recording:notify-stopped': { args: []; return: void };
  'recording:request-start': { args: []; return: void };

  // Window management
  'window:open-main': { args: []; return: void };
  'window:set-compact': { args: []; return: void };
  'window:set-dashboard': { args: []; return: void };

  // Dashboard unlock
  'dashboard:is-unlocked': { args: []; return: boolean };
  'dashboard:unlock': { args: [string]; return: UnlockResult };
  'dashboard:lock': { args: []; return: UnlockResult };

  // Review / Survey
  'review:save-response': { args: [{ pid: string; key: string; data: unknown }]; return: ReviewSaveResult };
}

export interface EventChannels {
  'server-ready': void;
  'server-error': string;
  'recording:do-start': void;
  'recording:manual-pause-changed': boolean;
  'recording:exclusion-pause': ExclusionPauseReason;
  'recording:exclusion-resume': void;
  'recording:started': void;
  'recording:stopped': void;
}

export type InvokeChannelName = keyof InvokeChannels;
export type EventChannelName = keyof EventChannels;
