const { contextBridge, ipcRenderer } = require('electron');
import type { IpcRendererEvent } from 'electron';
import type {
  InstalledApp,
  RecordingState,
  ExclusionPauseReason,
  ScreenPermissionStatus,
  UnlockResult,
  ReviewSaveResult,
} from './shared/ipc';

export interface ElectronAPI {
  // --- Existing ---
  getServerURL: () => string;
  getPlatform: () => string;
  onServerReady: (callback: (event: IpcRendererEvent) => void) => void;
  onServerError: (callback: (error: string) => void) => void;
  removeListeners: () => void;

  // --- Permissions ---
  getPermissionStatus: () => Promise<ScreenPermissionStatus>;
  requestPermission: () => Promise<ScreenPermissionStatus>;

  getAccessibilityStatus: () => Promise<boolean>;
  requestAccessibility: () => Promise<boolean>;

  getAutomationStatus: () => Promise<boolean>;
  requestAutomation: () => Promise<boolean>;

  // --- Setup ---
  isSetupComplete: () => Promise<boolean>;
  completeSetup: () => Promise<void>;
  relaunch: () => Promise<void>;

  // --- App / URL exclusion ---
  getInstalledApps: () => Promise<InstalledApp[]>;
  getExcludedApps: () => Promise<string[]>;
  setExcludedApps: (apps: string[]) => Promise<string[]>;
  getExcludedUrls: () => Promise<string[]>;
  setExcludedUrls: (urls: string[]) => Promise<string[]>;
  getFavicon: (domain: string) => Promise<string | null>;


  // --- Recording control ---
  togglePause: () => Promise<RecordingState>;
  getRecordingState: () => Promise<RecordingState>;
  notifyRecordingStarted: () => Promise<void>;
  notifyRecordingStopped: () => Promise<void>;
  onManualPauseChanged: (callback: (isPaused: boolean) => void) => () => void;
  onExclusionPause: (callback: (reason: ExclusionPauseReason) => void) => () => void;
  onExclusionResume: (callback: () => void) => () => void;
  onStartRequested: (callback: () => void) => () => void;

  // --- Window size ---
  setCompactMode: () => Promise<void>;
  setDashboardMode: () => Promise<void>;

  // --- Dashboard unlock ---
  isDashboardUnlocked: () => Promise<boolean>;
  unlockDashboard: (passcode: string) => Promise<UnlockResult>;
  lockDashboard: () => Promise<UnlockResult>;

  // --- Review / Survey ---
  saveReviewResponse: (pid: string, key: string, data: unknown) => Promise<ReviewSaveResult>;
}

const api: ElectronAPI = {
  // --- Existing ---
  getServerURL: () => {
    return 'http://127.0.0.1:8756';
  },
  getPlatform: () => {
    const platform = process.platform;
    if (platform === 'darwin') return 'macos';
    if (platform === 'linux') return 'gnome';
    return 'macos';
  },
  onServerReady: (callback) => {
    ipcRenderer.on('server-ready', callback);
  },
  onServerError: (callback) => {
    ipcRenderer.on('server-error', (_event: IpcRendererEvent, error: string) => callback(error));
  },
  removeListeners: () => {
    ipcRenderer.removeAllListeners('server-ready');
    ipcRenderer.removeAllListeners('server-error');
    ipcRenderer.removeAllListeners('recording:do-start');
    ipcRenderer.removeAllListeners('recording:manual-pause-changed');
    ipcRenderer.removeAllListeners('recording:exclusion-pause');
    ipcRenderer.removeAllListeners('recording:exclusion-resume');
  },

  // --- Screen Recording Permissions ---
  getPermissionStatus: () => ipcRenderer.invoke('screen:get-permission-status'),
  requestPermission: () => ipcRenderer.invoke('screen:request-permission'),

  // --- Accessibility Permission ---
  getAccessibilityStatus: () => ipcRenderer.invoke('accessibility:get-status'),
  requestAccessibility: () => ipcRenderer.invoke('accessibility:request'),

  // --- Automation (System Events) Permission ---
  getAutomationStatus: () => ipcRenderer.invoke('automation:get-status'),
  requestAutomation: () => ipcRenderer.invoke('automation:request'),

  // --- Setup Flow ---
  isSetupComplete: () => ipcRenderer.invoke('setup:is-complete'),
  completeSetup: () => ipcRenderer.invoke('setup:complete'),
  relaunch: () => ipcRenderer.invoke('app:relaunch'),

  // --- App Exclusion ---
  getInstalledApps: () => ipcRenderer.invoke('apps:get-installed'),
  getExcludedApps: () => ipcRenderer.invoke('apps:get-excluded'),
  setExcludedApps: (apps) => ipcRenderer.invoke('apps:set-excluded', apps),

  // --- URL Exclusion ---
  getExcludedUrls: () => ipcRenderer.invoke('urls:get-excluded'),
  setExcludedUrls: (urls) => ipcRenderer.invoke('urls:set-excluded', urls),
  getFavicon: (domain) => ipcRenderer.invoke('urls:get-favicon', domain),


  // --- Recording Control ---
  togglePause: () => ipcRenderer.invoke('recording:toggle-pause'),
  getRecordingState: () => ipcRenderer.invoke('recording:get-state'),
  notifyRecordingStarted: () => ipcRenderer.invoke('recording:notify-started'),
  notifyRecordingStopped: () => ipcRenderer.invoke('recording:notify-stopped'),
  onManualPauseChanged: (callback) => {
    const handler = (_: IpcRendererEvent, isPaused: boolean) => callback(isPaused);
    ipcRenderer.on('recording:manual-pause-changed', handler);
    return () => ipcRenderer.removeListener('recording:manual-pause-changed', handler);
  },
  onExclusionPause: (callback) => {
    const handler = (_: IpcRendererEvent, reason: ExclusionPauseReason) => callback(reason);
    ipcRenderer.on('recording:exclusion-pause', handler);
    return () => ipcRenderer.removeListener('recording:exclusion-pause', handler);
  },
  onExclusionResume: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('recording:exclusion-resume', handler);
    return () => ipcRenderer.removeListener('recording:exclusion-resume', handler);
  },
  onStartRequested: (callback) => {
    const handler = () => callback();
    ipcRenderer.on('recording:do-start', handler);
    return () => ipcRenderer.removeListener('recording:do-start', handler);
  },

  // --- Window Size ---
  setCompactMode: () => ipcRenderer.invoke('window:set-compact'),
  setDashboardMode: () => ipcRenderer.invoke('window:set-dashboard'),

  // --- Dashboard Unlock ---
  isDashboardUnlocked: () => ipcRenderer.invoke('dashboard:is-unlocked'),
  unlockDashboard: (passcode) => ipcRenderer.invoke('dashboard:unlock', passcode),
  lockDashboard: () => ipcRenderer.invoke('dashboard:lock'),

  // --- Review / Survey ---
  saveReviewResponse: (pid, key, data) =>
    ipcRenderer.invoke('review:save-response', { pid, key, data }),
};

contextBridge.exposeInMainWorld('electron', api);
