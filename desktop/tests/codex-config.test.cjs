const test = require('node:test');
const assert = require('node:assert/strict');

const { restoreOfficialConfigPreservingUserSettings } = require('../dist-electron/codexConfig.js');

test('official restore removes only bridge routing and preserves all MCP servers', () => {
  const source = [
    'openai_base_url = "http://127.0.0.1:8765/v1"',
    'model = "deepseek-v4-pro"',
    'model_catalog_json = "C:\\\\models.json"',
    '',
    '[features]',
    'enable_request_compression = false',
    'other_feature = true',
    '',
    '[model_providers.cnbridge]',
    'base_url = "http://127.0.0.1:8765/v1"',
    '',
    '[mcp_servers.example]',
    'url = "https://example.invalid/mcp"',
    'bearer_token_env_var = "EXAMPLE_MCP_TOKEN"',
    '',
    '[mcp_servers.example_stats]',
    'url = "https://example.invalid/stats/mcp"',
    '',
    '[mcp_servers.example_write]',
    'url = "https://example.invalid/write/mcp"',
    '',
    '[projects."C:\\\\work"]',
    'trust_level = "trusted"',
    '',
  ].join('\n');

  const restored = restoreOfficialConfigPreservingUserSettings(source);

  assert.match(restored, /^model = "gpt-5\.6-sol"$/m);
  assert.doesNotMatch(restored, /openai_base_url|model_catalog_json|model_providers\.cnbridge|enable_request_compression/);
  assert.match(restored, /\[mcp_servers\.example\]/);
  assert.match(restored, /\[mcp_servers\.example_stats\]/);
  assert.match(restored, /\[mcp_servers\.example_write\]/);
  assert.match(restored, /other_feature = true/);
  assert.match(restored, /\[projects\."C:\\\\work"\]/);
});

test('official restore seeds a model without deleting an otherwise empty config', () => {
  assert.equal(restoreOfficialConfigPreservingUserSettings(''), 'model = "gpt-5.6-sol"\n');
});
