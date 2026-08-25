export interface ProxyStatus {
  running: boolean;
  host: string;
  port: number;
  version: string;
  stats: {
    uptime_seconds: number;
    request_count: number;
    success_count: number;
    error_count: number;
    avg_latency_ms: number;
    avg_response_ms: number;
    avg_first_response_ms: number;
  };
}

export interface ModelConfig {
  alias: string;
  display_name: string;
  description: string;
  target_model: string;
  provider: string;
  adapter: string;
  wire_api: 'chat' | 'responses';
  route_kind: 'native_codex' | 'custom';
  read_only: boolean;
  capabilities: Record<string, unknown>;
  effective_context_window?: number;
  effective_auto_compact_token_limit?: number;
  default_context_window?: number;
  default_auto_compact_token_limit?: number;
  base_url: string;
  api_key_env: string;
  use_proxy: boolean;
  proxy_url: string;
  api_key_set: boolean;
  enabled: boolean;
  is_multimodal: boolean;
  vision_alias: string;
  is_image_gen: boolean;
  image_gen_alias: string;
  is_video_gen: boolean;
  video_gen_alias: string;
  is_reasoning_text?: boolean;
  available_adapters: string[];
}


export interface SlotConfig {
  slot_id: string;
  label: string;
  alias: string;
  target_model: string;
  provider: string;
  adapter: string;
  base_url: string;
  api_key_env: string;
  api_key_set: boolean;
  enabled: boolean;
  wire_api?: 'chat' | 'responses';
  is_responses?: boolean;
  is_multimodal: boolean;
  is_image_gen: boolean;
  is_video_gen: boolean;
  is_reasoning_text?: boolean;
  available_adapters: string[];
}

export interface ServerSettings {
  server: {
    host: string;
    port: number;
    log_level: string;
    launch_at_login: boolean;
    auto_start: boolean;
    close_to_tray: boolean;
    audit_log_path: string;
    codex_official_proxy_url: string;
    native_auth_injection: {
      enabled: boolean;
      auth_file: string;
      auth_file_found: boolean;
    };
  };
  config_path: string;
}

export interface WebSearchProviderConfig {
  adapter: string;
  display_name: string;
  base_url: string;
  api_key_env: string;
  api_key_set: boolean;
  enabled: boolean;
  timeout: number;
  max_results: number;
  summary: boolean;
  freshness: string;
}

export interface WebSearchSettings {
  enabled: boolean;
  active_provider: string;
  providers: Record<string, WebSearchProviderConfig>;
}

export interface WebSearchTestResult {
  status: 'ok' | 'error';
  message: string;
  elapsed_ms?: number;
  result_count?: number;
  results?: { title: string; url: string; snippet: string }[];
}

export interface RequestLogEntry {
  timestamp: number;
  time: string;
  model: string;
  endpoint: string;
  status_code: number;
  elapsed_ms: number;
  tokens: number;
  error: string;
  stream: boolean;
  provider: string;
  target_model: string;
  upstream_api: 'chat' | 'responses' | string;
  client_ip: string;
}

export interface TestResult {
  status: 'ok' | 'error';
  elapsed_ms?: number;
  message: string;
}

export interface CodexConfigStatus {
  exists: boolean;
  using_bridge: boolean;
  details: Record<string, string>;
  catalog_available: boolean;
}

export interface AccessKeyRecord {
  id: string;
  name: string;
  prefix: string;
  allowed_models: string[];
  enabled: boolean;
  created_at: string;
  last_used_at: string | null;
  request_count: number;
  total_tokens: number;
}

export interface AccessKeyAvailableModel {
  alias: string;
  display_name: string;
  route_kind: 'native_codex' | 'custom' | string;
}

export interface AccessKeysResponse {
  keys: AccessKeyRecord[];
  available_models: AccessKeyAvailableModel[];
}

export interface AccessKeySecretResponse {
  key: string;
  record: AccessKeyRecord;
}
