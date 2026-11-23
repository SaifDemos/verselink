# AGENTS.md

VerseLink is a browser-based, line-synced lyrics display and remote control for
desktop media players. `verselink.py` holds the Python HTTP server, the media
backends, and the entire frontend inside the `PAGE` raw string.
`win-gsmtc.ps1` is the Windows media backend. There is no build step, package
manifest, behavioral test suite, or linter — do not invent one without a request.
Keep the runtime dependency-free; GitHub Actions only performs syntax checks.

## Open-source project

- Public repository: https://github.com/SaifDemos/verselink; default branch `main`.
- Project code and original assets use the MIT license in `LICENSE`.
- `README.md`, `CONTRIBUTING.md`, and `SECURITY.md` cover user setup, development,
  and private vulnerability reporting. Keep them accurate when behavior changes.
- Never commit `.run/`, Python bytecode, credentials, local caches, or private
  media metadata. Published screenshots must use original/rights-cleared content.
- `.github/workflows/checks.yml` checks Python and embedded JS syntax on Linux
  and Windows, plus shell/PowerShell parsing. It does not exercise real media apps.

## Run and verify

```sh
./start.sh / ./stop.sh                 # Linux background service; pid/log in .run/
.\start.ps1 / .\stop.ps1               # Windows background service
python3 verselink.py [--player ID] [--host 127.0.0.1] [--port 8765]
python3 -m py_compile verselink.py
bash -n start.sh && bash -n stop.sh
```

Linux requires `playerctl`; Windows requires PowerShell 5.1 (built in) and uses
GSMTC. No Python packages on either platform, and the app must stay 3.8+
compatible. The start scripts print local and LAN URLs.

There is no test suite. To exercise the backend without a real player, put a fake
`playerctl` first on `PATH` that answers `-l` and `metadata --format`, then curl
`/state`, `/lyrics`, `/players`. To syntax-check embedded JS, fetch `/`, extract
the `<script>` body, and run `node --check`. Also check the page in a browser:
syntax checks alone do not catch undefined functions breaking startup.

## Media backends

- `IS_WINDOWS` chooses the backend; never invoke `playerctl` on Windows.
- Linux: `playerctl -l` lists open apps; metadata comes from the SEP-separated
  `--format` template; seeking uses `position`.
- Windows: `win_bridge()` runs `win-gsmtc.ps1` once per query and parses the last
  JSON line from stdout. Keep the helper's stdout JSON-only. State is cached for
  1.2 s and position is extrapolated between queries; commands invalidate it.
- `GET /players` returns apps currently open; `POST /player?p=<id>` switches
  (empty id = automatic). `get_meta()` and `active_player()` clear a selection
  that disappeared so playback falls back to automatic.
  `PLAYER` and the caches are guarded by `STATE_LOCK` / `_WIN_LOCK`; handlers run
  concurrently under `ThreadingHTTPServer`.

## Liquid background

- `Liquid` is the default. `#liquid` contains three `.liquid-layer` elements
  that move independently using CSS transforms and soft masks. They sit above
  the static `#bg` and below `#shade` and the lyrics; never nest them inside
  `#bg`, which would blur and darken the artwork twice.
- `prepareLiquidArt()` prepares a 96px blurred texture once per cover and sets
  `--liquid-art`. `setArt()` also supplies the original `--art`; missing/failed
  artwork clears both and falls back to palette gradients.
- Animation durations: normal 16/21/28 s, subtle 30/38/46 s, cinematic
  10/14/18 s. `syncBackground()` updates `data-bg-motion` to pause for Animation
  Off, reduced motion, or a hidden tab. Liquid works in every layout, including
  full-cover. Other background modes are static.
- No audio capture, permissions, extra routes, or platform-specific code are
  involved. Do not reintroduce the removed audio spectrum renderer.

## HTTP contract

