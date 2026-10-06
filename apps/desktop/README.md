# Luma Desktop

Electron provides the macOS and Windows window, tray, microphone permissions and a small IPC bridge. The interface is shared with `apps/web` and uses the account API on your own Luma server. Your data is stored on the server you deploy.

## Development

Install the backend dependencies as described in the root README, then start the API from the repository root:

```bash
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

In a second terminal, start the Web development server:

```bash
cd apps/web
npm ci
VITE_API_URL=http://localhost:8000/api/v1 npm run dev
```

In a third terminal, start Electron:

```bash
cd apps/desktop
npm ci
ELECTRON_RENDERER_URL=http://localhost:5173 npm run dev
```

`ELECTRON_RENDERER_URL` selects the development renderer, whose API address comes from Web's `VITE_API_URL`. Electron opens `/app` and adds `client=1`. The deployed interface address (also used to scope microphone permissions) is selected in this order:

1. `LUMA_SERVER_URL` environment variable.
2. `config.json` in the application's resources directory.
3. `config.local.json` next to `main.js`.
4. `http://localhost:8000` if no address is configured.

Missing, malformed or empty configuration files fall through to the next source. Server configuration only contains `serverUrl`:

```bash
cp config.example.json config.local.json
```

```json
{
  "serverUrl": "http://localhost:8000"
}
```

For a deployed server, set your own HTTP(S) address in that file or override it for a launch:

```bash
LUMA_SERVER_URL=https://luma.example.com npm start
```

The deployed Web interface and API use the same origin and HttpOnly session cookies. Login and registration follow the server's account policy. The packaged application loads this server interface; it does not bundle an offline backend.

## Packaging

Install dependencies with `npm ci`, copy `config.example.json` to `config.local.json`, and set the target server address. Keep this file free of passwords and tokens. The builder copies it to `config.json` in the package resources, outside ASAR.

```bash
npm run dist:mac
# On a Windows machine or Windows CI runner:
npm run dist:win
```

Artifacts are written to `dist/`. macOS builds produce separate arm64 and x64 DMG/ZIP files; Windows builds produce an x64 NSIS installer with an installation directory chooser and desktop shortcut. The application ID is `com.example.luma`. These commands never publish artifacts automatically.

### Signing certificates

Provide certificates through environment variables or your CI secret store. No signing identity is fixed in `package.json`. Export the certificate and its private key as a password-protected PKCS#12 file, then set:

```bash
export CSC_LINK=/path/to/developer-certificate.p12
# Set CSC_KEY_PASSWORD through your shell or CI secret store.
npm run dist:mac
```

`CSC_KEY_PASSWORD` contains the certificate password. Windows can use `CSC_LINK` / `CSC_KEY_PASSWORD` too, or `WIN_CSC_LINK` / `WIN_CSC_KEY_PASSWORD` for separate credentials. See the [electron-builder v26 signing documentation](https://www.electron.build/v26/docs/features/code-signing/).

macOS packaging requires a valid signing certificate (`forceCodeSigning: true`). Hardened runtime and the microphone/JIT entitlements are enabled for the app and helpers. Notarization is disabled by default; configure it with your own Apple credentials and verify it before distributing a notarized release. See [macOS signing](https://www.electron.build/v26/docs/features/code-signing/code-signing-mac/).

## Verification and native integration

```bash
npm run check
npm test
```

The tests run without a GUI and cover server selection, packaging metadata, icons, tray behavior, microphone permissions and IPC.

The window uses `contextIsolation`, disables `nodeIntegration` and enables the renderer sandbox. External links accept only HTTP(S). Microphone permissions are limited to audio on the configured server origin. The preload exposes application information, external links and three commands: new chat, search and voice input.

Closing the window hides it to the tray; choosing Quit exits. `CommandOrControl+Shift+L` toggles the window. macOS tray clicks open the menu, and Windows tray clicks restore the window. The startup checkbox delegates to the operating system.

## Regenerating icons

On macOS with Xcode command-line tools installed:

```bash
python3 scripts/generate_brand_icons.py --source /path/to/luma-logo
node --test tests/brand-icons.test.js
```

The generator uses Python's standard library and macOS image tools. It writes application/tray icons and their SHA256 manifest to `build/`. An intermediate iconset is optional: tests can inspect the generated ICNS directly.
