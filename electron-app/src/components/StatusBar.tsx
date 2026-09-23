import React, { useState, useEffect } from 'react';
import './StatusBar.css';
import { typePluralFromStatsKey } from '../utils/typeLabels';
import type { TempoStats, TempoStatus } from '../api/contracts';

type StatusBarProps = {
  status?: TempoStatus | null;
  stats?: TempoStats | null;
  serverHealth: boolean;
};

export default function StatusBar({ status, stats, serverHealth }: StatusBarProps) {
  const [elapsedTime, setElapsedTime] = useState<string | null>(null);

  // Calculate elapsed time when recording
  useEffect(() => {
    if (!status?.running || !status?.started_at) {
      setElapsedTime(null);
      return;
    }

    const startTime = new Date(status.started_at).getTime();
    
    const updateElapsed = () => {
      const now = Date.now();
      const diffMs = now - startTime;
      const hours = Math.floor(diffMs / 3600000);
      const minutes = Math.floor((diffMs % 3600000) / 60000);
      const seconds = Math.floor((diffMs % 60000) / 1000);
      
      if (hours > 0) {
        setElapsedTime(`${hours}h ${minutes}m ${seconds}s`);
      } else if (minutes > 0) {
        setElapsedTime(`${minutes}m ${seconds}s`);
      } else {
        setElapsedTime(`${seconds}s`);
      }
    };

    updateElapsed();
    const interval = setInterval(updateElapsed, 1000);
    return () => clearInterval(interval);
  }, [status?.running, status?.started_at]);

  return (
    <div className="status-bar" role="status" aria-live="polite">
      <div className="status-bar-left">
        <div className={`status-indicator ${status?.running ? 'recording' : 'idle'}`}>
          <span className="status-dot" />
          <span className="status-text">
            {status?.running ? 'Recording' : 'Idle'}
          </span>
          {elapsedTime && (
            <span className="elapsed-time">{elapsedTime}</span>
          )}
        </div>
      </div>

      <div className="status-bar-center">
        {stats && (
          <div className="stats-inline">
            <span className="stat-pill">
              <span className="stat-count">{stats.operations || 0}</span>
              <span className="stat-label">{typePluralFromStatsKey('operations')}</span>
            </span>
            <span className="stat-separator">•</span>
            <span className="stat-pill">
              <span className="stat-count">{stats.actions || 0}</span>
              <span className="stat-label">{typePluralFromStatsKey('actions')}</span>
            </span>
            <span className="stat-separator">•</span>
            <span className="stat-pill">
              <span className="stat-count">{stats.activities || 0}</span>
              <span className="stat-label">{typePluralFromStatsKey('activities')}</span>
            </span>
            <span className="stat-separator">•</span>
            <span className="stat-pill">
              <span className="stat-count">{stats.goals || 0}</span>
              <span className="stat-label">{typePluralFromStatsKey('goals')}</span>
            </span>
          </div>
        )}
      </div>

      <div className="status-bar-right">
        <div className={`backend-status ${serverHealth ? 'connected' : 'disconnected'}`}>
          <span className="backend-dot" />
          <span className="backend-text">
            {serverHealth ? 'Backend connected' : 'Backend disconnected'}
          </span>
        </div>
      </div>
    </div>
  );
}
