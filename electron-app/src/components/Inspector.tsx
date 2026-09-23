import React, { useState, useEffect, useRef } from 'react';
import api from '../utils/api';
import { useApiData } from '../hooks/useApiData';
import './Inspector.css';
import { typeTitle } from '../utils/typeLabels';
import { getServerBaseUrl } from '../platform/shell';
import type { Entity, EntityScreenshot, EntityType, Relation } from '../api/contracts';
import { LEVEL_TOKENS } from './hierarchy/tokens';
import type { ChatFocus } from '../api/assistantContracts';

const TABS = ['Overview', 'Relations', 'Metadata', 'Details'] as const;
type InspectorTab = typeof TABS[number];

function OverviewTimestamp({ value }: { value?: string | null }) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return <span className="stat-value">—</span>;
  return <time className="stat-value inspector-timestamp" dateTime={date.toISOString()}>
    <span>{date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })}</span>
    <span>{date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' })}</span>
  </time>;
}

type InspectorProps = {
  entityId: number | null;
  onClose: () => void;
  onSelectEntity: (entityId: number) => void;
  onDiscuss?: (focus: ChatFocus) => void;
  initialTab?: InspectorTab;
  tabTrigger?: number;
  width?: number;
};

export default function Inspector({
  entityId,
  onClose,
  onSelectEntity,
  onDiscuss,
  initialTab = 'Overview',
  tabTrigger = 0, // Increment this to force tab change even if same tab
  width
}: InspectorProps) {
  const { data: entity, loading } = useApiData<Entity | null>(
    () => (entityId ? api.getEntity(entityId) : Promise.resolve(null)),
    [entityId]
  );
  const [activeTab, setActiveTab] = useState<InspectorTab>(initialTab);
  const [showFullJson, setShowFullJson] = useState(false);
  const [showScreenshot, setShowScreenshot] = useState(false);
  const [screenshotZoom, setScreenshotZoom] = useState(100);
  const [screenshotError, setScreenshotError] = useState(false);
  const [screenshotUrl, setScreenshotUrl] = useState('');
  const [screenshotsList, setScreenshotsList] = useState<EntityScreenshot[]>([]);
  const [selectedScreenshotIdx, setSelectedScreenshotIdx] = useState(0);
  const lastTriggerRef = useRef(tabTrigger);
  const serverURL = getServerBaseUrl();
  
  // Update tab when initialTab changes OR when tabTrigger increments
  useEffect(() => {
    // Force tab change when tabTrigger increments (e.g., View Evidence clicked again)
    // or when a new entity is selected with a specific tab
    if (tabTrigger !== lastTriggerRef.current) {
      setActiveTab(initialTab);
      lastTriggerRef.current = tabTrigger;
    }
  }, [initialTab, tabTrigger]);
  
  // Reset to initialTab when entityId changes
  useEffect(() => {
    setActiveTab(initialTab);
  }, [entityId]);

  // Reset screenshot state when entity changes
  useEffect(() => {
    setShowScreenshot(false);
    setScreenshotError(false);
    setScreenshotsList([]);
    setSelectedScreenshotIdx(0);
  }, [entityId]);

  // Fetch screenshots list when toggled on
  useEffect(() => {
    const controller = new AbortController();
    setScreenshotError(false);
    if (!showScreenshot || !entityId) {
      setScreenshotUrl('');
      setScreenshotsList([]);
      return () => controller.abort();
    }
    // Fetch available screenshots for this entity
    api.getEntityScreenshots(entityId, controller.signal)
      .then(data => {
        const list = data.screenshots || [];
        setScreenshotsList(list);
        setSelectedScreenshotIdx(0);
        if (list.length > 0) {
          const sep = list[0].url.includes('?') ? '&' : '?';
          setScreenshotUrl(`${serverURL}${list[0].url}${sep}cache=${Date.now()}`);
        } else {
          // Fallback to single screenshot endpoint
          setScreenshotUrl(`${serverURL}/api/graph/entity/${entityId}/screenshot?cache=${Date.now()}`);
        }
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        // Fallback to single screenshot endpoint
        setScreenshotsList([]);
        setScreenshotUrl(`${serverURL}/api/graph/entity/${entityId}/screenshot?cache=${Date.now()}`);
      });
    return () => controller.abort();
  }, [showScreenshot, entityId, serverURL]);

  if (!entityId) return null;

  if (loading || !entity) {
    return (
      <aside className="inspector" aria-label="Entity inspector">
        <div className="inspector-header">
          <strong>Inspector</strong>
          <button className="close-btn" onClick={onClose} aria-label="Close inspector">×</button>
        </div>
        <div className="inspector-body">
          <div className={loading ? 'loading-state' : 'error-state'}>
            {loading ? 'Loading…' : 'Entity not found'}
          </div>
        </div>
      </aside>
    );
  }

  const formatTimestamp = (timestamp?: string | null) => {
    if (!timestamp) return '—';
    return new Date(timestamp).toLocaleString();
  };

  const getTypeColor = (type?: EntityType) => {
    return type && type !== 'proposition' ? LEVEL_TOKENS[type].accent : 'var(--muted)';
  };

  const renderOverview = () => (
    <div className="inspector-tab-content overview">
      <div className="overview-header">
        <span 
          className="entity-type-tag"
          style={{ backgroundColor: `color-mix(in srgb, ${getTypeColor(entity.type)} 12%, white)`, color: getTypeColor(entity.type) }}
        >
          {typeTitle(entity.type)}
        </span>
        {typeof entity.metadata.status === 'string' && (
          <span className={`status-tag ${entity.metadata.status}`}>
            {entity.metadata.status}
          </span>
        )}
      </div>

      <h3 className="entity-title">{entity.text || 'Unnamed Entity'}</h3>

      {typeof entity.metadata.goal === 'string' && (
        <p className="entity-description">{entity.metadata.goal}</p>
      )}

      <div className="overview-stats">
        {entity.confidence != null && (
          <div className="stat-block">
            <span className="stat-label">Confidence</span>
            <span className="stat-value">{Math.round(Number(entity.confidence) * 10)}%</span>
          </div>
        )}
        <div className="stat-block">
          <span className="stat-label">Created</span>
          <OverviewTimestamp value={entity.created_at || entity.timestamp_start || entity.timestamp} />
        </div>
        <div className="stat-block">
          <span className="stat-label">Started</span>
          <OverviewTimestamp value={entity.timestamp_start || entity.timestamp} />
        </div>
        {entity.timestamp_end && (
          <div className="stat-block">
            <span className="stat-label">Ended</span>
            <OverviewTimestamp value={entity.timestamp_end} />
          </div>
        )}
      </div>

      <div className="action-buttons">
        <button type="button" className="action-btn primary" onClick={() => setActiveTab('Relations')}>
          View Evidence
        </button>
        {onDiscuss && entity.type !== 'proposition' && <button type="button" className="action-btn" onClick={() => {
          if (entity.type !== 'proposition') onDiscuss({ kind: entity.type, title: entity.text.slice(0, 300), detail: '', entity_ids: [entity.id] });
        }}>Discuss with assistant</button>}
      </div>

      <div className="screenshot-toggle">
        <label>
          <input
            type="checkbox"
            checked={showScreenshot}
            onChange={(e) => setShowScreenshot(e.target.checked)}
          />
          Show screenshots{screenshotsList.length > 0 ? ` (${screenshotsList.length})` : ''}
        </label>
      </div>

      {showScreenshot && (
        <div className="screenshot-panel">
          <div className="screenshot-controls">
            <span>Zoom</span>
            <input
              type="range"
              min="50"
              max="200"
              step="10"
              value={screenshotZoom}
              onChange={(e) => setScreenshotZoom(Number(e.target.value))}
            />
            <span>{screenshotZoom}%</span>
          </div>

          {screenshotsList.length > 1 && (
            <div className="screenshot-nav">
              <button
                className="screenshot-nav-btn"
                disabled={selectedScreenshotIdx === 0}
                onClick={() => {
                  const idx = selectedScreenshotIdx - 1;
                  setSelectedScreenshotIdx(idx);
                  const sep = screenshotsList[idx].url.includes('?') ? '&' : '?';
                  setScreenshotUrl(`${serverURL}${screenshotsList[idx].url}${sep}cache=${Date.now()}`);
                  setScreenshotError(false);
                }}
              >
                Prev
              </button>
              <span className="screenshot-nav-label">
                {selectedScreenshotIdx + 1} / {screenshotsList.length}
              </span>
              <button
                className="screenshot-nav-btn"
                disabled={selectedScreenshotIdx === screenshotsList.length - 1}
                onClick={() => {
                  const idx = selectedScreenshotIdx + 1;
                  setSelectedScreenshotIdx(idx);
                  const sep = screenshotsList[idx].url.includes('?') ? '&' : '?';
                  setScreenshotUrl(`${serverURL}${screenshotsList[idx].url}${sep}cache=${Date.now()}`);
                  setScreenshotError(false);
                }}
              >
                Next
              </button>
            </div>
          )}

          <div className="screenshot-frame">
            {screenshotUrl && !screenshotError ? (
              <img
                className="screenshot-image"
                src={screenshotUrl}
                alt="Entity screenshot"
                style={{ width: `${screenshotZoom}%` }}
                onLoad={() => setScreenshotError(false)}
                onError={() => setScreenshotError(true)}
              />
            ) : (
              <div className="screenshot-empty">No screenshot available</div>
            )}
          </div>

          {screenshotsList.length > 1 && screenshotsList[selectedScreenshotIdx]?.timestamp && (
            <div className="screenshot-timestamp">
              {formatTimestamp(screenshotsList[selectedScreenshotIdx].timestamp)}
            </div>
          )}
        </div>
      )}
    </div>
  );

  const renderRelations = () => {
    const incoming = entity.relations?.incoming || [];
    const outgoing = entity.relations?.outgoing || [];

    const groupedIncoming = incoming.reduce<Record<string, Relation[]>>((acc, rel) => {
      const key = rel.type + (rel.subtype ? `-${rel.subtype}` : '');
      if (!acc[key]) acc[key] = [];
      acc[key].push(rel);
      return acc;
    }, {});

    const groupedOutgoing = outgoing.reduce<Record<string, Relation[]>>((acc, rel) => {
      const key = rel.type + (rel.subtype ? `-${rel.subtype}` : '');
      if (!acc[key]) acc[key] = [];
      acc[key].push(rel);
      return acc;
    }, {});

    const getTypeLabel = (type?: EntityType) => {
      switch(type) {
        case 'goal': return 'Goal';
        case 'activity': return 'Activity';
        case 'action': return 'Action';
        case 'operation': return 'Operation';
        default: return type || 'Entity';
      }
    };

    const renderRelationItem = (rel: Relation, isIncoming: boolean) => {
      const entityId = isIncoming ? rel.source_id : rel.target_id;
      const entityText = isIncoming ? rel.source_text : rel.target_text;
      const entityType = isIncoming ? rel.source_type : rel.target_type;
      
      return (
        <button
          type="button"
          key={rel.id || `${entityId}-${Math.random()}`} 
          className="relation-item"
          onClick={() => entityId !== undefined && onSelectEntity(entityId)}
        >
          <div className="relation-entity-info">
            {entityType && (
              <span 
                className="relation-type-badge"
                style={{ 
                  backgroundColor: `color-mix(in srgb, ${getTypeColor(entityType)} 12%, white)`,
                  color: getTypeColor(entityType) 
                }}
              >
                {getTypeLabel(entityType)}
              </span>
            )}
            <span className="relation-entity-text">
              {entityText || `Entity #${entityId}`}
            </span>
          </div>
          <div className="relation-meta">
            {rel.confidence !== undefined && rel.confidence !== null && (
              <span className="relation-confidence">
                {Math.round(rel.confidence * 100)}%
              </span>
            )}
            <span className="relation-id">#{entityId}</span>
          </div>
        </button>
      );
    };

    return (
    <div className="inspector-tab-content relations">
        {Object.keys(groupedIncoming).length > 0 && (
          <div className="relation-group">
            <h4 className="group-title">
              <span className="title-icon">←</span>
              Incoming Relations
            </h4>
            {Object.entries(groupedIncoming).map(([key, rels]) => (
              <div key={key} className="relation-category">
                <div className="category-header">
                  <span className="category-name">{key.replace('-', ' → ')}</span>
                  <span className="category-count">{rels.length}</span>
                </div>
                <div className="relation-list">
                  {rels.slice(0, 10).map((rel) => renderRelationItem(rel, true))}
                  {rels.length > 10 && (
                    <div className="more-relations">
                      +{rels.length - 10} more
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {Object.keys(groupedOutgoing).length > 0 && (
          <div className="relation-group">
            <h4 className="group-title">
              <span className="title-icon">→</span>
              Outgoing Relations
            </h4>
            {Object.entries(groupedOutgoing).map(([key, rels]) => (
              <div key={key} className="relation-category">
                <div className="category-header">
                  <span className="category-name">{key.replace('-', ' → ')}</span>
                  <span className="category-count">{rels.length}</span>
                </div>
                <div className="relation-list">
                  {rels.slice(0, 10).map((rel) => renderRelationItem(rel, false))}
                  {rels.length > 10 && (
                    <div className="more-relations">
                      +{rels.length - 10} more
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {Object.keys(groupedIncoming).length === 0 && Object.keys(groupedOutgoing).length === 0 && (
          <div className="empty-relations">No relations found for this entity</div>
        )}
      </div>
    );
  };

  const renderMetadata = () => (
    <div className="inspector-tab-content metadata">
      {entity.metadata && Object.keys(entity.metadata).length > 0 ? (
        <>
          <div className="metadata-summary">
            {Object.entries(entity.metadata).slice(0, 6).map(([key, value]) => (
              <div key={key} className="metadata-item">
                <span className="metadata-key">{key}</span>
                <span className="metadata-value">
                  {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                </span>
              </div>
            ))}
          </div>
          
          <button 
            className="show-json-btn"
            onClick={() => setShowFullJson(!showFullJson)}
          >
            {showFullJson ? 'Hide' : 'View'} full JSON
          </button>

          {showFullJson && (
            <pre className="json-view">
              {JSON.stringify(entity.metadata, null, 2)}
            </pre>
          )}
        </>
      ) : (
        <div className="empty-metadata">No metadata available</div>
      )}
    </div>
  );

  const renderDetails = () => (
    <div className="inspector-tab-content details">
      <div className="details-item">
        <span className="details-label">Entity ID</span>
        <span className="details-value">{entity.id}</span>
      </div>
      <div className="details-item">
        <span className="details-label">Type</span>
        <span className="details-value">{entity.type}</span>
      </div>
      <div className="details-item">
        <span className="details-label">Created At</span>
        <span className="details-value">{formatTimestamp(entity.created_at || entity.timestamp_start || entity.timestamp)}</span>
      </div>
      <div className="details-item">
        <span className="details-label">Time Range Start</span>
        <span className="details-value">{formatTimestamp(entity.timestamp_start || entity.timestamp)}</span>
      </div>
      {entity.confidence != null && (
        <div className="details-item">
          <span className="details-label">Confidence Score</span>
          <span className="details-value">{Number(entity.confidence).toFixed(4)}</span>
        </div>
      )}
      {typeof entity.metadata.source === 'string' && (
        <div className="details-item">
          <span className="details-label">Source</span>
          <span className="details-value">{entity.metadata.source}</span>
        </div>
      )}
      {typeof entity.metadata.app === 'string' && (
        <div className="details-item">
          <span className="details-label">Application</span>
          <span className="details-value">{entity.metadata.app}</span>
        </div>
      )}
    </div>
  );

  return (
    <aside className="inspector" aria-label="Entity inspector" style={width ? { width: `${width}px`, minWidth: `${width}px` } : undefined}>
      <div className="inspector-header">
        <div className="inspector-tabs">
          {TABS.map(tab => (
            <button
              key={tab}
              className={`inspector-tab ${activeTab === tab ? 'active' : ''}`}
              onClick={() => setActiveTab(tab)}
              aria-pressed={activeTab === tab}
            >
              {tab}
            </button>
          ))}
        </div>
        <button className="close-btn" onClick={onClose} aria-label="Close inspector">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="18" y1="6" x2="6" y2="18"/>
            <line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>

      <div className="inspector-body">
        {activeTab === 'Overview' && renderOverview()}
        {activeTab === 'Relations' && renderRelations()}
        {activeTab === 'Metadata' && renderMetadata()}
        {activeTab === 'Details' && renderDetails()}
      </div>
    </aside>
  );
}
