import React, { useState, useEffect, useRef } from 'react';
import { api } from '../services/api';
import { useApp } from '../App';
import type { RequestLogEntry } from '../types';

const Logs: React.FC = () => {
  const { tl } = useApp();
  const [logs, setLogs] = useState<RequestLogEntry[]>([]);
  const [paused, setPaused] = useState(false);
  const [connected, setConnected] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await api.getLogs(200);
        setLogs(res.logs || []);
      } catch {
        setLogs([]);
      }
    })();
  }, []);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      try {
        ws = new WebSocket('ws://127.0.0.1:8765/admin/api/logs/stream');
        ws.onopen = () => setConnected(true);
        ws.onclose = () => {
          setConnected(false);
          reconnectTimer = setTimeout(connect, 3000);
        };
        ws.onerror = () => ws?.close();
        ws.onmessage = (event) => {
          try {
            const entry = JSON.parse(event.data);
            if (!paused) {
              setLogs((prev) => [entry, ...prev].slice(0, 500));
            }
          } catch {
            // Ignore malformed log events.
          }
        };
      } catch {
        reconnectTimer = setTimeout(connect, 3000);
      }
    };

    connect();

    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [paused]);

  const handleClear = async () => {
    try {
      await api.clearLogs();
      setLogs([]);
    } catch {
      // Keep existing logs if clear fails.
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>{tl('logs.title')}
          <span className={`ws-indicator ${connected ? 'running' : 'stopped'}`}>
            {connected ? tl('logs.realtime') : tl('logs.disconnected')}
          </span>
        </h2>
        <div className="btn-row">
          <button className={`btn btn-sm ${paused ? 'btn-primary' : ''}`}
            onClick={() => setPaused(!paused)}>
            {paused ? tl('logs.resume') : tl('logs.pause')}
          </button>
          <button className="btn btn-sm btn-danger" onClick={handleClear}>{tl('logs.clear')}</button>
        </div>
      </div>

      <div className="log-list" ref={listRef}>
        {logs.length === 0 && <p className="muted">{tl('logs.empty')}</p>}
        {logs.map((log, i) => (
          <div key={i} className={`log-entry ${log.status_code >= 400 ? 'error' : ''}`}>
            <span className="log-time">{log.time}</span>
            <span className={`log-badge ${log.status_code < 400 ? 'running' : 'stopped'}`}>
              {log.status_code}
            </span>
            <span className="log-ip">{log.client_ip || '-'}</span>
            <span className="log-model">{log.model}</span>
            {log.provider && <span className="log-provider">{log.provider}/{log.target_model}</span>}
            <span className="log-endpoint" title={`${tl(['入站接口', 'Inbound endpoint'])}: ${log.endpoint}`}>
              {log.upstream_api || log.endpoint}
            </span>
            <span className="log-elapsed">{log.elapsed_ms}ms</span>
            {log.tokens > 0 && <span className="log-tokens">{log.tokens} tokens</span>}
            {log.error && <span className="log-error">{log.error}</span>}
          </div>
        ))}
      </div>
    </div>
  );
};

export default Logs;
