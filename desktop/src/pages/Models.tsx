import React, { useEffect, useState } from 'react';
import { api } from '../services/api';
import { useApp } from '../App';
import type { ModelConfig, SlotConfig, TestResult } from '../types';

type SlotType = 'text' | 'reasoning_text' | 'responses' | 'vision' | 'image_gen' | 'video_gen';

const SLOT_META: Record<SlotType, { title: [string, string]; hint: [string, string] }> = {
  text: { title: ['普通文本默认', 'Default text'], hint: ['常规文本任务', 'General text tasks'] },
  reasoning_text: { title: ['推理文本默认', 'Default reasoning'], hint: ['中高强度推理任务', 'Reasoning-heavy tasks'] },
  responses: { title: ['Responses 直连', 'Direct Responses'], hint: ['上游原生 Responses API，不经过 Chat 转换', 'Native upstream Responses API without Chat translation'] },
  vision: { title: ['视觉默认', 'Default vision'], hint: ['图片理解任务', 'Image understanding'] },
  image_gen: { title: ['生图默认', 'Default image generation'], hint: ['图片生成任务', 'Image generation'] },
  video_gen: { title: ['视频默认', 'Default video generation'], hint: ['视频生成任务', 'Video generation'] },
};

const EMPTY_FORM = {
  alias: '', display_name: '', description: '', target_model: '', provider: '', adapter: 'openai',
  wire_api: 'chat' as 'chat' | 'responses',
  base_url: '', api_key: '', api_key_env: '', use_proxy: false, proxy_url: '', enabled: true, is_multimodal: false,
  is_reasoning_text: false, is_image_gen: false, is_video_gen: false,
  context_window: '', auto_compact_token_limit: '',
};

type ModelForm = typeof EMPTY_FORM;

function compatible(slot: SlotType, model: ModelConfig): boolean {
  if (model.read_only || !model.enabled) return false;
  if (slot === 'image_gen') return model.is_image_gen || Boolean(model.capabilities?.image_generation);
  if (slot === 'video_gen') return model.is_video_gen || Boolean(model.capabilities?.video_generation);
  if (slot === 'vision') return model.is_multimodal || Boolean(model.capabilities?.vision);
  if (slot === 'responses') return model.wire_api === 'responses';
  if (slot === 'reasoning_text') return model.wire_api !== 'responses' && (model.is_reasoning_text || Boolean(model.capabilities?.reasoning));
  return model.wire_api !== 'responses' && !model.is_image_gen && !model.is_video_gen;
}

