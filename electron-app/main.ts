import { app, BrowserWindow, globalShortcut, Menu, nativeImage, session, Tray } from 'electron';
import type { MenuItemConstructorOptions } from 'electron';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { registerHandlers } from './ipc-handlers';
import { WAVE, waveY } from './shared/wave-mark';

let pythonServer: ReturnType<typeof spawn> | null = null;
let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
const SERVER_PORT = 8756;
const SERVER_URL = `http://127.0.0.1:${SERVER_PORT}`;
let isQuitting = false;
const startupTimers = new Set<ReturnType<typeof setTimeout>>();

function scheduleStartup(callback: () => void, delayMs: number) {
  const timer = setTimeout(() => {
    startupTimers.delete(timer);
    if (!isQuitting) callback();
  }, delayMs);
  startupTimers.add(timer);
  return timer;
}

function isLoopbackRequest(requestUrl: string) {
  try {
    const parsed = new URL(requestUrl);
    if (!['http:', 'https:', 'ws:', 'wss:'].includes(parsed.protocol)) return true;
    return ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(parsed.hostname);
  } catch {
    return false;
  }
}

function enforceRendererNetworkPolicy() {
  session.defaultSession.webRequest.onBeforeRequest((details, callback) => {
    const allowed = isLoopbackRequest(details.url);
    if (!allowed) {
      let destination = 'invalid URL';
      try { destination = new URL(details.url).origin; } catch (_) {}
      console.warn(`[network] Blocked non-loopback renderer request to ${destination}`);
    }
    callback({ cancel: !allowed });
  });
}

