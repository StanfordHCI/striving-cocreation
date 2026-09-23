// Temporary Electron compatibility bridge. Phase 6's browser UI calls these
// endpoints directly; keeping the bridge here avoids two settings stores now.
import http from 'node:http';
const SERVER_URL = 'http://127.0.0.1:8756';

export function requestJson<T = unknown>(
  method: string,
  endpoint: string,
  payload?: unknown,
  timeoutMs = 5000,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const body = payload === undefined ? null : JSON.stringify(payload);
    const url = new URL(endpoint, SERVER_URL);
    const request = http.request(url, {
      method,
      timeout: timeoutMs,
      headers: body ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } : {},
    }, (response) => {
      let raw = '';
      response.setEncoding('utf8');
      response.on('data', (chunk) => { raw += chunk; });
      response.on('end', () => {
        if ((response.statusCode || 500) >= 400) {
          reject(new Error(`Tempo API ${method} ${endpoint} returned ${response.statusCode}`));
          return;
        }
        try {
          resolve((raw ? JSON.parse(raw) : null) as T);
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on('error', reject);
    request.on('timeout', () => request.destroy(new Error('Tempo API request timed out')));
    if (body) request.write(body);
    request.end();
  });
}