- `GET /` page, `/state`, `/lyrics`, `/art`, `/players`, `/ping`.
- `POST /seek?t=<seconds>`, `/cmd?c=play-pause|next|previous`, `/player?p=<id>`.
- All POSTs require header `X-Requested-With: verselink` or return 403. This is
  deliberate: it forces a CORS preflight so other websites cannot drive the
  player. Keep it when adding POST routes.
- `Handler.protocol_version = "HTTP/1.1"` with keep-alive, so responses must keep
  framing correct — route responses through `Handler.reply()` (sets
  `Content-Length`); don't write to `wfile` directly.
- Access logging is intentionally disabled (`log_message` is a no-op).

## Editing the embedded frontend

- `PAGE` is an `r"""..."""` string served verbatim at `/`; no templating, bundler,
  or separate asset. Edit HTML/CSS/JS in place.
- Never introduce `"""` into `PAGE`; it terminates the Python string. Backslashes
  are literal (raw string), so JS escapes like `\u2014` stay exactly as written.
- Body `data-*` attributes drive CSS (`body[data-layout="cover"]` etc.);
  `applySettings()` owns them.
- Layout prefs persist per device in `localStorage` under
  `verselink.settings.v2`, falling back to `verselink.settings.v1`, then
  `synclyrics.settings.v2`. Migration replaces the old blur default and removed
  `viz` mode with Liquid, preserving other preferences. If you add/rename a
  setting, bump the key (and `DEFAULTS`).
- The source picker refreshes `/players` only while settings are open (3 s) and
  is not saved per browser; the selected player is server-wide.
- Playback position is extrapolated client-side (`nowPos()`); the client polls
  `/state` every 400 ms and refetches `/lyrics` only when the track `id` changes.
  Don't add server push for ordinary state updates.
- `applySettings()` calls `syncBackground()`; resize/fullscreen handlers only
  update lyrics layout. The main animation frame handles lyric sync and the
  seek bar, not the background. Keep motion CSS-only.

## Performance quirks

- Linux `get_meta()` shells out to `playerctl` on every `/state`, `/lyrics`, and
  `/art` request (2 s timeout). `/state` is hit ~2.5×/s per client, so keep
  handlers cheap and avoid extra `playerctl` calls.
- PowerShell startup is expensive; that is why the Windows state is cached and
  `/players` is only polled while the settings sheet is open.
- Liquid animation does not poll the backend or redraw a canvas every frame.
  Only the small artwork texture is generated when the cover changes.

## Lyrics and caching

- Line-level sync only. `parse_lrc()` honors `[offset:...]` and strips
  enhanced-LRC word tags (`<mm:ss.xx>`).
- Local lookup is only for `file://` track URLs: `<audio>.lrc`/`.LRC` beside the
  file. Otherwise LRCLIB is tried: exact `/api/get`, then `/api/search` by
  title/artist (closest duration wins).
- Disk cache: `~/.cache/verselink/<id>.lrc` and `~/.cache/verselink/art/<md5>`.
  If `~/.cache/verselink` does not exist but `~/.cache/synclyrics` does, the old
  directory is reused so previously fetched lyrics still work offline.
  `id` is the first 12 hex chars of `md5(url|artist|title)`; art is pruned above
  120 files. Online lyrics are cached, so offline replay works only for songs
  looked up before.
- Windows has no track URL; it uses `win:<app-id>` as the URL and
  `win:<app-id>:<track-hash>` as the art id. `_win_art()` fetches thumbnails via
  the PowerShell helper and caches them like remote art.

## Platform caveat

The Windows/GSMTC path cannot be exercised on Linux. Changes to
`win-gsmtc.ps1` or the Windows branch need a real Windows check (PowerShell 5.1,
Windows 10 1809+).

## Security

Default binding is `0.0.0.0`, which exposes full player control to the LAN. Use
`HOST=127.0.0.1` / `-Bind 127.0.0.1` / `--host 127.0.0.1` for local-only use.