function startPythonServer(): void {
  let pythonPath: string;
  let args: string[] = [];
  
  // Pass through important environment variables to Python server
  const pythonEnv: Record<string, string | undefined> = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    // Preserve API key environment variables
    OPENAI_API_KEY: process.env.OPENAI_API_KEY,
    OPENAI_API_BASE: process.env.OPENAI_API_BASE,
    ANTHROPIC_API_KEY: process.env.ANTHROPIC_API_KEY,
    ANTHROPIC_API_BASE: process.env.ANTHROPIC_API_BASE,
    ANTHROPIC_BASE_URL: process.env.ANTHROPIC_BASE_URL,
    GOOGLE_API_KEY: process.env.GOOGLE_API_KEY,
    VERTEX_EXPRESS_API_KEY: process.env.VERTEX_EXPRESS_API_KEY,
    GEMINI_VERTEXAI_EXPRESS: process.env.GEMINI_VERTEXAI_EXPRESS,
    TEMPO_LM_API_KEY: process.env.TEMPO_LM_API_KEY,
    TEMPO_LM_API_BASE: process.env.TEMPO_LM_API_BASE,
    MODEL_NAME: process.env.MODEL_NAME,
  };
  
  if (app.isPackaged) {
    // In production, use bundled Python executable
    const platform = process.platform;
    const ext = platform === 'win32' ? '.exe' : '';
    pythonPath = path.join(process.resourcesPath, 'python-server', `tempo-server${ext}`);
    
    if (!fs.existsSync(pythonPath)) {
      console.error(`Python server not found at ${pythonPath}`);
      return;
    }
  } else {
    // In development, use Python directly
    // Try to use the same Python that has the packages installed
    let pythonCmd = process.env.PYTHON;
    
    // If no explicit PYTHON env var, try to detect virtual environment
    if (!pythonCmd) {
      // Check for common virtual environment indicators
      const venvPath = process.env.VIRTUAL_ENV;
      if (venvPath) {
        // Use virtual environment's Python
        const isWindows = process.platform === 'win32';
        pythonCmd = path.join(venvPath, isWindows ? 'Scripts' : 'bin', isWindows ? 'python.exe' : 'python');
        console.log(`Detected virtual environment: ${venvPath}`);

        // Add venv's bin directory to PATH for executables like cbc (ILP solver)
        const venvBinDir = path.dirname(pythonCmd);
        if (pythonEnv.PATH) {
          pythonEnv.PATH = `${venvBinDir}:${pythonEnv.PATH}`;
        } else {
          pythonEnv.PATH = venvBinDir;
        }
        console.log(`Added to PATH: ${venvBinDir}`);
      } else {
        // Try to find python in common locations (uv .venv first, then conda fallbacks)
        const homeDir = require('os').homedir();
        const projectRoot = path.resolve(__dirname, '..', '..');
        const envName = 'tempo';
        const possiblePaths = [
          // uv / venv in project root (preferred)
          path.join(projectRoot, '.venv', 'bin', 'python'),
          // Legacy conda paths (fallback)
          path.join(homeDir, 'miniconda3', 'envs', envName, 'bin', 'python'),
          path.join(homeDir, '.conda', 'envs', envName, 'bin', 'python'),
          path.join(homeDir, 'anaconda3', 'envs', envName, 'bin', 'python'),
          path.join(homeDir, '.virtualenvs', envName, 'bin', 'python'),
        ];

        for (const possiblePath of possiblePaths) {
          if (fs.existsSync(possiblePath)) {
            pythonCmd = possiblePath;
            console.log(`Found Python at: ${pythonCmd}`);

            // Add env's bin directory to PATH for executables like cbc (ILP solver)
            const envBinDir = path.dirname(possiblePath);
            if (pythonEnv.PATH) {
              pythonEnv.PATH = `${envBinDir}:${pythonEnv.PATH}`;
            } else {
              pythonEnv.PATH = envBinDir;
            }
            console.log(`Added to PATH: ${envBinDir}`);
            break;
          }
        }
        
        // Fallback to python3
        if (!pythonCmd) {
          pythonCmd = 'python3';
          console.log('Using system python3 (may not have FastAPI installed)');
          console.log('Tip: Set PYTHON env var or activate your virtual environment');
        }
      }
    }
    
    pythonPath = pythonCmd;
    // Use the server.py file directly (not as module) to ensure correct path resolution
    args = [path.join(__dirname, '..', '..', 'tempo', 'server.py')];

    // Add project root to PYTHONPATH so imports work correctly
    const projectRoot = path.join(__dirname, '..', '..');
    if (!pythonEnv.PYTHONPATH) {
      pythonEnv.PYTHONPATH = projectRoot;
    } else {
      pythonEnv.PYTHONPATH = `${projectRoot}:${pythonEnv.PYTHONPATH}`;
    }
  }
  
  console.log(`Starting Python server: ${pythonPath} ${args.join(' ')}`);
  if (pythonEnv.PYTHONPATH) {
    console.log(`PYTHONPATH: ${pythonEnv.PYTHONPATH}`);
  }
  
  const server = spawn(pythonPath, args, {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: pythonEnv,
  });
  
  pythonServer = server;

  server.stdout.on('data', (data) => {
    console.log(`[Python Server] ${data.toString().trim()}`);
  });
  
  server.stderr.on('data', (data) => {
    console.error(`[Python Server Error] ${data.toString().trim()}`);
  });
  
  server.on('error', (error) => {
    console.error(`Failed to start Python server: ${error.message}`);
    if (mainWindow) {
      mainWindow.webContents.send('server-error', error.message);
    }
  });
  
  server.on('exit', (code, signal) => {
    console.log(`Python server exited with code ${code} and signal ${signal}`);
    if (code !== 0 && code !== null) {
      if (mainWindow) {
        mainWindow.webContents.send('server-error', `Server exited with code ${code}`);
      }
    }
  });
  
  // Wait a bit for server to start
  scheduleStartup(() => {
    checkServerHealth();
  }, 2000);
}

