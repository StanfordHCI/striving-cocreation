import { useEffect, useState, type FormEvent } from 'react';
import api from '../api/client';
import type { SearchResponse, TimelineResponse } from '../api/contracts';

export default function SearchView({ onSelectEntity }: { onSelectEntity: (id: number) => void }) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [timeline, setTimeline] = useState<TimelineResponse | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api.getTimeline(7, controller.signal).then(setTimeline).catch(() => undefined);
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const normalized = query.trim();
    if (!normalized) return;
    setSearching(true);
    setError(null);
    try {
      setResults(await api.search(normalized));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Search failed');
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className="view-scroll search-view">
      <div className="view-heading">
        <div>
          <h1>Search</h1>
          <p>Search recorded actions, activities, and goals.</p>
        </div>
      </div>
      <form className="search-form" onSubmit={(event) => void submit(event)}>
        <input
          className="search-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search goals, activities, actions…"
          aria-label="Search activity"
          autoFocus
        />
        <button type="submit" className="primary-button" disabled={searching || !query.trim()}>
          {searching ? 'Searching…' : 'Search'}
        </button>
      </form>
      {error && <p className="inline-error" role="alert">{error}</p>}

      <div className="search-columns">
        <section className="panel-card search-results">
          <div className="panel-heading">
            <h2>{results ? `${results.count} results` : 'Results'}</h2>
          </div>
          {!results ? <p className="empty-copy">Enter a query to search your local graph.</p> : (
            <div className="result-list">
              {results.results.map(({ entity, score }) => (
                <button type="button" className="result-row" key={entity.id} onClick={() => onSelectEntity(entity.id)}>
                  <span className={`type-mark ${entity.type}`} />
                  <span>
                    <small>{entity.type} · {Math.round(score * 100)}% match</small>
                    <strong>{entity.text}</strong>
                  </span>
                </button>
              ))}
              {results.count === 0 && <p className="empty-copy">No matching entities.</p>}
            </div>
          )}
        </section>

        <section className="panel-card timeline-panel">
          <div className="panel-heading"><h2>Past seven days</h2></div>
          {!timeline ? <p className="empty-copy">Loading timeline…</p> : (
            <div className="timeline-list">
              {timeline.activities.map((activity) => (
                <button type="button" className="timeline-row" key={activity.id} onClick={() => onSelectEntity(activity.id)}>
                  <time>{activity.timestamp_start ? new Date(activity.timestamp_start).toLocaleDateString() : '—'}</time>
                  <span>{activity.text}</span>
                </button>
              ))}
              {timeline.count === 0 && <p className="empty-copy">No recent activities.</p>}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
