export interface BridgeRestartState {
  desiredRunning: boolean;
  isQuitting: boolean;
  restartCount: number;
  maxRestarts: number;
}

/**
 * A bridge should restart after every unrequested exit, regardless of the
 * child's numeric exit code. Manual stop and application quit are represented
 * explicitly so a graceful exit can never race into an unwanted restart.
 */
export function shouldRestartBridge(state: BridgeRestartState): boolean {
  return state.desiredRunning && !state.isQuitting;
}

/**
 * Keep retrying forever, but cap the retry delay so a crash loop cannot spin
 * aggressively or permanently disable the service.
 */
export function bridgeRestartDelayMs(restartCount: number, maxBackoffStep = 5): number {
  const step = Math.max(0, Math.min(Math.trunc(restartCount), maxBackoffStep));
  return Math.min(30_000, 1_000 * (2 ** step));
}

export function shouldRecoverUnownedBridge(state: {
  desiredRunning: boolean;
  isQuitting: boolean;
  hasChildProcess: boolean;
  startInFlight: boolean;
}): boolean {
  return state.desiredRunning
    && !state.isQuitting
    && !state.hasChildProcess
    && !state.startInFlight;
}

/** Read a simple boolean from the YAML `server:` mapping without pulling a
 * YAML parser into the Electron main bundle. Values elsewhere in the file are
 * deliberately ignored so similarly named provider keys cannot override app
 * lifecycle behavior. */
export function readServerBooleanSetting(
  content: string,
  key: string,
  fallback: boolean,
): boolean {
  const lines = content.replace(/^\uFEFF/, '').split(/\r?\n/);
  let serverIndent: number | null = null;
  for (const rawLine of lines) {
    const withoutComment = rawLine.replace(/\s+#.*$/, '');
    const trimmed = withoutComment.trim();
    if (!trimmed) continue;
    const indent = withoutComment.length - withoutComment.trimStart().length;
    if (serverIndent === null) {
      if (/^server\s*:\s*$/.test(trimmed)) serverIndent = indent;
      continue;
    }
    if (indent <= serverIndent) break;
    const match = trimmed.match(new RegExp(`^${key}\\s*:\\s*(true|false)\\s*$`, 'i'));
    if (match) return match[1].toLowerCase() === 'true';
  }
  return fallback;
}