function checkServerHealth() {
  const req = http.get(`${SERVER_URL}/api/status`, (res) => {
    if (res.statusCode === 200) {
      console.log('Python server is healthy');
      if (mainWindow) {
        mainWindow.webContents.send('server-ready');
      }
    } else {
      console.warn(`Server health check returned ${res.statusCode}`);
    }
  });
  
  req.on('error', (error) => {
    console.warn(`Server health check failed: ${error.message}`);
    // Retry after a delay
    scheduleStartup(checkServerHealth, 3000);
  });
  
  req.setTimeout(2000, () => {
    req.destroy();
  });
}

function createWindow() {
  // Don't create a new window if one already exists
  if (mainWindow) {
    mainWindow.show();
    mainWindow.focus();
    return;
  }

  const window = new BrowserWindow({
    width: 1400,
    height: 900,
    backgroundColor: '#f7f4f2',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
    },
    icon: path.join(__dirname, '..', app.isPackaged ? 'dist' : 'public', 'icon.png'),
    show: false, // Don't show until ready
  });
  
  mainWindow = window;

  // Load the app
  if (app.isPackaged) {
    window.loadURL(SERVER_URL);
  } else {
    // In development, load from Vite dev server or file
    window.loadURL('http://localhost:5173'); // Vite default port
    // Open DevTools in dev with Cmd+Shift+I (or uncomment below to auto-open)
    // mainWindow.webContents.openDevTools({ mode: 'detach' });
  }
  
  // Show window when ready
  window.once('ready-to-show', () => {
    window.show();
    window.focus();
  });
  
  // Handle window close - hide instead of destroy
  window.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      window.hide();

      // Keep dock icon visible so users can reopen the app by clicking it
      if (process.platform === 'darwin') {
        app.dock.show();
      }
    }
  });
  
  // Show dock when window is shown (macOS)
  window.on('show', () => {
    if (process.platform === 'darwin') {
      app.dock.show();
    }
  });
  
  window.on('closed', () => {
    mainWindow = null;
  });
}

function createTray() {
  // The waveform mark, rasterised for the menu bar. Geometry is shared with the
  // SVG the app chrome draws (see shared/wave-mark.ts) so the tray and the
  // in-app logo stay the same mark.
  const size = WAVE.size;
  const imageData = Buffer.alloc(size * size * 4);
  imageData.fill(0); // Transparent background

  for (let x = WAVE.startX; x < WAVE.endX; x++) {
    const y = waveY(x);

    // Slight thickness so the stroke survives at menu-bar scale.
    const yTop = Math.floor(y - 0.5);
    const yBottom = Math.ceil(y + 0.5);

    for (let yPos = yTop; yPos <= yBottom; yPos++) {
      if (yPos >= 0 && yPos < size) {
        const idx = (yPos * size + x) * 4;
        imageData[idx] = 255;     // R
        imageData[idx + 1] = 255; // G
        imageData[idx + 2] = 255; // B
        imageData[idx + 3] = 255; // A
      }
    }
  }

  let trayIcon = nativeImage.createFromBuffer(imageData, { width: size, height: size });
  
  // On macOS, set as template image so it adapts to light/dark menu bar
  if (process.platform === 'darwin') {
    trayIcon.setTemplateImage(true);
  }
  
  const nextTray = new Tray(trayIcon);
  tray = nextTray;
  
  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show Tempo',
      click: () => {
        createWindow();
        if (process.platform === 'darwin') {
          app.dock.show();
        }
      }
    },
    {
      label: 'Launch at login',
      type: 'checkbox',
      checked: app.getLoginItemSettings().openAtLogin,
      click: (menuItem) => {
        app.setLoginItemSettings({ openAtLogin: menuItem.checked });
      },
    },
    { type: 'separator' },
    {
      label: 'Quit Tempo',
      click: () => {
        isQuitting = true;
        if (pythonServer) {
          pythonServer.kill();
          pythonServer = null;
        }
        app.quit();
      }
    }
  ]);
  
  nextTray.setToolTip('Tempo - Behavioral Pattern Observer');
  nextTray.setContextMenu(contextMenu);
  
  // Both left and right clicks show the context menu
  nextTray.on('click', () => {
    nextTray.popUpContextMenu();
  });
  
  // Also handle right click explicitly
  nextTray.on('right-click', () => {
    nextTray.popUpContextMenu();
  });
}

