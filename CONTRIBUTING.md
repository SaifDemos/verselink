# Contributing to VerseLink

Thanks for helping make VerseLink better. Small fixes, documentation changes,
accessibility improvements, and media-player compatibility reports are welcome.
Be kind, explain your reasoning, and keep feedback focused on the work.

## Before you start

- Search [existing issues](https://github.com/SaifDemos/verselink/issues) before
  opening a bug report or feature request.
- For large changes or new dependencies, open an issue to discuss the approach first.
- Report vulnerabilities privately using [SECURITY.md](SECURITY.md), not a public issue.
- Do not include private paths, tokens, listening history, or commercial lyrics/artwork
  in reports, fixtures, or screenshots. Use original demo content instead.

## Development setup

Fork the repository, clone your fork, and create a branch:

```sh
git clone https://github.com/YOUR-USERNAME/verselink.git
cd verselink
git switch -c your-change
```

Use Python **3.8+**. On Linux, install `playerctl`; on Windows, use the built-in
PowerShell 5.1/GSMTC backend. There are **no Python packages, package manifests,
frontend dependencies, or build steps** to install. Node.js is only needed to
syntax-check JavaScript during development, not to run VerseLink.

Run a separate foreground instance so you do not disturb an existing service:

```sh
python3 verselink.py --host 127.0.0.1 --port 9000
```

On Windows, use `python` or `py -3` instead of `python3`. Open
http://127.0.0.1:9000 and stop the development server with `Ctrl+C`.

## Where things live

- `verselink.py` contains the Python backend and the entire frontend in the raw
  `PAGE` string. Edit HTML, CSS, and JavaScript in place; there is no bundler.
- `win-gsmtc.ps1` is the Windows media bridge. Its stdout must remain JSON-only.
- `start.*` / `stop.*` manage the background service and print connection URLs.
- [AGENTS.md](AGENTS.md) documents the HTTP contract, caching, settings migration,
  platform behavior, and important implementation constraints.

Never put a triple double-quote sequence inside `PAGE`: it terminates the Python
string. Keep background motion CSS-only, and preserve the custom header required
for every POST route.

## Checks

Check Python syntax:

```sh
python3 -m py_compile verselink.py
```

On Linux, also check each launcher:

```sh
bash -n start.sh
bash -n stop.sh
```

To check the embedded frontend without starting the server, run this in Bash
with Node.js available:

```sh
python3 - <<'PY'
import ast
from pathlib import Path
import subprocess
import tempfile

tree = ast.parse(Path("verselink.py").read_text(encoding="utf-8"))
page = next(ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PAGE"
                    for target in node.targets))
script = page.split("<script>", 1)[1].split("</script>", 1)[0]
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "page.js"
    path.write_text(script, encoding="utf-8")
    subprocess.run(["node", "--check", str(path)], check=True)
PY
```

GitHub Actions runs syntax checks with Python 3.8 and 3.14 on Linux and Windows.
It also parses the PowerShell scripts with Windows PowerShell. There is no
automated behavioral test suite; a green workflow does not prove that a player
backend or the browser UI works.

## Manual verification

For changes affecting playback or the frontend, check what is relevant:

- The page starts without browser-console exceptions and loads artwork and lyrics.
- Play/pause, previous/next, seeking, and the seek bar work with a real player.
- Audio source lists open apps, switches correctly, and falls back when an app closes.
- Local `.lrc` files work; online lyrics are cached and can replay offline.
- Liquid visibly moves, works without artwork, pauses for Off/reduced motion,
  and works in full-cover and phone-sized layouts.
- Settings persist, and old preferences migrate without losing unrelated choices.
- Launchers start/stop the correct service and print useful local/LAN URLs.
- POSTs without `X-Requested-With: verselink` remain forbidden.

You can exercise the Linux HTTP backend without a real player by placing a fake
`playerctl` first on `PATH` that answers `-l` and `metadata --format`, then checking
`/state`, `/lyrics`, and `/players`. Keep synthetic fixtures outside runtime code.

Changes to GSMTC or Windows launchers need a **real Windows 10 1809+/Windows 11
check with PowerShell 5.1**. If you cannot perform it, say so clearly in the PR;
Linux checks and CI parsing do not validate Windows media sessions.

## Pull requests

Keep changes focused and consistent with the surrounding code. Describe the
problem, your approach, and the checks you actually ran. Include screenshots for
visual changes using content you have permission to publish.

Do not commit `.run/`, Python bytecode, local caches, credentials, or generated
development files. No particular commit-message format is required.

By contributing, you agree that your contribution may be distributed under
VerseLink's [MIT License](LICENSE).
