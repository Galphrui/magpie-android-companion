Magpie computer-side companion bridge
=====================================

Full management over USB
------------------------

`tools/usb_start.sh --full-browser` also serves the installed Mac Magpie's
original browser interface on loopback port 43427. The USB supervisor verifies
the installed companion's signing certificate and maps both loopback ports.
The APK authenticates with the pinned certificate, receives an HttpOnly
session cookie, and opens the responsive interface. Magpie handles the same
API calls it handles in its own browser window, including settings, providers,
model assignments, library skills, plugins, and usage. The upstream browser
key stays on the Mac. The summary service and its `--allow-write` option are
independent of the browser's management operations.

The original browser still has browser-versus-desktop restrictions. This
transport does not handle external OAuth popups, gateway client endpoints,
or third-party plugin private service routes. Full management is USB-only.

Summary service
---------------

This small Python standard-library service is separate from Magpie and only
binds 127.0.0.1:43425 by default. It starts an HTTPS listener with a newly generated,
seven-day self-signed leaf certificate and a new random 256-bit URL-safe bearer
token on every run. stdout's first line is the pairing value:

    <leaf-certificate-SHA-256-as-64-lowercase-hex>:<bearer-token>

Pass that value to the Android companion over a trusted channel. Android must
pin the exact leaf certificate fingerprint before it sends the bearer token.
Use TLS and the complete pairing value; do not weaken certificate checking or
put the pairing value in a URL. Stop and restart the bridge to revoke that run's
token and certificate. The fingerprint is not a CA certificate/fingerprint.

Read-only setup (macOS/Linux)
-----------------------------

1. In the running desktop Magpie, turn on Browser access in local mode. The
   pinned desktop default is http://127.0.0.1:3430; use its configured local
   Browser access port if it differs. Never use a LAN Browser URL or include
   the Browser key in the URL. The desktop endpoint authenticates this read
   using its `magpie_web_<port>` cookie, created by the desktop UI.
2. From this directory, run:

       python3 bridge.py

   Type/paste the Browser access key at the hidden terminal prompt. Only the
   key itself is accepted, not its full link. Alternatively, a secret manager
   or protected pipe can feed one line with:

       python3 bridge.py --browser-key-stdin

   For a different local desktop port and gateway port:

       python3 bridge.py --browser-url http://127.0.0.1:3431 --gateway-port 3425

   Open the Android connection using the displayed pairing value. With ADB,
   the usual local reverse is `adb reverse tcp:43425 tcp:43425`; then Android
   connects to https://127.0.0.1:43425 and verifies the displayed certificate
   fingerprint before authenticating. The default listener remains on loopback. For a trusted private network only, optionally bind one explicit RFC1918 IPv4 address:

       python3 bridge.py --lan 192.168.1.23

   For USB, use the project-level `tools/usb_start.sh`: after the app is installed and the phone is authorized by ADB, it creates the reverse tunnel, starts this bridge with a short-lived pairing file, and imports the token through the app's shell-UID-only provider. The phone does not ask for a USB pairing code. Do not copy the pairing file or token elsewhere.

   Restrict your firewall to trusted network peers, then enter
   `https://192.168.1.23:43425` and the displayed pair on Android. LAN mode
   uses the same pinned certificate and token, but is not an end-to-end tunnel:
   devices on the shared network can still attempt to disrupt service. Never
   port-forward this service or bind wildcard, public, IPv6, or hostname
   addresses. Choose only this computer's address on the intended private
   network. `--browser-url` is still restricted to a numeric loopback or
   `localhost` HTTP origin; no paths, credentials, query strings, redirects or
   arbitrary upstream URLs are accepted.

Opt-in currency write
---------------------

Writes are off unless explicitly enabled. To enable the one supported write,
with the local Magpie CLI available on PATH:

    python3 bridge.py --allow-write

Or choose a trusted installed CLI executable explicitly:

    python3 bridge.py --allow-write --magpie-bin /absolute/path/to/magpie

