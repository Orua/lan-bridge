import { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, dialog, shell } from 'electron';
import { spawn, ChildProcess } from 'child_process';
import { crashReporter } from 'electron';
import http from 'http';
import net from 'net';
import path from 'path';
import fs from 'fs';
import {
  bridgeRestartDelayMs,
  readServerBooleanSetting,
  shouldRecoverUnownedBridge,
  shouldRestartBridge,
} from './bridgeLifecycle';
import { restoreOfficialConfigPreservingUserSettings } from './codexConfig';

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let bridgeProcess: ChildProcess | null = null;
let bridgeOwnedByApp = false;
let isQuitting = false;
let bridgeRestartCount = 0;
let bridgeDesiredRunning = true;
let bridgeStartInFlight = false;
let bridgeRestartTimer: NodeJS.Timeout | null = null;
let bridgeStableTimer: NodeJS.Timeout | null = null;
let bridgeWatchdogTimer: NodeJS.Timeout | null = null;
let bridgeWatchdogChecking = false;
let bridgeHealthFailureCount = 0;
const MAX_RESTART = 5;
const STARTUP_HEALTH_TIMEOUT_MS = 30_000;
const STABLE_RUN_RESET_MS = 30_000;

const BRIDGE_PORT = 8765;
const isDev = !app.isPackaged;
const DESKTOP_LOG_PATH = path.join(app.getPath('logs'), 'main.log');
const DESKTOP_LOG_MAX_BYTES = 2 * 1024 * 1024;

function formatError(value: unknown): string {
  if (value instanceof Error) return value.stack || value.message;
  return String(value);
}

function writeDesktopLog(level: 'INFO' | 'WARN' | 'ERROR' | 'FATAL', message: string, detail?: unknown): void {
  try {
    fs.mkdirSync(path.dirname(DESKTOP_LOG_PATH), { recursive: true });
    if (fs.existsSync(DESKTOP_LOG_PATH) && fs.statSync(DESKTOP_LOG_PATH).size >= DESKTOP_LOG_MAX_BYTES) {
      const rotated = `${DESKTOP_LOG_PATH}.1`;
      if (fs.existsSync(rotated)) fs.unlinkSync(rotated);
      fs.renameSync(DESKTOP_LOG_PATH, rotated);
    }
    const suffix = detail === undefined ? '' : `\n${formatError(detail)}`;
    fs.appendFileSync(
      DESKTOP_LOG_PATH,
      `${new Date().toISOString()} [${level}] ${message}${suffix}\n`,
      'utf-8',
    );
  } catch {
    // Diagnostics must never be able to terminate the desktop supervisor.
  }
}

function safeSend(channel: string, payload: unknown): void {
  try {
    if (!mainWindow || mainWindow.isDestroyed() || mainWindow.webContents.isDestroyed()) return;
    mainWindow.webContents.send(channel, payload);
  } catch (error) {
    writeDesktopLog('ERROR', `向渲染进程发送 ${channel} 失败；主进程继续运行`, error);
  }
}

try {
  crashReporter.start({ uploadToServer: false, compress: false });
} catch (error) {
  writeDesktopLog('ERROR', '无法启动 Electron 崩溃转储记录', error);
}

process.on('uncaughtException', (error, origin) => {
  writeDesktopLog('FATAL', `Electron 主进程未捕获异常 (${origin})；已阻止进程退出`, error);
});

process.on('unhandledRejection', (reason) => {
  writeDesktopLog('ERROR', 'Electron 主进程未处理的 Promise 拒绝；已阻止进程退出', reason);
});

process.on('exit', (code) => {
  writeDesktopLog('WARN', `Electron 主进程退出，exit_code=${code}, intentional=${isQuitting}`);
});

const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  writeDesktopLog('INFO', '检测到已有 LAN BRIDGE 实例，当前实例正常退出');
  app.quit();
}

app.on('second-instance', () => {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
});

app.commandLine.appendSwitch('proxy-bypass-list', '<-loopback>;127.0.0.1;localhost');

