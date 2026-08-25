import type { ProxyStatus, ModelConfig, SlotConfig, ServerSettings, RequestLogEntry, TestResult, CodexConfigStatus, WebSearchSettings, WebSearchTestResult, AccessKeysResponse, AccessKeySecretResponse, AccessKeyRecord } from '../types';

const BASE = 'http://127.0.0.1:8765';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  // 状态
  getStatus: () => request<ProxyStatus>('/admin/api/status'),

  // 模型 CRUD
  getModels: () => request<{ models: ModelConfig[] }>('/admin/api/models'),
  addModel: (data: Record<string, unknown>) =>
    request<{ status: string }>('/admin/api/models', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateModel: (alias: string, data: Record<string, unknown>) =>
    request<{ status: string }>(`/admin/api/models/${encodeURIComponent(alias)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteModel: (alias: string) =>
    request<{ status: string }>(`/admin/api/models/${encodeURIComponent(alias)}`, {
      method: 'DELETE',
    }),
  testConnection: (alias: string, data?: Record<string, unknown>) =>
    request<TestResult>(`/admin/api/models/${encodeURIComponent(alias)}/test`, {
      method: 'POST',
      body: JSON.stringify(data || {}),
    }),

  // 设置
  getSettings: () => request<ServerSettings>('/admin/api/settings'),
  updateSettings: (data: Record<string, unknown>) =>
    request<{ status: string; message: string }>('/admin/api/settings', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  getWebSearchSettings: () => request<WebSearchSettings>('/admin/api/web-search'),
  updateWebSearchSettings: (data: Record<string, unknown>) =>
    request<{ status: string; message: string; web_search: WebSearchSettings }>('/admin/api/web-search', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  testWebSearch: (data: Record<string, unknown>) =>
    request<WebSearchTestResult>('/admin/api/web-search/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  // 日志
  getLogs: (limit = 100) => request<{ logs: RequestLogEntry[] }>(`/admin/api/logs?limit=${limit}`),
  clearLogs: () => request<{ status: string }>('/admin/api/logs/clear', { method: 'POST' }),

  // 配置导入导出
  exportConfig: () => request<{ yaml: string; config_path: string }>('/admin/api/config/export'),
  importConfig: (yaml: string) =>
    request<{ status: string; error?: string }>('/admin/api/config/import', {
      method: 'POST',
      body: JSON.stringify({ yaml }),
    }),

  // 关闭
  shutdown: () => request<{ status: string }>('/admin/api/shutdown', { method: 'POST' }),

  // Codex 配置切换
  getCodexStatus: () => request<CodexConfigStatus>('/admin/api/codex/status'),
  enableUnifiedRouting: () => request<{ status: string; message: string }>('/admin/api/codex/enable-unified', { method: 'POST' }),
  restoreOfficialRouting: () => request<{ status: string; message: string }>('/admin/api/codex/restore-official', { method: 'POST' }),
  // Compatibility aliases for older callers.
  switchCodexToCustom: () => request<{ status: string; message: string; catalog_available: boolean }>('/admin/api/codex/switch-to-custom', { method: 'POST' }),
  switchCodexToOfficial: () => request<{ status: string; message: string }>('/admin/api/codex/switch-to-official', { method: 'POST' }),


  // Slots
  getSlots: () => request<{ slots: SlotConfig[] }>('/admin/api/slots'),
  updateSlot: (slotId: string, data: Record<string, unknown>) =>
    request<{ status: string; slot_id: string }>('/admin/api/slots/' + slotId, { method: 'PUT', body: JSON.stringify(data) }),
  testSlot: (slotId: string, data?: Record<string, string>) =>
    request<TestResult>('/admin/api/slots/' + slotId + '/test', { method: 'POST', body: JSON.stringify(data || {}) }),

  // Client access keys. Full secrets are returned only by create/rotate.
  getAccessKeys: () => request<AccessKeysResponse>('/admin/api/access-keys'),
  createAccessKey: (data: { name: string; allowed_models: string[] }) =>
    request<AccessKeySecretResponse>('/admin/api/access-keys', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateAccessKey: (id: string, data: { name: string; allowed_models: string[]; enabled: boolean }) =>
    request<AccessKeyRecord>(`/admin/api/access-keys/${encodeURIComponent(id)}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  rotateAccessKey: (id: string) =>
    request<AccessKeySecretResponse>(`/admin/api/access-keys/${encodeURIComponent(id)}/rotate`, {
      method: 'POST',
    }),
  deleteAccessKey: (id: string) =>
    request<{ status: string }>(`/admin/api/access-keys/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
};