The command endpoint accepts only `setCurrency` with `usd`, `cny`, or
`provider`, and the current opaque revision. The bridge reads the latest
desktop's current /api/settings currency and checks
`magpie settings get currency --json` against it before invoking the fixed argv `magpie settings set currency VALUE`.
It does not invoke a shell or accept a command, file, setting name, or model
from the phone. As with stock Magpie's settings CLI/file update, the separate
read/check and later CLI write are not one transactional compare-and-swap:
another desktop edit in the tiny interval between those operations can race.
For strict multi-writer transactionality, leave writes disabled. There is no
model write; Magpie's agent settings are agent-specific files and are outside
this bridge's safe command set.

Data, refresh and bounds
-----------------------

GET /companion/v1/events is authenticated SSE. It sends protocol v1 with
agent id/name/current model, gateway availability and request count, currency
plus revision, and (when available) a short routing outcome summary. The
routing summary is omitted if its read is unavailable. `upstreamAvailable`
indicates when the desktop's cached state is unavailable. For data freshness,
the bridge reads agent/model data from Magpie's cached `GET /api/state?last=1`
and currency from `GET /api/settings`. It does not request the full agent/provider
scan. If the upstream has
not yet built a cached state or its cache does not match the pinned application
version, the agents/settings are reported unavailable. This is not a substitute
for a fresh full Magpie UI state.

Concurrency uses the pinned local-only `/v1/magpie/concurrency` loopback route.
Routing is a sampled projection of `/v1/magpie/trace`; no call body, prompt,
raw trace, key/account label, error text, session title or log is sent to the
phone. Route model names are limited to model IDs already present in desktop
state; only `success`, `error`, and `inFlight` outcomes cross the bridge. The
browser key is only sent as the desktop Browser cookie; the gateway endpoint is
separate and gets no Browser or gateway key.

No stream subscribers means no Magpie reads. With subscribers, a single shared
worker polls cached state/concurrency no faster than every five seconds and
routing no faster than every ten seconds; heartbeats are comments. There are at
most four event streams, twelve total client connections, and a 60 KiB SSE data
line; payloads, JSON depth via the runtime, sockets, and CLI are bounded. A
slow/disconnected phone is not allowed to stall upstream reads indefinitely.
No third-party packages, Android edits, provider requests, config writes, or
host state changes are made by the default run. Tests use fake local desktop,
gateway, and CLI servers/files only.

Pinned compatibility evidence
-----------------------------

Contracts were checked with targeted WebFetch excerpts from upstream commit
f288cc61af507e8a8289f4a08dc4368759cb9881 (the upstream source is not checked
out here):

* `internal/gui/apppage.go` and `internal/gui/web.go`: local Browser access is
  a localhost HTTP page. Its `web-page.json` key becomes an HttpOnly cookie
  named `magpie_web_<port>`; browser routes are key-protected.
* `internal/gui/web.go`: the page exposes GUI `/api` behind its own page key;
  the `onePort` gateway client routes are handled separately. The companion
  does not proxy these routes or any arbitrary request.
* `internal/gui/api.go`: cached state is `GET /api/state?last=1`, with
  `last:true`, `agents[].id/name/fields[].key/value`, and `settings`. Plain
  `GET /api/state` calls `provider.FetchNewSoon`, so the bridge purposely never
  uses it. The page state removes LAN, Bark and GitHub credentials and proxies.
* `internal/gui/trace.go`, `internal/gateway/trace.go` and the pinned gateway
  routes: routing trace uses `/v1/magpie/trace`, carries models/done/status and
  potentially sensitive details, which this adapter projects and filters.
  `/v1/magpie/concurrency` returns an object tagged `concurrency` and its
  `requests` in-flight count. Gateway reads are local-only, without a key.
* `internal/cli/settings_cli.go` and `internal/settings/settings.go`: the CLI
  supports `settings get currency --json` and `settings set currency VALUE`;
  accepted currency values are `usd`, `cny`, `provider`. `settingsapi.Apply`
  merges the one setting over the current settings so it does not replace
  unrelated fields. The upstream API deliberately isn't posted here because a
  partial settings JSON can replace fields.

Run the isolated integration suite from this directory:

    python3 -B -m unittest -v

The tests generate local ephemeral TLS certificates and use mocked upstream
services and a fake CLI; they do not connect to a real Magpie instance or
change the actual computer configuration.