for (const stream of [process.stdout, process.stderr]) {
  stream?.on?.('error', (err: NodeJS.ErrnoException) => {
    if (err.code !== 'EPIPE') {
      // A detached packaged app can lose its inherited console streams.
      // Never crash the Electron main process just because diagnostics cannot
      // be written to stdout/stderr.
      return;
    }
  });
}

function findConfigPath(): string | null {
  // 优先找项目根目录的 config.yaml（适合开发模式）
  const projectConfig = path.join(__dirname, '..', '..', 'config.yaml');
  if (fs.existsSync(projectConfig)) return projectConfig;
  // 其次找用户主目录的配置
  const homeConfig = path.join(app.getPath('home'), '.lan-bridge.yaml');
  if (fs.existsSync(homeConfig)) return homeConfig;
  return null;
}

function getBridgeCommand(): { cmd: string; args: string[] } {
  const configPath = findConfigPath();
  const configArgs: string[] = configPath ? ['-c', configPath] : [];

  if (isDev) {
    return {
      cmd: 'python',
      args: ['-m', 'code_cn_bridge.cli', 'start', '--port', String(BRIDGE_PORT), ...configArgs],
    };
  }
  // 生产模式：使用 PyInstaller 打包的可执行文件
  const exeName = process.platform === 'win32' ? 'lan-bridge.exe' : 'lan-bridge';
  const exePath = path.join(process.resourcesPath, 'backend', exeName);
  if (!fs.existsSync(exePath)) {
    throw new Error(`正式版后端缺失：${exePath}。请重新安装或使用“恢复 Codex 官方直连（急救）”。`);
  }
  return { cmd: exePath, args: ['start', '--port', String(BRIDGE_PORT), ...configArgs] };
}