const Models: React.FC = () => {
  const { tl } = useApp();
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [slots, setSlots] = useState<SlotConfig[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<ModelForm>(EMPTY_FORM);
  const [baseCapabilities, setBaseCapabilities] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState('');
  const [result, setResult] = useState<TestResult | null>(null);

  const load = async () => {
    const [modelResult, slotResult] = await Promise.all([api.getModels(), api.getSlots()]);
    setModels(modelResult.models || []);
    setSlots(slotResult.slots || []);
  };

  useEffect(() => { load().catch(() => setModels([])); }, []);

  const official = models.filter(model => model.read_only);
  const custom = models.filter(model => !model.read_only);

  const openForm = (model?: ModelConfig) => {
    setEditing(model?.alias || '__new__');
    setResult(null);
    setBaseCapabilities(model?.capabilities || {});
    setForm(model ? {
      alias: model.alias,
      display_name: model.display_name || model.alias,
      description: model.description || '',
      target_model: model.target_model,
      provider: model.provider,
      adapter: model.adapter || 'openai',
      wire_api: model.wire_api || 'chat',
      base_url: model.base_url || '',
      api_key: '',
      api_key_env: model.api_key_env || '',
      use_proxy: Boolean(model.use_proxy),
      proxy_url: model.proxy_url || '',
      enabled: model.enabled,
      is_multimodal: model.is_multimodal,
      is_reasoning_text: Boolean(model.is_reasoning_text),
      is_image_gen: model.is_image_gen,
      is_video_gen: model.is_video_gen,
      context_window: model.capabilities?.context_window ? String(model.capabilities.context_window) : '',
      auto_compact_token_limit: model.capabilities?.auto_compact_token_limit ? String(model.capabilities.auto_compact_token_limit) : '',
    } : { ...EMPTY_FORM });
  };

  const payload = () => {
    const capabilities: Record<string, unknown> = {
      ...baseCapabilities,
      vision: form.is_multimodal,
      reasoning: form.is_reasoning_text,
      image_generation: form.is_image_gen,
      video_generation: form.is_video_gen,
    };
    if (form.context_window.trim()) capabilities.context_window = Number(form.context_window);
    else delete capabilities.context_window;
    if (form.auto_compact_token_limit.trim()) capabilities.auto_compact_token_limit = Number(form.auto_compact_token_limit);
    else delete capabilities.auto_compact_token_limit;
    return { ...form, capabilities };
  };

  const tokenLabel = (value?: number) => value ? value.toLocaleString() : '-';

  const save = async () => {
    if (form.use_proxy && !form.proxy_url.trim()) {
      setResult({ status: 'error', message: tl(['启用代理时必须填写代理地址', 'A proxy URL is required when proxy is enabled']) });
      return;
    }
    setBusy('save');
    setResult(null);
    try {
      if (editing === '__new__') await api.addModel(payload());
      else await api.updateModel(editing!, payload());
      setEditing(null);
      await load();
    } catch (error: any) {
      setResult({ status: 'error', message: error.message || String(error) });
    } finally { setBusy(''); }
  };

  const test = async (alias: string, data?: Record<string, unknown>) => {
    if (data?.use_proxy && !String(data.proxy_url || '').trim()) {
      setResult({ status: 'error', message: tl(['启用代理时必须填写代理地址', 'A proxy URL is required when proxy is enabled']) });
      return;
    }
    setBusy(`test:${alias}`);
    setResult(null);
    try { setResult(await api.testConnection(alias, data)); }
    catch (error: any) { setResult({ status: 'error', message: error.message || String(error) }); }
    finally { setBusy(''); }
  };

  const remove = async (alias: string) => {
    if (!window.confirm(tl(['确定删除此第三方模型？', 'Delete this custom model?']))) return;
    setBusy(`delete:${alias}`);
    try { await api.deleteModel(alias); await load(); }
    finally { setBusy(''); }
  };

  const toggle = async (model: ModelConfig) => {
    setBusy(`toggle:${model.alias}`);
    try { await api.updateModel(model.alias, { enabled: !model.enabled }); await load(); }
    finally { setBusy(''); }
  };

  const assignSlot = async (slotId: string, alias: string) => {
    if (!alias) return;
    setBusy(`slot:${slotId}`);
    try { await api.updateSlot(slotId, { alias }); await load(); }
    finally { setBusy(''); }
  };

  return (
    <div className="models-page model-library">
      <header className="library-hero">
        <div>
          <span className="eyebrow">CN BRIDGE / MODEL REGISTRY</span>
          <h2>{tl(['统一模型库', 'Unified model library'])}</h2>
          <p>{tl(['官方 Codex 模型保持原生直通；第三方模型使用独立凭据和适配器。', 'Official Codex models stay native; custom models use isolated credentials and adapters.'])}</p>
        </div>
        <button className="btn btn-primary" onClick={() => openForm()}>{tl(['添加第三方模型', 'Add custom model'])}</button>
      </header>

      <section className="library-section native-section">
        <div className="section-heading">
          <div><h3>{tl(['官方 Codex 模型', 'Official Codex models'])}</h3><p>{tl(['只读，使用 ChatGPT 登录态原生透传', 'Read-only, native passthrough using ChatGPT authentication'])}</p></div>
          <span className="route-pill native">NATIVE</span>
        </div>
        <div className="library-grid">
          {official.map(model => <article className="library-card native-card" key={model.alias}>
            <div className="model-card-top"><span className="model-glyph">O</span><span className="route-pill">READ ONLY</span></div>
            <h4>{model.display_name}</h4><code>{model.alias}</code>
            <p>{model.description}</p>
          </article>)}
        </div>
      </section>

      <section className="library-section">
        <div className="section-heading">
          <div><h3>{tl(['第三方模型', 'Custom models'])}</h3><p>{tl(['可独立新增、编辑、测试、停用和删除', 'Add, edit, test, disable, or delete independently'])}</p></div>
          <span className="model-count">{custom.length}</span>
        </div>
        {custom.length === 0 && <div className="library-empty">{tl(['暂无第三方模型', 'No custom models'])}</div>}
        <div className="custom-model-list">
          {custom.map(model => <article className={`custom-model-row${model.enabled ? '' : ' disabled'}`} key={model.alias}>
            <div className="custom-model-identity"><span className="model-glyph custom">C</span><div><h4>{model.display_name || model.alias}</h4><code>{model.alias}</code></div></div>
            <div className="custom-model-route"><strong>{model.provider}</strong><span>{model.target_model}</span></div>
            <div className="capability-tags">
              {model.is_reasoning_text && <span>Reasoning</span>}{model.is_multimodal && <span>Vision</span>}
              {model.is_image_gen && <span>Image</span>}{model.is_video_gen && <span>Video</span>}
              {!model.is_reasoning_text && !model.is_multimodal && !model.is_image_gen && !model.is_video_gen && <span>Text</span>}
              {!model.is_image_gen && !model.is_video_gen && <span>{tokenLabel(model.effective_context_window)} ctx</span>}
              <span className={model.use_proxy ? 'proxy-enabled' : 'proxy-direct'}>{model.use_proxy ? 'VPN' : 'DIRECT'}</span>
            </div>
            <div className="row-actions">
              <button className="btn btn-sm" onClick={() => test(model.alias)} disabled={Boolean(busy)}>{tl(['测试', 'Test'])}</button>
              <button className="btn btn-sm" onClick={() => openForm(model)}>{tl(['编辑', 'Edit'])}</button>
              <button className="btn btn-sm" onClick={() => toggle(model)} disabled={Boolean(busy)}>{model.enabled ? tl(['停用', 'Disable']) : tl(['启用', 'Enable'])}</button>
              <button className="btn btn-sm btn-danger-soft" onClick={() => remove(model.alias)} disabled={Boolean(busy)}>{tl(['删除', 'Delete'])}</button>
            </div>
          </article>)}
        </div>
      </section>

      {editing && <section className="model-editor">
        <div className="section-heading"><div><h3>{editing === '__new__' ? tl(['添加第三方模型', 'Add custom model']) : tl(['编辑第三方模型', 'Edit custom model'])}</h3><p>{tl(['API Key 仅用于该第三方 Provider，不会接收 OpenAI OAuth。', 'The API key is scoped to this custom provider; OpenAI OAuth is never forwarded.'])}</p></div></div>
        <div className="editor-grid">
          <label>{tl(['模型别名', 'Model alias'])}<input value={form.alias} disabled={editing !== '__new__'} onChange={e => setForm({ ...form, alias: e.target.value })} /></label>
          <label>{tl(['显示名称', 'Display name'])}<input value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} /></label>
          <label>{tl(['Provider 名称', 'Provider name'])}<input value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })} /></label>
          <label>{tl(['上游模型 ID', 'Upstream model ID'])}<input value={form.target_model} onChange={e => setForm({ ...form, target_model: e.target.value })} /></label>
          <label>{tl(['适配器', 'Adapter'])}<input value={form.adapter} onChange={e => setForm({ ...form, adapter: e.target.value })} /></label>
          <label>{tl(['连接方式', 'Connection API'])}<select value={form.wire_api} onChange={e => setForm({ ...form, wire_api: e.target.value as 'chat' | 'responses' })}>
            <option value="chat">Chat Completions</option>
            <option value="responses">Responses</option>
          </select></label>
          <label>{tl(['API 地址', 'API endpoint'])}<input value={form.base_url} onChange={e => setForm({ ...form, base_url: e.target.value })} /></label>
          <label>API Key<input type="password" value={form.api_key} onChange={e => setForm({ ...form, api_key: e.target.value })} placeholder={tl(['留空则保留现有密钥', 'Leave blank to keep existing key'])} /></label>
          <label>{tl(['密钥环境变量', 'Key environment variable'])}<input value={form.api_key_env} onChange={e => setForm({ ...form, api_key_env: e.target.value })} /></label>
          <label className="proxy-toggle"><span>{tl(['使用代理', 'Use proxy'])}</span><span className="proxy-toggle-control"><input type="checkbox" checked={form.use_proxy} onChange={e => setForm({ ...form, use_proxy: e.target.checked })} />{form.use_proxy ? tl(['已开启', 'Enabled']) : tl(['直连', 'Direct'])}</span></label>
          <label>{tl(['代理地址', 'Proxy URL'])}<input value={form.proxy_url} disabled={!form.use_proxy} onChange={e => setForm({ ...form, proxy_url: e.target.value })} placeholder="http://127.0.0.1:19828" /></label>
          <label>{tl(['上下文窗口（tokens）', 'Context window (tokens)'])}<input type="number" min="1" value={form.context_window} onChange={e => setForm({ ...form, context_window: e.target.value })} placeholder={editing === '__new__' ? tl(['留空使用模型默认值', 'Blank uses the model default']) : `${tl(['默认', 'Default'])} ${tokenLabel(models.find(model => model.alias === editing)?.default_context_window)}`} /></label>
          <label>{tl(['自动压缩阈值（tokens）', 'Auto compact limit (tokens)'])}<input type="number" min="1" value={form.auto_compact_token_limit} onChange={e => setForm({ ...form, auto_compact_token_limit: e.target.value })} placeholder={editing === '__new__' ? tl(['留空使用模型默认值', 'Blank uses the model default']) : `${tl(['默认', 'Default'])} ${tokenLabel(models.find(model => model.alias === editing)?.default_auto_compact_token_limit)}`} /></label>
          <label className="editor-wide">{tl(['说明', 'Description'])}<input value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label>
        </div>
        <div className="catalog-note">{tl(['代理按模型独立生效；关闭时强制直连，不会继承系统 VPN。国产模型默认关闭。留空时上下文窗口按模型系列使用默认值。', 'Proxy settings apply per model. When disabled, the model connects directly and never inherits the system VPN. Context fields left blank use model-family defaults.'])}</div>
        <div className="capability-picker">
          {([['is_reasoning_text', 'Reasoning'], ['is_multimodal', 'Vision'], ['is_image_gen', 'Image generation'], ['is_video_gen', 'Video generation']] as const).map(([key, label]) =>
            <label key={key}><input type="checkbox" checked={form[key]} onChange={e => setForm({ ...form, [key]: e.target.checked })} />{label}</label>)}
        </div>
        <div className="slot-actions">
          <button className="btn btn-primary" onClick={save} disabled={busy === 'save'}>{busy === 'save' ? tl(['保存中...', 'Saving...']) : tl(['保存模型', 'Save model'])}</button>
          <button className="btn" onClick={() => test(form.alias || '__new__', payload())} disabled={Boolean(busy)}>{tl(['测试连接', 'Test connection'])}</button>
          <button className="btn btn-ghost" onClick={() => { setEditing(null); setResult(null); }}>{tl(['取消', 'Cancel'])}</button>
        </div>
      </section>}

      <section className="library-section slot-defaults">
        <div className="section-heading"><div><h3>{tl(['默认能力分配', 'Default capability assignments'])}</h3><p>{tl(['Slot 只选择模型库中的默认项，不创建或删除模型。', 'Slots only choose defaults from the model library; they never create or delete models.'])}</p></div></div>
        <div className="slot-default-grid">
          {(Object.keys(SLOT_META) as SlotType[]).map(slotId => {
            const slot = slots.find(item => item.slot_id === slotId);
            // Never hide the persisted selection because of stale capability metadata.
            const options = custom.filter(model => model.alias === slot?.alias || compatible(slotId, model));
            return <label className="slot-default" key={slotId}><span><strong>{tl(SLOT_META[slotId].title)}</strong><small>{tl(SLOT_META[slotId].hint)}</small></span>
              <select value={slot?.alias || ''} onChange={e => assignSlot(slotId, e.target.value)} disabled={busy === `slot:${slotId}`}>
                <option value="">{tl(['未分配', 'Unassigned'])}</option>{options.map(model => <option value={model.alias} key={model.alias}>{model.display_name || model.alias} ({model.alias})</option>)}
              </select></label>;
          })}
        </div>
      </section>

      <div className="catalog-note">{tl(['模型新增、删除或默认项变更后，已打开的 Codex 可能需要重新加载模型目录；Bridge 不会自动关闭或重启 Codex。', 'After catalog changes, an open Codex session may need to reload its model catalog. Bridge never closes or restarts Codex automatically.'])}</div>
      {result && <div className={`test-result floating-result ${result.status === 'ok' ? 'success' : 'error'}`}>{result.message}{result.elapsed_ms ? ` · ${result.elapsed_ms} ms` : ''}</div>}
    </div>
  );
};

export default Models;
