import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useApp } from '../App';
import { api } from '../services/api';
import type { AccessKeyAvailableModel, AccessKeyRecord } from '../types';

const formatNumber = (value: number) => new Intl.NumberFormat().format(value || 0);

const AccessKeys: React.FC = () => {
  const { tl, lang } = useApp();
  const [keys, setKeys] = useState<AccessKeyRecord[]>([]);
  const [models, setModels] = useState<AccessKeyAvailableModel[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [newName, setNewName] = useState('');
  const [newModels, setNewModels] = useState<string[]>(['*']);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState('');
  const [editModels, setEditModels] = useState<string[]>([]);
  const [revealedKey, setRevealedKey] = useState<{ key: string; name: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.getAccessKeys();
      setKeys(result.keys);
      setModels(result.available_models);
      setNewModels(current => current.length ? current : ['*']);
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const modelByAlias = useMemo(
    () => new Map(models.map(model => [model.alias, model])),
    [models],
  );

  const toggleModel = (alias: string, selected: string[], setter: (next: string[]) => void) => {
    const explicit = selected.includes('*') ? models.map(model => model.alias) : selected;
    setter(explicit.includes(alias) ? explicit.filter(item => item !== alias) : [...explicit, alias]);
  };

  const createKey = async () => {
    if (!newName.trim() || !newModels.length) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.createAccessKey({ name: newName.trim(), allowed_models: newModels });
      setRevealedKey({ key: result.key, name: result.record.name });
      setCopied(false);
      setNewName('');
      await load();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const beginEdit = (record: AccessKeyRecord) => {
    setEditingId(record.id);
    setEditName(record.name);
    setEditModels([...record.allowed_models]);
  };

  const saveEdit = async (record: AccessKeyRecord) => {
    if (!editName.trim() || !editModels.length) return;
    setBusy(true);
    setError('');
    try {
      await api.updateAccessKey(record.id, {
        name: editName.trim(),
        allowed_models: editModels,
        enabled: record.enabled,
      });
      setEditingId(null);
      await load();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const setEnabled = async (record: AccessKeyRecord, enabled: boolean) => {
    setBusy(true);
    setError('');
    try {
      await api.updateAccessKey(record.id, {
        name: record.name,
        allowed_models: record.allowed_models,
        enabled,
      });
      await load();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const rotateKey = async (record: AccessKeyRecord) => {
    if (!window.confirm(tl(['轮换后旧密匙将立即失效。确定继续？', 'The old key will stop working immediately. Continue?']))) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.rotateAccessKey(record.id);
      setRevealedKey({ key: result.key, name: result.record.name });
      setCopied(false);
      await load();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const deleteKey = async (record: AccessKeyRecord) => {
    if (!window.confirm(tl([`确定删除“${record.name}”？客户端将立即无法继续使用。`, `Delete “${record.name}”? Its clients will lose access immediately.`]))) return;
    setBusy(true);
    setError('');
    try {
      await api.deleteAccessKey(record.id);
      await load();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setBusy(false);
    }
  };

  const copyRevealedKey = async () => {
    if (!revealedKey) return;
    try {
      await navigator.clipboard.writeText(revealedKey.key);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  const formatDate = (value: string | null) => {
    if (!value) return tl(['从未使用', 'Never']);
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(lang === 'zh' ? 'zh-CN' : 'en-US');
  };

  const modelPicker = (selected: string[], setter: (next: string[]) => void) => (
    <div className="access-model-picker">
      <label className={selected.includes('*') ? 'selected' : ''}>
        <input
          type="checkbox"
          checked={selected.includes('*')}
          onChange={() => setter(selected.includes('*') ? models.map(model => model.alias) : ['*'])}
        />
        <span>
          <strong>{tl(['全部模型（含 Codex 内部模型）', 'All models, including internal Codex models'])}</strong>
          <code>*</code>
        </span>
        <small>{tl(['不限制', 'UNRESTRICTED'])}</small>
      </label>
      {models.map(model => (
        <label key={model.alias} className={selected.includes(model.alias) ? 'selected' : ''}>
          <input
            type="checkbox"
            checked={selected.includes(model.alias)}
            onChange={() => toggleModel(model.alias, selected, setter)}
          />
          <span>
            <strong>{model.display_name || model.alias}</strong>
            <code>{model.alias}</code>
          </span>
          <small>{model.route_kind === 'native_codex' ? 'OPENAI' : 'CUSTOM'}</small>
        </label>
      ))}
    </div>
  );

  return (
    <div className="page access-keys-page">
      <div className="access-keys-hero">
        <div>
          <span className="eyebrow">CLIENT ACCESS CONTROL</span>
          <h2>{tl(['访问密匙', 'Access Keys'])}</h2>
            <p>{tl(['为每台客户端分配独立密匙。默认允许全部模型，也可按需限制；完整密匙只在创建或轮换时显示一次。', 'Issue a separate key to each client. Keys allow all models by default, or can be restricted as needed. The full key is shown only once after creation or rotation.'])}</p>
        </div>
        <div className="access-summary">
          <strong>{keys.length}</strong>
          <span>{tl(['已分配密匙', 'Issued keys'])}</span>
        </div>
      </div>

      {revealedKey && (
        <section className="access-secret-panel" role="alert">
          <div>
            <strong>{tl(['请立即复制并安全保存', 'Copy and store this key now'])}</strong>
            <p>{tl([`“${revealedKey.name}”的完整密匙关闭后无法再次查看。`, `The full key for “${revealedKey.name}” cannot be viewed again after this panel is closed.`])}</p>
          </div>
          <code>{revealedKey.key}</code>
          <div className="btn-row">
            <button className="btn btn-primary" onClick={copyRevealedKey}>{copied ? tl(['已复制', 'Copied']) : tl(['复制密匙', 'Copy key'])}</button>
            <button className="btn" onClick={() => { setRevealedKey(null); setCopied(false); }}>{tl(['我已保存，关闭', 'I saved it, close'])}</button>
          </div>
        </section>
      )}

      {error && <div className="test-result error">{tl('common.error')}: {error}</div>}

      <section className="access-create-card">
        <div className="section-heading">
          <div>
            <h3>{tl(['分配新密匙', 'Issue a new key'])}</h3>
            <p>{tl(['建议按设备或用途命名，便于后续停用和统计。', 'Name it after the device or purpose for easier revocation and reporting.'])}</p>
          </div>
        </div>
        <div className="access-create-row">
          <div className="form-group">
            <label>{tl(['密匙名称', 'Key name'])}</label>
            <input value={newName} onChange={event => setNewName(event.target.value)} placeholder={tl(['例如：工作室电脑', 'e.g. Studio PC'])} />
          </div>
          <button className="btn btn-primary" disabled={busy || !newName.trim() || !newModels.length} onClick={createKey}>
            {tl(['生成密匙', 'Generate key'])}
          </button>
        </div>
        <div className="access-picker-heading">
          <span>{tl(['允许使用的模型', 'Allowed models'])}</span>
          <div>
            <button className="btn btn-sm" onClick={() => setNewModels(['*'])}>{tl(['不限制模型', 'Allow all'])}</button>
            <button className="btn btn-sm" onClick={() => setNewModels(models.map(model => model.alias))}>{tl(['仅选目录全部', 'Select catalog'])}</button>
            <button className="btn btn-sm" onClick={() => setNewModels([])}>{tl(['清空', 'Clear'])}</button>
          </div>
        </div>
        {models.length ? modelPicker(newModels, setNewModels) : <p className="muted">{tl(['暂无可分配模型', 'No models are available'])}</p>}
      </section>

      <section className="access-key-list">
        <div className="section-heading">
          <div>
            <h3>{tl(['已分配密匙', 'Issued keys'])}</h3>
            <p>{tl(['列表仅显示密匙前缀；请求数和 Token 用量由桥接器累计。', 'Only key prefixes are listed; request and token usage are accumulated by the bridge.'])}</p>
          </div>
          <button className="btn btn-sm" disabled={loading} onClick={load}>{tl(['刷新', 'Refresh'])}</button>
        </div>

        {loading ? <div className="library-empty">{tl(['正在加载...', 'Loading...'])}</div> : keys.length === 0 ? (
          <div className="library-empty">{tl(['尚未分配访问密匙', 'No access keys have been issued'])}</div>
        ) : keys.map(record => {
          const editing = editingId === record.id;
          return (
            <article key={record.id} className={`access-key-card ${record.enabled ? '' : 'disabled'}`}>
              <div className="access-key-header">
                <div>
                  {editing ? (
                    <input className="access-name-input" value={editName} onChange={event => setEditName(event.target.value)} />
                  ) : <h4>{record.name}</h4>}
                  <code>{record.prefix}••••••••</code>
                </div>
                <label className="provider-toggle">
                  <span className="toggle-text">{record.enabled ? tl('common.enabled') : tl('common.disabled')}</span>
                  <span className="toggle-switch">
                    <input type="checkbox" checked={record.enabled} disabled={busy || editing} onChange={event => setEnabled(record, event.target.checked)} />
                    <span className="toggle-track"><span className="toggle-thumb" /></span>
                  </span>
                </label>
              </div>

              {editing ? modelPicker(editModels, setEditModels) : (
                <div className="access-model-tags">
                  {record.allowed_models.includes('*') ? (
                    <span>{tl(['全部模型（含 Codex 内部模型）', 'All models, including internal Codex models'])}<code>*</code></span>
                  ) : record.allowed_models.map(alias => <span key={alias}>{modelByAlias.get(alias)?.display_name || alias}<code>{alias}</code></span>)}
                </div>
              )}

              <div className="access-key-stats">
                <span><strong>{formatNumber(record.request_count)}</strong>{tl(['请求', 'requests'])}</span>
                <span><strong>{formatNumber(record.total_tokens)}</strong>Tokens</span>
                <span><strong>{formatDate(record.last_used_at)}</strong>{tl(['最近使用', 'last used'])}</span>
                <span><strong>{formatDate(record.created_at)}</strong>{tl(['创建时间', 'created'])}</span>
              </div>

              <div className="row-actions access-key-actions">
                {editing ? (
                  <>
                    <button className="btn btn-sm btn-primary" disabled={busy || !editName.trim() || !editModels.length} onClick={() => saveEdit(record)}>{tl('common.save')}</button>
                    <button className="btn btn-sm" onClick={() => setEditingId(null)}>{tl('common.cancel')}</button>
                  </>
                ) : (
                  <>
                    <button className="btn btn-sm" disabled={busy} onClick={() => beginEdit(record)}>{tl('common.edit')}</button>
                    <button className="btn btn-sm btn-outline" disabled={busy} onClick={() => rotateKey(record)}>{tl(['轮换密匙', 'Rotate key'])}</button>
                    <button className="btn btn-sm btn-danger" disabled={busy} onClick={() => deleteKey(record)}>{tl('common.delete')}</button>
                  </>
                )}
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
};

export default AccessKeys;
