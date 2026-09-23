// Registers all ipcMain.handle() handlers for the Tempo Electron app.
// Covers: permissions, app/URL exclusion, setup flow, dashboard unlock.
import { app, BrowserWindow, desktopCapturer, ipcMain, screen, shell, systemPreferences } from 'electron';
import type { IpcMainInvokeEvent } from 'electron';
import { execFile } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { setManualPause, setRecording, setMainWindow, getRecordingState } from './app-exclusion';
import { getDomainAvatarDataUrl } from './url-exclusion';
import { requestJson } from './backend-api';
import type { ExclusionSettings } from './shared/ipc';

// --- Paths ---
const setupFlagPath = path.join(app.getPath('userData'), '.setup-complete');
const unlockFlagPath = path.join(app.getPath('userData'), 'dashboard-unlocked.json');
const reviewResponsesPath = path.join(app.getPath('userData'), 'review-responses.json');

// Dashboard unlock passcode — research team knows this, participants don't
const UNLOCK_PASSCODE = 'tempo2026';

// --- State ---
let mainWindowRef: BrowserWindow | null = null;

// --- Setup flag ---
export function isSetupComplete(): boolean {
  return fs.existsSync(setupFlagPath);
}

function markSetupComplete() {
  fs.writeFileSync(setupFlagPath, '1');
}

// --- Dashboard unlock ---
export function isDashboardUnlocked(): boolean {
  try {
    const data = JSON.parse(fs.readFileSync(unlockFlagPath, 'utf-8'));
    return data.unlocked === true;
  } catch {
    return false;
  }
}

// --- Permission helpers ---
function getScreenPermissionStatus() {
  return systemPreferences.getMediaAccessStatus('screen');
}

function getAccessibilityStatus() {
  return systemPreferences.isTrustedAccessibilityClient(false);
}