app.whenReady().then(() => {
  enforceRendererNetworkPolicy();
  startPythonServer();
  createTray();
  createWindow();

  // Register IPC handlers (permissions, app/URL exclusion, setup, unlock)
  // Must be called after createWindow() so mainWindow ref is available
  if (mainWindow) registerHandlers(mainWindow);

  // Create application menu (macOS menu bar)
  if (process.platform === 'darwin') {
    const template: MenuItemConstructorOptions[] = [
      {
        label: app.getName(),
        submenu: [
          { role: 'about', label: `About ${app.getName()}` },
          { type: 'separator' },
          { role: 'services', submenu: [] },
          { type: 'separator' },
          { role: 'hide', label: `Hide ${app.getName()}` },
          { role: 'hideOthers' },
          { role: 'unhide' },
          { type: 'separator' },
          {
            label: 'Quit Tempo',
            accelerator: 'Command+Q',
            click: () => {
              isQuitting = true;
              if (pythonServer) {
                pythonServer.kill();
                pythonServer = null;
              }
              app.quit();
            }
          }
        ]
      },
      {
        label: 'Edit',
        submenu: [
          { role: 'undo', label: 'Undo' },
          { role: 'redo', label: 'Redo' },
          { type: 'separator' },
          { role: 'cut', label: 'Cut' },
          { role: 'copy', label: 'Copy' },
          { role: 'paste', label: 'Paste' },
          { role: 'selectAll', label: 'Select All' }
        ]
      },
      {
        label: 'Window',
        submenu: [
          { role: 'minimize', label: 'Minimize' },
          { role: 'close', label: 'Close' },
          { type: 'separator' },
          { role: 'front', label: 'Bring All to Front' }
        ]
      }
    ];
    const menu = Menu.buildFromTemplate(template);
    Menu.setApplicationMenu(menu);
  } else {
    // For Windows/Linux, also add Edit menu
    const template: MenuItemConstructorOptions[] = [
      {
        label: 'File',
        submenu: [
          {
            label: 'Quit',
            accelerator: process.platform === 'win32' ? 'Ctrl+Q' : 'Ctrl+Q',
            click: () => {
              isQuitting = true;
              if (pythonServer) {
                pythonServer.kill();
                pythonServer = null;
              }
              app.quit();
            }
          }
        ]
      },
      {
        label: 'Edit',
        submenu: [
          { role: 'undo', label: 'Undo' },
          { role: 'redo', label: 'Redo' },
          { type: 'separator' },
          { role: 'cut', label: 'Cut' },
          { role: 'copy', label: 'Copy' },
          { role: 'paste', label: 'Paste' },
          { role: 'selectAll', label: 'Select All' }
        ]
      }
    ];
    const menu = Menu.buildFromTemplate(template);
    Menu.setApplicationMenu(menu);
  }
  
  // macOS: Reopen window when dock icon is clicked
  app.on('activate', () => {
    if (mainWindow) {
      mainWindow.show();
      app.dock.show();
    } else {
      createWindow();
    }
  });
});

// Don't quit when all windows are closed - keep running in background
app.on('window-all-closed', () => {
  // Don't quit - keep the app running in the background
  // The Python server will continue running
  // User can reopen window from tray icon or dock (macOS)
});

// Only quit when explicitly requested
app.on('before-quit', () => {
  isQuitting = true;
  startupTimers.forEach(clearTimeout);
  startupTimers.clear();
  globalShortcut.unregisterAll();
  if (pythonServer) {
    console.log('Stopping Python server...');
    pythonServer.kill();
    pythonServer = null;
  }
});

// Export server URL constant (used by preload.js)
// Note: In production, this would be passed via IPC or contextBridge
