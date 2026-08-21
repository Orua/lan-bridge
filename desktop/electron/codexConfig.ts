const BRIDGE_TOP_LEVEL_KEYS = new Set([
  'model_provider',
  'openai_base_url',
  'model_catalog_json',
  'model_context_window',
  'model_auto_compact_token_limit',
  'tool_output_token_limit',
]);

const BRIDGE_ONLY_SECTIONS = new Set([
  'model_providers.cnbridge',
  'model_providers.custom',
  'features.network_proxy',
]);

/**
 * Restore OpenAI routing without replacing the user's config.toml.
 * MCP servers, plugins, projects, skills, hooks, and unrelated settings are
 * copied byte-for-byte at the line level. Only Bridge-owned routing fields are
 * removed and the official model is selected.
 */
export function restoreOfficialConfigPreservingUserSettings(
  content: string,
  officialModel = 'gpt-5.6-sol',
): string {
  if (!content.replace(/^\uFEFF/, '').trim()) return `model = "${officialModel}"\n`;
  const hadTrailingNewline = /\r?\n$/.test(content);
  const lines = content.replace(/^\uFEFF/, '').split(/\r?\n/);
  const output: string[] = [];
  let section = '';
  let skipSection = false;
  let modelWritten = false;

  for (const line of lines) {
    const sectionMatch = line.match(/^\s*\[([^\]]+)\]\s*(?:#.*)?$/);
    if (sectionMatch) {
      section = sectionMatch[1].trim();
      skipSection = BRIDGE_ONLY_SECTIONS.has(section);
      if (!skipSection) output.push(line);
      continue;
    }
    if (skipSection) continue;

    const keyMatch = line.match(/^\s*([A-Za-z0-9_-]+)\s*=/);
    const key = keyMatch?.[1] || '';
    if (!section && BRIDGE_TOP_LEVEL_KEYS.has(key)) continue;
    if (section === 'features' && key === 'enable_request_compression') continue;
    if (!section && key === 'model') {
      if (!modelWritten) output.push(`model = "${officialModel}"`);
      modelWritten = true;
      continue;
    }
    output.push(line);
  }

  if (!modelWritten) {
    const firstSection = output.findIndex(line => /^\s*\[/.test(line));
    const insertAt = firstSection < 0 ? output.length : firstSection;
    output.splice(insertAt, 0, `model = "${officialModel}"`, '');
  }

  while (output.length > 1 && !output[output.length - 1] && !output[output.length - 2]) {
    output.pop();
  }
  const result = output.join('\n');
  return hadTrailingNewline || result ? `${result.replace(/\n*$/, '')}\n` : '';
}
