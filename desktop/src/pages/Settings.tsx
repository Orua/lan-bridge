import React, { useState, useEffect, useCallback } from 'react';
import { api } from '../services/api';
import { useApp, ThemeName } from '../App';
import { Lang } from '../i18n';
import type { ServerSettings, WebSearchSettings, WebSearchTestResult } from '../types';

const THEMES: { id: ThemeName; label: [string, string] }[] = [
  { id: 'dark', label: ['深色', 'Dark'] },
  { id: 'light', label: ['浅色', 'Light'] },
  { id: 'blue', label: ['海蓝', 'Ocean'] },
  { id: 'green', label: ['森绿', 'Forest'] },
  { id: 'purple', label: ['紫韵', 'Purple'] },
  { id: 'warm', label: ['暖橙', 'Warm'] },
];

const DEFAULT_WEB_PROVIDER = {
  adapter: 'bocha',
  display_name: 'Bocha Web Search',
  base_url: 'https://api.bocha.cn/v1/web-search',
  api_key_env: 'BOCHA_API_KEY',
  api_key: '',
  enabled: true,
  timeout: 30,
  max_results: 5,
  summary: true,
  freshness: 'noLimit',
};

const Settings: React.FC = () => {
  const { theme, setTheme, lang, setLang, tl } = useApp();
  const [settings, setSettings] = useState<ServerSettings | null>(null);
  const [form, setForm] = useState({
    host: '127.0.0.1',
    port: 8765,
    log_level: 'info',
    launch_at_login: false,
    auto_start: false,
    close_to_tray: true,
    audit_log_path: '',
    codex_official_proxy_url: '',
    native_auth_injection: {
      enabled: false,
      auth_file: '',
      auth_file_found: false,
    },
  });
  const [saved, setSaved] = useState(false);
  const [importYaml, setImportYaml] = useState('');
  const [webSearch, setWebSearch] = useState<WebSearchSettings | null>(null);
  const [webEnabled, setWebEnabled] = useState(false);
  const [webProvider, setWebProvider] = useState({ ...DEFAULT_WEB_PROVIDER });
  const [webSaved, setWebSaved] = useState(false);
  const [testQuery, setTestQuery] = useState('OpenAI');
  const [testingSearch, setTestingSearch] = useState(false);
  const [searchTest, setSearchTest] = useState<WebSearchTestResult | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await api.getSettings();
      setSettings(s);
      setForm({
        ...s.server,
        native_auth_injection: { ...s.server.native_auth_injection },
      });
    } catch {
      setSettings(null);
    }
    try {
      const value = await api.getWebSearchSettings();
      const provider = value.providers[value.active_provider] || DEFAULT_WEB_PROVIDER;
      setWebSearch(value);
      setWebEnabled(value.enabled);
      setWebProvider({ ...DEFAULT_WEB_PROVIDER, ...provider, api_key: '' });
    } catch {
      setWebSearch(null);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSave = async () => {
    try {
      await api.updateSettings(form);
      if (window.electronAPI) {
        const loginResult = await window.electronAPI.setLaunchAtLogin(form.launch_at_login);
        if (loginResult.status !== 'ok') {
          throw new Error(loginResult.message || 'Unable to update login startup setting');
        }
      }
      await load();
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err: any) {
      alert(`${tl('common.error')}: ${err.message || err}`);
    }
  };

  const handleExport = async () => {
    try {
      const data = await api.exportConfig();
      if (window.electronAPI) {
        await window.electronAPI.saveFile({ defaultPath: 'lan-bridge-config.yaml', content: data.yaml });
      } else {
        const blob = new Blob([data.yaml], { type: 'text/yaml' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'lan-bridge-config.yaml';
        a.click();
        URL.revokeObjectURL(url);
      }
    } catch (err: any) {
      alert(`${tl('common.error')}: ${err.message || err}`);
    }
  };

  const webPayload = () => ({
    enabled: webEnabled,
    active_provider: 'bocha',
    provider: webProvider,
  });

  const handleWebSave = async () => {
    try {
      const result = await api.updateWebSearchSettings(webPayload());
      const provider = result.web_search.providers[result.web_search.active_provider];
      setWebSearch(result.web_search);
      setWebProvider({ ...DEFAULT_WEB_PROVIDER, ...provider, api_key: '' });
      setWebSaved(true);
      setTimeout(() => setWebSaved(false), 2000);
    } catch (err: any) {
      alert(`${tl('common.error')}: ${err.message || err}`);
    }
  };

  const handleWebTest = async () => {
    setTestingSearch(true);
    setSearchTest(null);
    try {
      const result = await api.testWebSearch({ ...webPayload(), query: testQuery });
      setSearchTest(result);
    } catch (err: any) {
      setSearchTest({ status: 'error', message: err.message || String(err) });
    } finally {
      setTestingSearch(false);
    }
  };

  const handleImport = async () => {
    if (!importYaml.trim()) return;
    try {
      const result = await api.importConfig(importYaml);
      if (result.error) {
        alert(`${tl('common.error')}: ${result.error}`);
      } else {
        alert(tl('common.ok'));
        setImportYaml('');
        load();
      }
    } catch (err: any) {
      alert(`${tl('common.error')}: ${err.message || err}`);
    }
  };

  const handleSelectFile = async () => {
    try {
      if (window.electronAPI) {
        const filePath = await window.electronAPI.selectFile({ filters: [{ name: 'YAML', extensions: ['yaml', 'yml'] }] });
        if (filePath) {
          const res = await fetch(`file://${filePath}`);
          setImportYaml(await res.text());
        }
      }
    } catch {
      // Keep the text box as the fallback import path.
    }
  };

  return (
    <div className="page">
      <h2>{tl('settings.title')}</h2>

      <section className="settings-section">
        <h3>{tl('settings.server')}</h3>
        {settings?.config_path && <p className="field-hint">Config: {settings.config_path}</p>}
        <div className="form-grid">
          <div className="form-group">
            <label>{tl('settings.host')}</label>
            <input value={form.host}
              onChange={e => setForm({ ...form, host: e.target.value })} />
          </div>
          <div className="form-group">
            <label>{tl('settings.port')}</label>
            <input type="number" value={form.port}
              onChange={e => setForm({ ...form, port: Number(e.target.value) })} />
          </div>
          <div className="form-group">
            <label>{tl('settings.logLevel')}</label>
            <select value={form.log_level}
              onChange={e => setForm({ ...form, log_level: e.target.value })}>
              <option value="debug">Debug</option>
              <option value="info">Info</option>
              <option value="warning">Warning</option>
              <option value="error">Error</option>
            </select>
          </div>
          <div className="form-group">
            <label>{tl('settings.auditLog')}</label>
            <input value={form.audit_log_path}
              onChange={e => setForm({ ...form, audit_log_path: e.target.value })}
              placeholder={tl(['留空则不写审计日志', 'Leave empty to disable audit logging'])} />
          </div>
        </div>

        <label className="checkbox-label">
          <input type="checkbox" checked={form.launch_at_login}
            onChange={e => setForm({ ...form, launch_at_login: e.target.checked })} />
          {tl('settings.launchAtLogin')}
        </label>
        <label className="checkbox-label">
          <input type="checkbox" checked={form.auto_start}
            onChange={e => setForm({ ...form, auto_start: e.target.checked })} />
          {tl('settings.autoStart')}
        </label>
        <label className="checkbox-label">
          <input type="checkbox" checked={form.close_to_tray}
            onChange={e => setForm({ ...form, close_to_tray: e.target.checked })} />
          {tl('settings.closeToTray')}
        </label>
      </section>

      <section className="settings-section">
        <h3>{tl(['Codex 官方联网代理', 'Codex Official Network Proxy'])}</h3>
        <p className="field-hint">
          {tl(['仅在切换到 OpenAI 官方版时使用；不是 Bridge API 的监听或转发地址。', 'Used only when switching Codex to the official OpenAI API; this is not the Bridge API endpoint.'])}
        </p>
        <div className="form-grid">
          <div className="form-group full-width">
            <label>{tl(['VPN / HTTP 代理 URL', 'VPN / HTTP Proxy URL'])}</label>
            <input value={form.codex_official_proxy_url || ''}
              onChange={e => setForm({ ...form, codex_official_proxy_url: e.target.value })}
              placeholder="http://127.0.0.1:7890" />
          </div>
        </div>
      </section>

      <section className="settings-section">
        <h3>{tl(['主机 Codex 登录注入', 'Host Codex Login Injection'])}</h3>
        <p className="field-hint">
          {tl([
            'OpenAI 登录凭据只保留在桥接电脑。客户端使用“访问密匙”页面分配的独立密匙，密匙可限制模型并统计 Token 用量。',
            'OpenAI credentials stay only on this computer. Clients use keys issued on the Access Keys page, with per-model permissions and token usage tracking.',
          ])}
        </p>
        <label className="checkbox-label">
          <input type="checkbox" checked={form.native_auth_injection.enabled}
            onChange={e => setForm({
              ...form,
              native_auth_injection: { ...form.native_auth_injection, enabled: e.target.checked },
            })} />
          {tl(['启用服务器端登录注入', 'Enable server-side login injection'])}
        </label>
        <div className="form-grid">
          <div className="form-group full-width">
            <label>{tl(['Codex auth.json 路径（留空自动检测）', 'Codex auth.json path (leave empty for auto-detect)'])}</label>
            <input value={form.native_auth_injection.auth_file}
              onChange={e => setForm({
                ...form,
                native_auth_injection: { ...form.native_auth_injection, auth_file: e.target.value },
              })}
              placeholder="%USERPROFILE%\\.codex\\auth.json" />
            <span className="field-hint">
              {form.native_auth_injection.auth_file_found
                ? tl(['已找到登录缓存', 'Login cache found'])
                : tl(['尚未找到登录缓存；请先在桥接电脑登录 Codex', 'Login cache not found; sign in to Codex on this computer first'])}
            </span>
          </div>
        </div>
      </section>

      <section className="settings-section">
        <h3>{tl(['联网搜索服务', 'Web Search Provider'])}</h3>
        <p className="field-hint">
          {tl(['当前支持 Bocha；接口地址和密钥均可维护。', 'Bocha is currently supported. Endpoint and key are configurable.'])}
        </p>
        <label className="checkbox-label">
          <input type="checkbox" checked={webEnabled}
            onChange={e => setWebEnabled(e.target.checked)} />
          {tl(['启用联网搜索配置', 'Enable web search configuration'])}
        </label>
        <div className="form-grid">
          <div className="form-group">
            <label>{tl(['服务提供方', 'Provider'])}</label>
            <select value="bocha" disabled>
              <option value="bocha">Bocha</option>
            </select>
          </div>
          <div className="form-group">
            <label>{tl(['API Key 环境变量（可选）', 'API Key environment variable (optional)'])}</label>
            <input value={webProvider.api_key_env}
              onChange={e => setWebProvider({ ...webProvider, api_key_env: e.target.value })} />
          </div>
          <div className="form-group full-width">
            <label>{tl(['搜索 API 地址', 'Search API endpoint'])}</label>
            <input value={webProvider.base_url}
              onChange={e => setWebProvider({ ...webProvider, base_url: e.target.value })} />
          </div>
          <div className="form-group">
            <label>API Key</label>
            <input type="password" value={webProvider.api_key}
              placeholder={webSearch?.providers.bocha?.api_key_set
                ? tl(['已保存，留空则保持不变', 'Saved; leave blank to keep it'])
                : tl(['请输入 API Key', 'Enter API Key'])}
              onChange={e => setWebProvider({ ...webProvider, api_key: e.target.value })} />
          </div>
          <div className="form-group">
            <label>{tl(['返回结果数量', 'Result count'])}</label>
            <input type="number" min={1} max={10} value={webProvider.max_results}
              onChange={e => setWebProvider({ ...webProvider, max_results: Number(e.target.value) })} />
          </div>
          <div className="form-group">
            <label>{tl(['测试关键词', 'Test query'])}</label>
            <input value={testQuery} onChange={e => setTestQuery(e.target.value)} />
          </div>
          <div className="form-group">
            <label>{tl(['时效范围', 'Freshness'])}</label>
            <select value={webProvider.freshness}
              onChange={e => setWebProvider({ ...webProvider, freshness: e.target.value })}>
              <option value="noLimit">{tl(['不限', 'No limit'])}</option>
              <option value="oneDay">{tl(['一天内', 'Past day'])}</option>
              <option value="oneWeek">{tl(['一周内', 'Past week'])}</option>
              <option value="oneMonth">{tl(['一月内', 'Past month'])}</option>
              <option value="oneYear">{tl(['一年内', 'Past year'])}</option>
            </select>
          </div>
        </div>
        <label className="checkbox-label">
          <input type="checkbox" checked={webProvider.summary}
            onChange={e => setWebProvider({ ...webProvider, summary: e.target.checked })} />
          {tl(['请求搜索摘要', 'Request search summaries'])}
        </label>
        <div className="btn-row" style={{ marginTop: 14 }}>
          <button className="btn btn-primary" onClick={handleWebSave}>
            {tl(['保存搜索设置', 'Save search settings'])}
          </button>
          <button className="btn btn-outline" onClick={handleWebTest} disabled={testingSearch}>
            {testingSearch ? tl(['测试中...', 'Testing...']) : tl(['测试连接', 'Test connection'])}
          </button>
          {webSaved && <span className="save-confirm">{tl('settings.saved')}</span>}
        </div>
        {searchTest && (
          <div className={`test-result ${searchTest.status}`}>
            {searchTest.status === 'ok'
              ? `${tl(['连接成功', 'Connection succeeded'])} (${searchTest.elapsed_ms} ms, ${searchTest.result_count || 0} ${tl(['条结果', 'results'])})`
              : `${tl(['连接失败', 'Connection failed'])}: ${searchTest.message}`}
            {searchTest.results && searchTest.results.length > 0 && (
              <div className="search-test-results">
                {searchTest.results.map((item, index) => (
                  <a key={`${item.url}-${index}`} href={item.url} target="_blank" rel="noreferrer">
                    {item.title || item.url}
                  </a>
                ))}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="settings-section">
        <h3>{tl('settings.config')}</h3>
        <div className="form-grid">
          <div className="form-group full-width">
            <label>{tl('settings.import')}</label>
            <textarea value={importYaml} onChange={e => setImportYaml(e.target.value)}
              rows={6} placeholder="YAML..."
              style={{ fontFamily: 'monospace', fontSize: '13px' }} />
            <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
              <button className="btn btn-sm" onClick={handleSelectFile}>{tl('settings.selectFile')}</button>
              <button className="btn btn-sm btn-primary" onClick={handleImport}>{tl('settings.importBtn')}</button>
            </div>
          </div>
        </div>
        <div className="btn-row" style={{ marginTop: 12 }}>
          <button className="btn btn-outline" onClick={handleExport}>{tl('settings.export')}</button>
        </div>
      </section>

      <section className="settings-section">
        <h3>{tl('settings.appearance')}</h3>
        <div className="form-grid">
          <div className="form-group">
            <label>{tl('settings.theme')}</label>
            <div className="theme-swatches">
              {THEMES.map(t => (
                <div
                  key={t.id}
                  className={`theme-swatch ${t.id} ${theme === t.id ? 'active' : ''}`}
                  title={tl(t.label)}
                  onClick={() => setTheme(t.id)}
                />
              ))}
            </div>
          </div>
          <div className="form-group">
            <label>{tl('settings.language')}</label>
            <select value={lang} onChange={e => setLang(e.target.value as Lang)}>
              <option value="zh">{tl('settings.langZh')}</option>
              <option value="en">{tl('settings.langEn')}</option>
            </select>
          </div>
        </div>
      </section>

      <div className="btn-row" style={{ marginTop: 24 }}>
        <button className="btn btn-primary" onClick={handleSave}>{tl('settings.save')}</button>
        {saved && <span className="save-confirm">{tl('settings.saved')}</span>}
      </div>
    </div>
  );
};

export default Settings;
