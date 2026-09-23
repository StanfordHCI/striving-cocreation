// URL matching and AppleScript browser inspection now live in tempo/exclusions.py.
// This local avatar is retained only for the legacy Phase 5 renderer.
export function getDomainAvatarDataUrl(domain: string): string {
  const normalized = String(domain || '').trim().toLowerCase();
  const letter = normalized.match(/[a-z0-9]/)?.[0]?.toUpperCase() || '?';
  const hue = Array.from(normalized).reduce((value: number, char: string) => {
    return (value * 31 + char.charCodeAt(0)) % 360;
  }, 210);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="hsl(${hue} 42% 42%)"/><text x="16" y="21" text-anchor="middle" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="15" font-weight="600" fill="white">${letter}</text></svg>`;
  return `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`;
}