function bridgeIsHealthy(): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${BRIDGE_PORT}/admin/api/status`, (res) => {
      let body = '';
      res.on('data', (chunk: Buffer) => { body += chunk.toString(); });
      res.on('end', () => {
        try {
          const status = JSON.parse(body);
          resolve(res.statusCode === 200 && status.running === true);
        } catch {
          resolve(false);
        }
      });
    });
    req.on('error', () => resolve(false));
    req.setTimeout(1200, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function bridgePortIsOpen(): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: '127.0.0.1', port: BRIDGE_PORT });
    let settled = false;
    const finish = (open: boolean) => {
      if (settled) return;
      settled = true;
      socket.destroy();
      resolve(open);
    };
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.setTimeout(800, () => finish(false));
  });
}

async function waitForBridgeHealthy(child: ChildProcess): Promise<boolean> {
  const deadline = Date.now() + STARTUP_HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline && bridgeProcess === child && child.exitCode === null) {
    if (await bridgeIsHealthy()) return true;
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
}

function cancelBridgeRestart() {
  if (bridgeRestartTimer) {
    clearTimeout(bridgeRestartTimer);
    bridgeRestartTimer = null;
  }
}

function cancelBridgeStableTimer() {
  if (bridgeStableTimer) {
    clearTimeout(bridgeStableTimer);
    bridgeStableTimer = null;
  }
}

function startBridgeWatchdog() {
  if (bridgeWatchdogTimer) return;
  bridgeWatchdogTimer = setInterval(async () => {
    if (bridgeWatchdogChecking || !bridgeDesiredRunning || isQuitting || bridgeStartInFlight) return;
    bridgeWatchdogChecking = true;
    try {
      if (await bridgeIsHealthy()) {
        bridgeHealthFailureCount = 0;
        return;
      }
      bridgeHealthFailureCount++;
      writeDesktopLog('WARN', `Bridge 健康检查失败 (${bridgeHealthFailureCount}/3)`);
      if (bridgeProcess) {
        if (bridgeHealthFailureCount >= 3 && bridgeProcess.exitCode === null) {
          writeDesktopLog('ERROR', 'Bridge 连续健康检查失败，终止失效子进程并自动重启');
          bridgeProcess.kill('SIGTERM');
        }
      } else if (shouldRecoverUnownedBridge({
        desiredRunning: bridgeDesiredRunning,
        isQuitting,
        hasChildProcess: false,
        startInFlight: bridgeStartInFlight,
      })) {
        writeDesktopLog('WARN', 'Bridge watchdog detected an unavailable process; recovering.');
        void startBridgeProcess();
      }
    } finally {
      bridgeWatchdogChecking = false;
    }
  }, 5000);
}

function stopBridgeWatchdog() {
  if (bridgeWatchdogTimer) {
    clearInterval(bridgeWatchdogTimer);
    bridgeWatchdogTimer = null;
  }
}

function scheduleBridgeRestart() {
  if (!shouldRestartBridge({
    desiredRunning: bridgeDesiredRunning,
    isQuitting,
    restartCount: bridgeRestartCount,
    maxRestarts: MAX_RESTART,
  })) {
    return;
  }
  const delayMs = bridgeRestartDelayMs(bridgeRestartCount, MAX_RESTART);
  bridgeRestartCount = Math.min(bridgeRestartCount + 1, MAX_RESTART);
  cancelBridgeRestart();
  writeDesktopLog('WARN', `Bridge 将在 ${delayMs}ms 后自动重启，backoff_step=${bridgeRestartCount}`);
  bridgeRestartTimer = setTimeout(() => {
    bridgeRestartTimer = null;
    void startBridgeProcess();
  }, delayMs);
}

async function startBridgeProcess() {
  if (!bridgeDesiredRunning || bridgeProcess || bridgeStartInFlight) return;
  bridgeStartInFlight = true;
  cancelBridgeRestart();
  try {
  if (await bridgeIsHealthy()) {
    bridgeOwnedByApp = false;
    bridgeRestartCount = 0;
    bridgeHealthFailureCount = 0;
    console.log(`[Main] Reusing bridge already listening on ${BRIDGE_PORT}`);
    writeDesktopLog('INFO', `复用已在 ${BRIDGE_PORT} 端口运行的 Bridge`);
    safeSend('bridge-status', { running: true });
    return;
  }
  if (await bridgePortIsOpen()) {
    const message = `端口 ${BRIDGE_PORT} 已被其他异常进程占用，未启动新的 Bridge。`;
    console.error(`[Main] ${message}`);
    safeSend('bridge-status', { running: false, error: message });
    scheduleBridgeRestart();
    return;
  }

  let command: { cmd: string; args: string[] };
  try {
    command = getBridgeCommand();
  } catch (err: any) {
    const message = err.message || String(err);
    console.error(`[Main] ${message}`);
    safeSend('bridge-status', { running: false, error: message });
    scheduleBridgeRestart();
    return;
  }
  const { cmd, args } = command;
  console.log(`[Main] Starting bridge: ${cmd} ${args.join(' ')}`);
  writeDesktopLog('INFO', `启动 Bridge 后端: ${cmd}`);

  const child = spawn(cmd, args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
    detached: process.platform === 'win32',
    windowsHide: true,
  });
  bridgeProcess = child;
  bridgeOwnedByApp = true;

  child.stdout?.on('data', (data: Buffer) => {
    const text = data.toString();
    console.log(`[Bridge] ${text.trim()}`);
    safeSend('bridge-log', { level: 'info', text: text.trim() });
  });

  child.stderr?.on('data', (data: Buffer) => {
    const text = data.toString();
    console.error(`[Bridge Error] ${text.trim()}`);
    safeSend('bridge-log', { level: 'error', text: text.trim() });
  });

  child.on('close', (code: number | null, signal: NodeJS.Signals | null) => {
    console.log(`[Main] Bridge process exited with code ${code}`);
    writeDesktopLog(
      bridgeDesiredRunning && !isQuitting ? 'ERROR' : 'INFO',
      `Bridge 子进程退出，code=${code}, signal=${signal || 'none'}, intentional=${!bridgeDesiredRunning || isQuitting}`,
    );
    if (bridgeProcess !== child) return;
    cancelBridgeStableTimer();
    bridgeProcess = null;
    bridgeOwnedByApp = false;
    safeSend('bridge-status', { running: false });
    // Any unrequested exit is a failure, including exit code 0. Manual stops
    // set bridgeDesiredRunning=false before the child exits.
    scheduleBridgeRestart();
  });

  child.on('error', (err: Error) => {
    console.error('[Main] Failed to start bridge:', err.message);
    writeDesktopLog('ERROR', 'Bridge 子进程启动或运行错误', err);
    if (bridgeProcess !== child) return;
    cancelBridgeStableTimer();
    bridgeProcess = null;
    bridgeOwnedByApp = false;
    safeSend('bridge-status', { running: false, error: err.message });
    scheduleBridgeRestart();
  });

  if (await waitForBridgeHealthy(child)) {
    bridgeHealthFailureCount = 0;
    safeSend('bridge-status', { running: true });
    writeDesktopLog('INFO', `Bridge 后端健康检查通过，pid=${child.pid || 'unknown'}`);
    cancelBridgeStableTimer();
    bridgeStableTimer = setTimeout(() => {
      bridgeStableTimer = null;
      if (bridgeProcess === child && child.exitCode === null) {
        bridgeRestartCount = 0;
        console.log('[Main] Bridge remained healthy; restart budget reset.');
      }
    }, STABLE_RUN_RESET_MS);
  } else if (bridgeProcess === child && child.exitCode === null) {
    const message = `Bridge 在 ${STARTUP_HEALTH_TIMEOUT_MS / 1000} 秒内未通过健康检查，正在重启。`;
    console.error(`[Main] ${message}`);
    writeDesktopLog('ERROR', message);
    safeSend('bridge-status', { running: false, error: message });
    child.kill('SIGTERM');
  }
  } finally {
    bridgeStartInFlight = false;
  }
}

function stopBridgeProcess(stopBorrowedBridge = false) {
  bridgeDesiredRunning = false;
  cancelBridgeRestart();
  cancelBridgeStableTimer();
  const processToStop = bridgeProcess;
  if (!processToStop && !stopBorrowedBridge) return;
  if (processToStop && !bridgeOwnedByApp && !stopBorrowedBridge) return;

  // 尝试优雅关闭
  try {
    const req = http.request({
      hostname: '127.0.0.1',
      port: BRIDGE_PORT,
      path: '/admin/api/shutdown',
      method: 'POST',
      timeout: 3000,
    });
    req.on('error', () => {});
    req.end();
  } catch {
    // ignore
  }

  setTimeout(() => {
    // Never kill a replacement child created after this stop request.
    if (processToStop && bridgeProcess === processToStop && processToStop.exitCode === null) {
      processToStop.kill('SIGTERM');
    }
  }, 1500);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 900,
    minHeight: 600,
   title: 'LAN BRIDGE',
    icon: path.join(isDev ? path.join(__dirname, '..', '..', 'desktop', 'assets') : path.join(process.resourcesPath, 'assets'), 'icon.ico'),
   backgroundColor: '#0f1117',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
    frame: true,
    titleBarStyle: 'default',
  });
  mainWindow.setMenuBarVisibility(false);

  // 窗口准备好后显示
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
  });

  // 关闭窗口 → 根据设置决定隐藏到托盘或退出
  mainWindow.on('close', (event) => {
    if (!isQuitting && getCloseToTraySetting()) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function createTray() {
  // 创建 16x16 托盘图标
  const icon = nativeImage.createFromPath(path.join(isDev ? path.join(__dirname, '..', '..', 'desktop', 'assets') : path.join(process.resourcesPath, 'assets'), 'icon.ico'));
  tray = new Tray(icon);

  // 使用自定义标题
  if (process.platform === 'darwin') {
    tray.setTitle('LB');
  }

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示主窗口',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createWindow();
        }
      },
    },
    { type: 'separator' },
    {
      label: '停止代理',
      click: () => {
        stopBridgeProcess(true);
      },
    },
    {
      label: '启动代理',
      click: () => {
        bridgeDesiredRunning = true;
        bridgeRestartCount = 0;
        void startBridgeProcess();
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true;
        stopBridgeProcess();
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);
  tray.setToolTip('LAN BRIDGE');

  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    } else {
      createWindow();
    }
  });
}

// ── IPC Handlers ────────────────────────────────────────────────────

ipcMain.handle('get-bridge-status', async () => {
  try {
    const http = require('http');
    return new Promise((resolve) => {
      const req = http.get(`http://127.0.0.1:${BRIDGE_PORT}/admin/api/status`, (res: any) => {
        let body = '';
        res.on('data', (chunk: string) => { body += chunk; });
        res.on('end', () => {
          try { resolve(JSON.parse(body)); } catch { resolve({ running: false }); }
        });
      });
      req.on('error', () => resolve({ running: false }));
      req.setTimeout(3000, () => { req.destroy(); resolve({ running: false }); });
    });
  } catch {
    return { running: false };
  }
});

