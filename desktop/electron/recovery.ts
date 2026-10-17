import fs from 'fs';
import path from 'path';
import { spawn } from 'child_process';

export interface RecoveryState {
  enabled: boolean;
  desiredRunning: boolean;
  desktopPid: number;
  desktopExecutable: string;
  version: string;
}

export function writeRecoveryState(file: string, state: RecoveryState): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temporary, JSON.stringify({ ...state, updatedAt: new Date().toISOString() }) + '\n', 'utf8');
    fs.renameSync(temporary, file);
  } finally {
    if (fs.existsSync(temporary)) fs.unlinkSync(temporary);
  }
}

export function installRecoveryTask(script: string, stateFile: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn('powershell.exe', [
      '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass',
      '-File', script, '-Action', 'Install', '-StatePath', stateFile,
    ], { windowsHide: true, stdio: 'ignore' });
    child.once('error', reject);
    child.once('close', code => code === 0 ? resolve() : reject(new Error(`Recovery task setup exited ${code}`)));
  });
}
