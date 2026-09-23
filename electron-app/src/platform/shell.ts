export type DesktopPermissionState = {
  screen: boolean;
  accessibility: boolean;
  automation: boolean;
};

function electronShell() {
  return typeof window === 'undefined' ? undefined : window.electron;
}

export function getServerBaseUrl(): string {
  const configured = electronShell()?.getServerURL?.();
  if (configured) return configured.replace(/\/$/, '');
  if (
    typeof window !== 'undefined'
    && ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(window.location.hostname)
    && window.location.port !== '5173'
  ) {
    return window.location.origin;
  }
  return 'http://127.0.0.1:8756';
}

export function getRuntimePlatform(): 'macos' | 'gnome' {
  const shellPlatform = electronShell()?.getPlatform?.();
  if (shellPlatform === 'gnome') return 'gnome';
  if (shellPlatform === 'macos') return 'macos';
  if (typeof navigator !== 'undefined' && /linux/i.test(navigator.userAgent)) return 'gnome';
  return 'macos';
}

export function hasDesktopShell(): boolean {
  return Boolean(electronShell());
}

export async function getDesktopPermissions(): Promise<DesktopPermissionState | null> {
  const shell = electronShell();
  if (!shell) return null;
  const [screen, accessibility, automation] = await Promise.all([
    shell.getPermissionStatus(),
    shell.getAccessibilityStatus(),
    shell.getAutomationStatus(),
  ]);
  return {
    screen: screen === 'granted',
    accessibility,
    automation,
  };
}

export async function requestDesktopPermission(
  permission: keyof DesktopPermissionState,
): Promise<void> {
  const shell = electronShell();
  if (!shell) return;
  if (permission === 'screen') await shell.requestPermission();
  if (permission === 'accessibility') await shell.requestAccessibility();
  if (permission === 'automation') await shell.requestAutomation();
}