ipcMain.handle('export-config', async () => {
  if (!mainWindow) return { yaml: '' };
  try {
    return await mainWindow.webContents.executeJavaScript(
      `fetch('http://127.0.0.1:${BRIDGE_PORT}/admin/api/config/export').then(r => r.json())`
    );
  } catch {
    return { yaml: '' };
  }
});

ipcMain.handle('import-config', async (_event, yamlStr: string) => {
  try {
    const http = require('http');
    const data = JSON.stringify({ yaml: yamlStr });
    return new Promise((resolve) => {
      const req = http.request({
        hostname: '127.0.0.1', port: BRIDGE_PORT,
        path: '/admin/api/config/import', method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': data.length },
      }, (res: any) => {
        let body = '';
        res.on('data', (chunk: string) => { body += chunk; });
        res.on('end', () => resolve(JSON.parse(body)));
      });
      req.on('error', () => resolve({ error: '连接失败' }));
      req.write(data);
      req.end();
    });
  } catch {
    return { error: '导入失败' };
  }
});

ipcMain.handle('select-file', async (_event, options: { filters?: Array<{ name: string; extensions: string[] }> }) => {
  const result = await dialog.showOpenDialog(mainWindow!, {
    properties: ['openFile'],
    filters: options.filters || [{ name: 'YAML', extensions: ['yaml', 'yml'] }],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('save-file', async (_event, options: { defaultPath?: string; content: string }) => {
  const result = await dialog.showSaveDialog(mainWindow!, {
    defaultPath: options.defaultPath || 'config.yaml',
    filters: [{ name: 'YAML', extensions: ['yaml', 'yml'] }],
  });
  if (!result.canceled && result.filePath) {
    fs.writeFileSync(result.filePath, options.content, 'utf-8');
    return result.filePath;
  }
  return null;
});

ipcMain.handle('open-external', async (_event, url: string) => {
  await shell.openExternal(url);
});

function restoreCodexOfficialLocally(): { status: string; message: string; backup_path?: string } {
  const codexDir = path.join(app.getPath('home'), '.codex');
  const configPath = path.join(codexDir, 'config.toml');
  const backupDir = path.join(codexDir, 'backups');
  const stamp = new Date().toISOString().replace(/\D/g, '').slice(0, 17);
  const backupPath = path.join(backupDir, `config.toml.${stamp}-${process.pid}.bak`);
  const tempPath = path.join(codexDir, `.config.toml.official-${process.pid}-${Date.now()}.tmp`);
  let backupCreated = false;

  try {
    fs.mkdirSync(backupDir, { recursive: true });
    const currentConfig = fs.existsSync(configPath) ? fs.readFileSync(configPath, 'utf-8') : '';
    const restoredConfig = restoreOfficialConfigPreservingUserSettings(currentConfig);
    fs.writeFileSync(tempPath, restoredConfig, { encoding: 'utf-8', flag: 'wx' });
    if (fs.existsSync(configPath)) {
      fs.copyFileSync(configPath, backupPath);
      backupCreated = true;
    }
    fs.renameSync(tempPath, configPath);
    const generatedCatalog = path.join(codexDir, 'lan-bridge', 'merged-models.json');
    try {
      if (fs.existsSync(generatedCatalog)) fs.unlinkSync(generatedCatalog);
    } catch (catalogError) {
      writeDesktopLog('WARN', '恢复官方直连时无法删除生成的 Bridge 模型目录', catalogError);
    }
    try {
      const backups = fs.readdirSync(backupDir)
        .filter((name) => /^config\.toml\..+\.bak$/i.test(name))
        .map((name) => ({ name, modified: fs.statSync(path.join(backupDir, name)).mtimeMs }))
        .sort((a, b) => b.modified - a.modified);
      for (const stale of backups.slice(10)) {
        fs.unlinkSync(path.join(backupDir, stale.name));
      }
    } catch (pruneError: any) {
      console.warn('[Main] Codex config backup pruning failed:', pruneError.message || String(pruneError));
    }
    return {
      status: 'ok',
      message: '已恢复 Codex 官方直连；MCP、插件、项目和其他用户设置均已保留。重新加载 Codex 后生效。',
      ...(backupCreated ? { backup_path: backupPath } : {}),
    };
  } catch (err: any) {
    try {
      if (fs.existsSync(tempPath)) fs.unlinkSync(tempPath);
    } catch { /* preserve the original error; the original config was never moved */ }
    return { status: 'error', message: `恢复官方直连失败：${err.message || String(err)}` };
  }
}

ipcMain.handle('restore-codex-official', async () => restoreCodexOfficialLocally());

// ── App Lifecycle ────────────────────────────────────────────────────

function setLaunchAtLogin(enabled: boolean): { status: string; enabled?: boolean; message?: string } {
  try {
    app.setLoginItemSettings({
      openAtLogin: enabled,
      path: process.execPath,
    });
    return { status: 'ok', enabled: app.getLoginItemSettings().openAtLogin };
  } catch (err: any) {
    return { status: 'error', message: err.message || String(err) };
  }
}

ipcMain.handle('set-launch-at-login', async (_event, enabled: boolean) => {
  return setLaunchAtLogin(Boolean(enabled));
});

function getCloseToTraySetting(): boolean {
  const configPath = findConfigPath();
  if (!configPath) return true;
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    return readServerBooleanSetting(content, 'close_to_tray', true);
  } catch { /* ignore */ }
  return true;
}

function getAutoStartSetting(): boolean {
  const configPath = findConfigPath();
  if (!configPath) return true;
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    return readServerBooleanSetting(content, 'auto_start', true);
  } catch { /* ignore */ }
  return true;
}

function getLaunchAtLoginSetting(): boolean {
  const configPath = findConfigPath();
  if (!configPath) return false;
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    return readServerBooleanSetting(content, 'launch_at_login', false);
  } catch { /* ignore */ }
  return false;
}

app.whenReady().then(() => {
  writeDesktopLog('INFO', `LAN BRIDGE Desktop 启动，version=${app.getVersion()}, pid=${process.pid}`);
  Menu.setApplicationMenu(null);
  setLaunchAtLogin(getLaunchAtLoginSetting());
  createTray();
  createWindow();
  startBridgeWatchdog();
  if (getAutoStartSetting()) {
    bridgeDesiredRunning = true;
    void startBridgeProcess();
  } else {
    bridgeDesiredRunning = false;
    console.log('[Main] auto_start disabled, bridge not started automatically');
  }
}).catch((error) => {
  writeDesktopLog('FATAL', 'Electron 初始化失败；主进程保持存活以便诊断', error);
});

app.on('render-process-gone', (_event, webContents, details) => {
  writeDesktopLog(
    details.reason === 'clean-exit' ? 'INFO' : 'ERROR',
    `渲染进程退出，reason=${details.reason}, exit_code=${details.exitCode}, web_contents_id=${webContents.id}`,
  );
});

app.on('child-process-gone', (_event, details) => {
  writeDesktopLog(
    details.reason === 'clean-exit' ? 'INFO' : 'ERROR',
    `Electron 子进程退出，type=${details.type}, reason=${details.reason}, exit_code=${details.exitCode}`,
  );
});

app.on('window-all-closed', () => {
  // 不退出，保持托盘运行
});

app.on('activate', () => {
  if (mainWindow) {
    mainWindow.show();
  } else {
    createWindow();
  }
});

app.on('before-quit', () => {
  writeDesktopLog('INFO', '收到桌面应用退出请求，正在停止 Bridge');
  isQuitting = true;
  bridgeDesiredRunning = false;
  stopBridgeWatchdog();
  stopBridgeProcess();
});

app.on('quit', () => {
  stopBridgeProcess();
});
