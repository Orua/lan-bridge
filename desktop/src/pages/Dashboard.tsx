import React, { useState, useEffect, useCallback } from 'react';
import { api } from '../services/api';
import { useApp } from '../App';
import type { ProxyStatus, ModelConfig, CodexConfigStatus } from '../types';

const Dashboard: React.FC = () => {
  const { tl } = useApp();
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [codexStatus, setCodexStatus] = useState<CodexConfigStatus | null>(null);
  const [switching, setSwitching] = useState(false);
  const [switchMsg, setSwitchMsg] = useState('');

  const load = useCallback(async () => {
    try {
      const [s, m, cs] = await Promise.all([
        api.getStatus(),
        api.getModels(),
        api.getCodexStatus().catch(() => null),
      ]);
      setStatus(s);
      setModels(m.models || []);
      setCodexStatus(cs as CodexConfigStatus | null);
    } catch {
      setStatus(null);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const uptime = status ? Math.floor(status.stats.uptime_seconds) : 0;
  const uptimeStr = `${Math.floor(uptime / 3600)}h ${Math.floor((uptime % 3600) / 60)}m ${uptime % 60}s`;

  const handleSwitch = async (target: 'unified' | 'official') => {
    setSwitching(true);
    setSwitchMsg('');
    try {
      const fn = target === 'unified' ? api.enableUnifiedRouting : api.restoreOfficialRouting;
      const res = await fn();
      setSwitchMsg(res.message);
      setCodexStatus(await api.getCodexStatus().catch(() => null));
    } catch (e: any) {
      setSwitchMsg(`Failed: ${e.message || e}`);
    } finally {
      setSwitching(false);
    }
  };

  return (
    <div className="page">
      <h2>{tl('dashboard.title')}</h2>

      <div className="cards-row">
        <div className="card status-card">
          <h3>{tl('dashboard.status')}</h3>
          <div className={`status-badge ${status?.running ? 'running' : 'stopped'}`}>
            {status?.running ? tl('app.running') : tl('app.stopped')}
          </div>
          {status?.running && (
            <div className="card-detail">
              <div>{tl('dashboard.uptime')}: {uptimeStr}</div>
              <div>{tl('about.version')}: {status.version}</div>
            </div>
          )}
        </div>

        <div className="card stats-card">
          <h3>{tl('dashboard.stats')}</h3>
          <div className="stats-grid">
            <div className="stat">
              <span className="stat-num">{status?.stats.request_count ?? 0}</span>
              <span className="stat-label">{tl('dashboard.total')}</span>
            </div>
            <div className="stat success">
              <span className="stat-num">{status?.stats.success_count ?? 0}</span>
              <span className="stat-label">{tl('dashboard.success')}</span>
            </div>
            <div className="stat error">
              <span className="stat-num">{status?.stats.error_count ?? 0}</span>
              <span className="stat-label">{tl('dashboard.error')}</span>
            </div>
            <div className="stat">
              <span className="stat-num">{status?.stats.avg_response_ms ?? status?.stats.avg_latency_ms ?? 0}ms</span>
              <span className="stat-label">{tl('dashboard.avgLatency')}</span>
            </div>
            <div className="stat">
              <span className="stat-num">{status?.stats.avg_first_response_ms ?? 0}ms</span>
              <span className="stat-label">{tl('dashboard.avgFirstResponse')}</span>
            </div>
          </div>
        </div>
      </div>

      <h3>{tl('dashboard.models')}</h3>
      <div className="model-health-grid">
        {models.length === 0 && <p className="muted">{tl('dashboard.noModels')}</p>}
        {models.map((m) => (
          <div key={m.alias} className="health-card">
            <div className="health-card-header">
              <span className={`dot ${m.enabled ? 'running' : 'stopped'}`} />
              <strong>{m.alias}</strong>
            </div>
            <div className="health-card-body">
              <div>{m.target_model}</div>
              <div className="muted">{m.provider}</div>
            </div>
          </div>
        ))}
      </div>

      <h3>{tl(['Codex 路由模式', 'Codex routing mode'])}</h3>
      <div className="cc-switch-card">
        {codexStatus ? (
          <div className="cc-switch-status">
            <span className={`status-badge ${codexStatus.using_bridge ? 'running' : 'stopped'}`}>
              {codexStatus.using_bridge ? tl(['当前：CN Bridge 统一路由', 'Current: CN Bridge unified routing']) : tl(['当前：OpenAI 官方直连', 'Current: OpenAI official direct'])}
            </span>
            {codexStatus.details.model && (
              <span className="muted">模型：{codexStatus.details.model}</span>
            )}
          </div>
        ) : (
          <p className="muted">无法读取 Codex 配置</p>
        )}
        <div className="cc-switch-buttons">
          <button
            className="btn btn-primary"
            disabled={switching || codexStatus?.using_bridge === true}
            onClick={() => handleSwitch('unified')}
          >
            {tl(['启用 CN Bridge 统一路由', 'Enable CN Bridge unified routing'])}
          </button>
          <button
            className="btn btn-outline"
            disabled={switching}
            onClick={() => handleSwitch('official')}
          >
            {tl(['切换到 Codex 官方直连', 'Switch to Codex official direct'])}
          </button>
        </div>
        {switchMsg && (
          <p className={`switch-msg ${switchMsg.startsWith('Failed') ? 'error' : 'success'}`}>
            {switchMsg}
          </p>
        )}
      </div>
      <p className="muted">{tl(['切换只修改 Bridge 自己的模型、目录和代理字段；MCP、插件、项目及其他 Codex 设置始终保留。不会关闭当前 Codex，重新加载后生效。', 'Switching only changes Bridge-owned model, catalog, and proxy fields. MCP servers, plugins, projects, and other Codex settings are always preserved. Reload Codex to apply.'])}</p>

      <div className="quick-actions">
        <button className="btn btn-primary" onClick={load}>刷新状态</button>
      </div>
    </div>
  );
};

export default Dashboard;