// --- Register all handlers ---
export function registerHandlers(mainWindow: BrowserWindow): void {
  mainWindowRef = mainWindow;
  setMainWindow(mainWindow);

  // --- Screen Recording Permissions ---
  ipcMain.handle('screen:get-permission-status', () => {
    return getScreenPermissionStatus();
  });

  ipcMain.handle('screen:request-permission', async () => {
    const status = getScreenPermissionStatus();
    if (status === 'granted') return 'granted';
    try {
      await desktopCapturer.getSources({ types: ['screen'] });
    } catch (_) {}
    if (getScreenPermissionStatus() !== 'granted') {
      shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
    }
    return getScreenPermissionStatus();
  });

  // --- Accessibility Permission ---
  ipcMain.handle('accessibility:get-status', () => {
    return getAccessibilityStatus();
  });

  ipcMain.handle('accessibility:request', () => {
    return systemPreferences.isTrustedAccessibilityClient(true);
  });

  // --- Automation (System Events) Permission ---
  ipcMain.handle('automation:get-status', async () => {
    return new Promise<boolean>((resolve) => {
      const script = 'tell application "System Events" to get name of first application process whose frontmost is true';
      execFile('osascript', ['-e', script], { timeout: 5000 }, (err) => {
        resolve(!err);
      });
    });
  });

  ipcMain.handle('automation:request', async () => {
    return new Promise<boolean>((resolve) => {
      const script = 'tell application "System Events" to get name of first application process whose frontmost is true';
      execFile('osascript', ['-e', script], { timeout: 10000 }, (err) => {
        resolve(!err);
      });
    });
  });

  // --- App Exclusion ---
  ipcMain.handle('apps:get-installed', async () => {
    try {
      return await requestJson('GET', '/api/apps/installed', undefined, 30000);
    } catch {
      return [];
    }
  });

  ipcMain.handle('apps:get-excluded', async () => {
    const settings = await requestJson<ExclusionSettings>('GET', '/api/settings/exclusions');
    return settings.apps;
  });

  ipcMain.handle('apps:set-excluded', async (_event: IpcMainInvokeEvent, apps: string[]) => {
    const settings = await requestJson<ExclusionSettings>('GET', '/api/settings/exclusions');
    const updated = await requestJson<ExclusionSettings>('PUT', '/api/settings/exclusions', { ...settings, apps });
    return updated.apps;
  });

  // --- URL Exclusion ---
  ipcMain.handle('urls:get-excluded', async () => {
    const settings = await requestJson<ExclusionSettings>('GET', '/api/settings/exclusions');
    return settings.domains;
  });

  ipcMain.handle('urls:set-excluded', async (_event: IpcMainInvokeEvent, urls: string[]) => {
    const settings = await requestJson<ExclusionSettings>('GET', '/api/settings/exclusions');
    const updated = await requestJson<ExclusionSettings>('PUT', '/api/settings/exclusions', { ...settings, domains: urls });
    return updated.domains;
  });

  ipcMain.handle('urls:get-favicon', async (_event: IpcMainInvokeEvent, domain: string) => {
    return getDomainAvatarDataUrl(domain);
  });

  // --- Setup Flow ---
  ipcMain.handle('setup:is-complete', () => {
    return isSetupComplete();
  });

  ipcMain.handle('setup:complete', () => {
    markSetupComplete();
  });

  // --- Window Management ---
  ipcMain.handle('window:open-main', () => {
    if (mainWindowRef && !mainWindowRef.isDestroyed()) {
      mainWindowRef.show();
      mainWindowRef.focus();
    }
  });

  ipcMain.handle('window:set-compact', () => {
    if (!mainWindowRef || mainWindowRef.isDestroyed()) return;
    const display = screen.getPrimaryDisplay();
    const { x, y, width: sw, height: sh } = display.workArea;
    const w = 480, h = 660;
    mainWindowRef.setResizable(true); // must be resizable before setBounds
    mainWindowRef.setBounds({
      x: x + Math.round((sw - w) / 2),
      y: y + Math.round((sh - h) / 2),
      width: w,
      height: h,
    }, true);
    mainWindowRef.setResizable(false);
  });

  ipcMain.handle('window:set-dashboard', () => {
    if (!mainWindowRef || mainWindowRef.isDestroyed()) return;
    const display = screen.getPrimaryDisplay();
    const { x, y, width: sw, height: sh } = display.workArea;
    const w = 1400, h = 900;
    mainWindowRef.setResizable(true);
    mainWindowRef.setBounds({
      x: x + Math.round((sw - w) / 2),
      y: y + Math.round((sh - h) / 2),
      width: w,
      height: h,
    }, true);
  });

  // --- Recording Control ---
  ipcMain.handle('recording:toggle-pause', async () => {
    const state = getRecordingState();
    await setManualPause(!state.isPausedManually);
    const newState = getRecordingState();
    if (mainWindowRef && !mainWindowRef.isDestroyed()) {
      mainWindowRef.webContents.send('recording:manual-pause-changed', newState.isPausedManually);
    }
    return newState;
  });

  ipcMain.handle('recording:get-state', () => {
    return getRecordingState();
  });

  // --- Recording Start/Stop Notifications ---
  ipcMain.handle('recording:notify-started', () => {
    setRecording(true);
  });

  ipcMain.handle('recording:notify-stopped', () => {
    setRecording(false);
  });

  // --- Dashboard Unlock ---
  ipcMain.handle('dashboard:is-unlocked', () => {
    return isDashboardUnlocked();
  });

  ipcMain.handle('dashboard:unlock', (_event: IpcMainInvokeEvent, passcode: string) => {
    if (passcode === UNLOCK_PASSCODE) {
      fs.writeFileSync(unlockFlagPath, JSON.stringify({
        unlocked: true,
        unlocked_at: new Date().toISOString(),
      }));
      return { success: true };
    }
    return { success: false, error: 'Invalid passcode' };
  });

  ipcMain.handle('dashboard:lock', () => {
    fs.writeFileSync(unlockFlagPath, JSON.stringify({
      unlocked: false,
      locked_at: new Date().toISOString(),
    }));
    return { success: true };
  });

  // --- Review / Survey Responses ---
  ipcMain.handle('review:save-response', async (
    _event: IpcMainInvokeEvent,
    { pid, key, data }: { pid: string; key: string; data: unknown },
  ) => {
    try {
      let responses: unknown[] = [];
      try {
        const saved = JSON.parse(fs.readFileSync(reviewResponsesPath, 'utf-8'));
        if (Array.isArray(saved)) responses = saved;
      } catch (_) {}
      const participant = String(pid || '').trim() || 'anonymous';
      const responseKey = String(key || '').trim();
      if (!responseKey) return { success: false, error: 'Missing response key' };
      responses.push({
        participant,
        key: responseKey,
        data,
        savedAt: new Date().toISOString(),
      });
      fs.writeFileSync(reviewResponsesPath, JSON.stringify(responses, null, 2));
      return { success: true };
    } catch (err: unknown) {
      console.error('[review] Failed to save response locally:', err instanceof Error ? err.message : String(err));
      return { success: false, error: 'Failed to save response locally' };
    }
  });

  // --- App Lifecycle ---
  ipcMain.handle('app:relaunch', () => {
    app.relaunch();
    app.exit(0);
  });
}
