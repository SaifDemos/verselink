# Security policy

## Intended use

VerseLink is a local desktop companion and remote control for **trusted local
networks**. It is not designed to be an internet-facing service.

- The default bind address is `0.0.0.0`: anyone who can reach the port can read
  track metadata and control playback, seeking, and the selected media source.
- There is **no authentication or built-in HTTPS**. Do not port-forward the
  service or expose it on an untrusted network.
- POST requests require `X-Requested-With: verselink`. This blocks ordinary
  cross-origin browser control by forcing a CORS preflight, but it is **not an
  access-control mechanism** for clients that can reach the server.
- Use `HOST=127.0.0.1 ./start.sh`, `.\start.ps1 -Bind 127.0.0.1`, or
  `python3 verselink.py --host 127.0.0.1` for local-only use.
- If allowing LAN access through a firewall, scope the rule to your private,
  trusted network. Avoid public Wi-Fi and public-network firewall rules.

Online lyric lookup sends song metadata to LRCLIB. Remote artwork is retrieved
from the URL supplied by the media player. Local lyrics/artwork caches are stored
under the server user's home directory. Avoid sharing those caches or runtime
logs without reviewing their contents.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting:

**[Report a vulnerability](https://github.com/SaifDemos/verselink/security/advisories/new)**

Include the affected commit/version, platform, reproduction steps, impact, and
any suggested mitigation. Use synthetic data and redact tokens, private paths,
addresses, and personal media metadata.

Do not open a public issue or publish an exploit containing sensitive details
before the maintainer has had a chance to assess the report. If private reporting
is unavailable, contact the maintainer privately through their
[GitHub profile](https://github.com/SaifDemos) before disclosing details publicly.

## Supported versions

Security fixes target the current `main` branch. Older snapshots are not
independently maintained; update to the latest code before reporting a problem.
