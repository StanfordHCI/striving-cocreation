import { useEffect, useMemo, useState } from 'react';
import api from '../api/client';
import type { ExclusionSettings, InstalledApp } from '../api/contracts';

type AppFilter = 'installed' | 'apple' | 'excluded';

function normalizeDomain(input: string): string | null {
  const raw = input.trim().toLowerCase();
  if (!raw) return null;
  try {
    const parsed = new URL(raw.includes('://') ? raw : `https://${raw}`);
    const domain = parsed.hostname.replace(/^www\./, '').replace(/\.$/, '');
    if (!domain || !domain.includes('.') || /[^a-z0-9.-]/.test(domain)) return null;
    return domain;
  } catch {
    return null;
  }
}

export default function ExclusionSettingsPanel() {
  const [apps, setApps] = useState<InstalledApp[]>([]);
  const [settings, setSettings] = useState<ExclusionSettings>({ apps: [], domains: [] });
  const [filter, setFilter] = useState<AppFilter>('installed');
  const [search, setSearch] = useState('');
  const [domain, setDomain] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api.getInstalledApps(controller.signal),
      api.getExclusions(controller.signal),
    ]).then(([installed, exclusions]) => {
      setApps(installed);
      setSettings(exclusions);
      setLoading(false);
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : 'Unable to load exclusions');
      setLoading(false);
    });
    return () => controller.abort();
  }, []);

  const visibleApps = useMemo(() => {
    const query = search.trim().toLowerCase();
    return apps.filter((app) => {
      const selected = settings.apps.includes(app.bundle_id);
      const inFilter = filter === 'excluded'
        ? selected
        : filter === 'apple'
          ? app.source === 'system' || app.source === 'utilities'
          : app.source === 'applications' || app.source === 'user';
      return inFilter && (!query || app.name.toLowerCase().includes(query));
    });
  }, [apps, filter, search, settings.apps]);

  async function save(next: ExclusionSettings) {
    const previous = settings;
    setSettings(next);
    setSaving(true);
    setError(null);
    try {
      setSettings(await api.updateExclusions(next));
    } catch (reason) {
      setSettings(previous);
      setError(reason instanceof Error ? reason.message : 'Unable to save exclusions');
    } finally {
      setSaving(false);
    }
  }

  function toggleApp(bundleId: string) {
    const selected = settings.apps.includes(bundleId);
    void save({
      ...settings,
      apps: selected
        ? settings.apps.filter((item) => item !== bundleId)
        : [...settings.apps, bundleId],
    });
  }

  function addDomain() {
    const normalized = normalizeDomain(domain);
    if (!normalized) {
      setError('Enter a valid domain, such as example.com.');
      return;
    }
    setDomain('');
    if (settings.domains.includes(normalized)) return;
    void save({ ...settings, domains: [...settings.domains, normalized] });
  }

  if (loading) return <div className="panel-state">Loading installed apps…</div>;

  return (
    <section className="exclusion-grid" aria-label="Recording exclusions">
      <div className="panel-card app-picker-card">
        <div className="panel-heading">
          <div>
            <h2>Excluded apps</h2>
          </div>
          <span className="count-chip">{settings.apps.length} excluded</span>
        </div>
        <div className="filter-row" role="tablist" aria-label="Application filters">
          {(['installed', 'apple', 'excluded'] as const).map((item) => (
            <button
              key={item}
              type="button"
              className={filter === item ? 'filter-tab active' : 'filter-tab'}
              onClick={() => setFilter(item)}
            >
              {item === 'apple' ? 'Apple apps' : item[0].toUpperCase() + item.slice(1)}
            </button>
          ))}
        </div>
        <input
          className="field-input app-search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search applications"
          aria-label="Search applications"
        />
        <div className="app-list">
          {visibleApps.length === 0 ? (
            <p className="empty-copy">No applications match this filter.</p>
          ) : visibleApps.map((app) => {
            const selected = settings.apps.includes(app.bundle_id);
            return (
              <label className={selected ? 'app-row selected' : 'app-row'} key={app.bundle_id}>
                {app.icon_data_url ? (
                  <img src={app.icon_data_url} alt="" className="app-icon" />
                ) : (
                  <span className="app-icon app-icon-fallback">{app.name.slice(0, 1)}</span>
                )}
                <span className="app-copy">
                  <strong>{app.name}</strong>
                  <small>{app.source === 'utilities' ? 'Apple utility' : app.source}</small>
                </span>
                <input
                  type="checkbox"
                  checked={selected}
                  disabled={saving}
                  onChange={() => toggleApp(app.bundle_id)}
                  aria-label={`Exclude ${app.name}`}
                />
              </label>
            );
          })}
        </div>
      </div>

      <div className="panel-card domain-card">
        <div className="panel-heading">
          <div>
            <h2>Excluded websites</h2>
          </div>
        </div>
        <p className="panel-description">
          Safari and Chrome are checked locally. Subdomains are covered automatically.
        </p>
        <div className="domain-input-row">
          <input
            className="field-input"
            value={domain}
            onChange={(event) => setDomain(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') addDomain(); }}
            placeholder="example.com"
            aria-label="Domain to exclude"
          />
          <button type="button" className="secondary-button" onClick={addDomain} disabled={saving}>
            Add
          </button>
        </div>
        <div className="domain-list">
          {settings.domains.length === 0 ? (
            <p className="empty-copy">No websites excluded yet.</p>
          ) : settings.domains.map((item) => (
            <div className="domain-row" key={item}>
              <span className="domain-avatar">{item.slice(0, 1).toUpperCase()}</span>
              <span>{item}</span>
              <button
                type="button"
                className="icon-button"
                onClick={() => void save({
                  ...settings,
                  domains: settings.domains.filter((candidate) => candidate !== item),
                })}
                disabled={saving}
                aria-label={`Remove ${item}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <p className="privacy-note">URLs and app selections stay on this device.</p>
      </div>
    </section>
  );
}
