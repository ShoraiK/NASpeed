<div align="center">

<img src="webui/favicon.svg" alt="NASpeed logo" width="88" height="88">

# NASpeed

### The internet speed test that runs *on your TerraMaster NAS*

Measure the connection your TerraMaster NAS actually gets — not the one your browser sees.

<p>
  <img alt="TerraMaster TOS 7" src="https://img.shields.io/badge/TerraMaster-TOS%207-1f6feb?style=flat-square">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white">
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-none-2fd08a?style=flat-square">
  <img alt="6 interface languages" src="https://img.shields.io/badge/UI%20languages-6-ffb52e?style=flat-square">
  <img alt="Version 0.4.0 beta" src="https://img.shields.io/badge/version-0.4.0%20beta-fe7d37?style=flat-square">
  <a href="LICENSE"><img alt="License: GPL-3.0-only" src="https://img.shields.io/badge/license-GPL--3.0--only-a32d2a?style=flat-square"></a>
</p>

<p>
  <a href="#why-naspeed">Why</a> ·
  <a href="#features">Features</a> ·
  <a href="#providers">Providers</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#getting-started">Getting started</a> ·
  <a href="#permissions-ports-and-files">Permissions</a> ·
  <a href="#roadmap">Roadmap</a> ·
  <a href="#license">License</a>
</p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/screenshots/test-dark.png">
  <img alt="NASpeed after a test: large download, upload and latency figures with their traces, the four providers and the connection details" src=".github/screenshots/test-light.png">
</picture>

<sub>Screenshots show sample data and follow your light or dark theme.</sub>

</div>

> [!IMPORTANT]
> **Beta.** The backend and the web interface are complete, and the first installable TOS 7 package is
> available on the [Releases page](https://github.com/ShoraiK/NASpeed/releases). On one x86_64 TerraMaster NAS it
> has been installed, upgraded, uninstalled and reinstalled, used for real tests and checked with an expired TOS
> session. A NAS reboot is still untested, so expect rough edges — see how to [install it](#install-on-tos-7-beta)
> and the [roadmap](#roadmap).

> [!WARNING]
> **Do not install version 0.2.0 on TOS 7.** Its package fails during installation. Download **0.2.2 or newer**
> from the Releases page instead.

## Why NASpeed?

A speed test in your browser measures the path between *your computer* and the internet. That says very little
about your NAS, which may sit behind a different switch port, VLAN, firewall or uplink — and which is the machine
that actually runs your cloud backups, downloads, sync jobs and remote access.

NASpeed runs the test **on the NAS itself**, so the numbers are the ones your NAS really gets:

- Is my NAS getting the speed my ISP promises?
- Why are my cloud backups slower than the speed test on my laptop?
- Does the line behave differently depending on the provider, the server or the time of day?

## Features

- 🖥️ **Measures from the NAS, not from the browser.** A service on the NAS runs the test; the web UI only controls and displays it.
- 🌐 **Four providers in one place.** Cloudflare, Ookla, LibreSpeed and M-Lab. Pick a provider — and, where supported, a server — for each test, and compare.
- 📊 **The full picture.** Download, upload, idle latency, jitter and latency under load (bufferbloat), with live traces that show the warm-up apart from the measured window.
- ⚡ **Built for fast lines.** Parallel connections scale with the speed observed, and every TLS connection gets its own worker process so Python's GIL does not cap multi-gigabit links.
- 🩺 **Knows when the NAS is the bottleneck.** Every result records the processor load during the measurement and warns when a busy CPU or a CPU quota, not your line, may have held the speed back.
- 🕘 **History you control.** Keep 10 to 1000 results, page through them, switch history off entirely, filter, delete or export to CSV.
- 🎨 **Modern interface.** Light and dark themes, a layout that adapts to narrow windows and six languages: English, Español, Français, Deutsch, Português and 中文. Nothing is loaded from the internet — no CDNs, no web fonts.
- 🔐 **Secure by design.** Access is tied to your TOS session; requests without one are rejected.
- 🧰 **Command line included.** `naspeed test` and `naspeed providers`, with JSON output for scripting.
- 📦 **Zero dependencies.** Pure Python standard library: nothing to compile, nothing to keep up to date.

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset=".github/screenshots/history-dark.png">
  <img alt="The NASpeed history view: median download, upload and latency, a bar chart of every test, medians by provider and the list of saved results" src=".github/screenshots/history-light.png">
</picture>

<sub>The history view: medians overall and by provider, one pair of bars per test, and every saved result.</sub>

</div>

## Providers

| Provider | Protocol | Servers | Notes |
|---|---|---|---|
| **Cloudflare** | HTTPS | Automatic — the nearest Cloudflare edge | Nothing to configure |
| **Ookla Speedtest** | HTTP, falling back to HTTPS | Pick from the Speedtest server list; search by name or city | Unofficial protocol — see below |
| **LibreSpeed** | HTTP or HTTPS, as each server offers | Pick from the public LibreSpeed server list | Open-source project |
| **M-Lab NDT7** | WebSocket over TLS | The nearest server, located automatically | One connection, ~10 s per direction. Results are public — see below |

> [!NOTE]
> **Ookla** has no public API. NASpeed speaks the same HTTP protocol as popular third-party clients, which Ookla
> does not endorse and may change or block at any time. If that happens, the other providers are unaffected.

> [!WARNING]
> **M-Lab** publishes the result of every test as open data and keeps it indefinitely, **including your public IP
> address**. The web interface warns you before every M-Lab test until you tick *Don't show this again*, and you can
> turn the warning back on in *Settings > Providers*. The command line refuses to run one unless you add
> `--accept-notice`.

Each provider is a small, self-contained module in [`src/naspeed/providers/`](src/naspeed/providers), so adding another one is straightforward.

## How it works

```mermaid
flowchart LR
    browser["Browser<br/>TOS desktop"]

    subgraph nas ["Your TerraMaster NAS"]
        proxy["TOS platform proxy"]
        service["NASpeed service<br/>checks the TOS session"]
        engine["Measurement engine<br/>one worker process per TLS connection"]
    end

    servers(["Speed-test servers<br/>Cloudflare · Ookla · LibreSpeed · M-Lab"])

    browser -->|"HTTPS"| proxy
    proxy -->|"Unix socket"| service
    service --> engine
    engine -->|"TCP / TLS"| servers
```

- **Web UI** — static HTML, CSS and JS with no build step. Inside TOS 7 the platform serves the files and the page opens as a window on the desktop; when you run NASpeed from source, the service serves them itself.
- **Service** — a small Python HTTP server (standard library only). On the NAS it listens on a Unix socket behind the platform proxy; it can also listen on a TCP port, which is how you run it from source.
- **Engine** — runs one test at a time. The main process only coordinates and samples latency; the data itself moves through worker processes (TLS) or threads (plain HTTP), so the measurement never starves the coordinator.
- **Live progress** — the UI polls the service about ten times per second.

### How a test is measured

Every provider is measured with the **same method**, so results are comparable between providers.

| Step | What happens |
|---|---|
| **Server** | Candidate servers are ranked by latency; the best one is used unless you choose another |
| **Latency** | 12 samples of TCP connect time, reported as median, minimum, maximum and jitter |
| **Warm-up** | Connections ramp up first. This phase (at least 1.5 s) is **discarded** |
| **Download / upload** | Bytes are counted over a fixed window of **5 s, 8 s or 15 s** (Quick, Standard, Long) per direction. Speed = bytes × 8 ÷ window length, in decimal Mb/s |
| **Connections** | Scaled automatically — from 4 up to 32, adding more while the speed observed keeps rising, within limits set by the NAS's CPU count — or fixed by you |
| **Under load** | Latency keeps being sampled during the transfers, to expose bufferbloat |
| **NAS load** | The processor is read at the start and end of each window: the whole CPU, its busiest core and NASpeed's own share. The result warns when the CPU reached 90 %, one core 95 %, or a CPU quota held NASpeed back in 5 % of scheduler periods |
| **Payload** | Uploads send random, incompressible data |

Numbers will not necessarily match a provider's own website, which uses its own method.
M-Lab is the exception to the shared method: NDT7 uses a single connection for about ten seconds per direction.

<details>
<summary><strong>Design decisions</strong></summary>

<br>

- **Python standard library only.** Python 3.10 ships with TOS 7, so there is nothing to compile or install and no dependencies to keep up to date.
- **One process per TLS connection.** In a quick loopback feasibility test on a desktop CPU, a single TLS connection reached about 11.7 Gb/s, but several threads in one Python process fell to roughly 3.5–4 Gb/s — most likely because of the GIL. Giving each TLS connection its own process removes that ceiling. Plain-HTTP transfers use threads.
- **A native Deb app, not Docker.** A container would measure through Docker's bridge network, and TOS does not allow Docker apps to use host networking.
- **Polling, not streaming.** The UI polls for live progress because it cannot be assumed that the TOS proxy supports streamed responses.
- **No external resources.** The interface uses the system font and loads nothing from third parties — no CDNs, no web fonts, no analytics — so opening it never sends anything outside your network.

</details>

## Getting started

NASpeed is distributed as a TOS 7 package. The whole application — service and web UI — also runs from source on any machine with **Python 3.10 or newer**, with no dependencies to install.

On first launch, without saved preferences, the interface uses **English** and selects **LibreSpeed**.
Change the language in **Settings**; it is saved on the NAS and restored whenever the app opens.
The provider is remembered when a test starts, not merely when its tile is selected.

### Install on TOS 7 (beta)

NASpeed is not in the TerraMaster App Center yet. Download `naspeed_x86_64.deb` and `naspeed_x86_64.deb.sha256` from the [Releases page](https://github.com/ShoraiK/NASpeed/releases) and check the download on your computer. On Linux or WSL:

```bash
sha256sum -c naspeed_x86_64.deb.sha256   # check the download
```

On Windows, compare the output of `Get-FileHash naspeed_x86_64.deb` with the hash in the `.sha256` file.

Then open the **App Center** on the TOS desktop and install the `.deb` with its manual installation option. To upgrade, install a newer package the same way, on top of the installed one; your settings and history are kept.

To uninstall, open NASpeed from the App Center's **Installed** list and choose **Uninstall** in the menu next to **Open**. Leave the option to also delete the app's configuration files unticked to keep your settings and history: they stay on the NAS and come back if you reinstall — see [After uninstalling](#after-uninstalling). Ticking it is meant to delete them as well; that has not been tested yet.

This is how the package has been installed, upgraded, uninstalled and reinstalled on a real NAS. TerraMaster also documents `sudo dpkg -i naspeed_x86_64.deb` over SSH for developers; that route has not been tested with NASpeed.

### Run the web UI

From the root of the repository:

**Linux / WSL**

```bash
export PYTHONPATH=src
python3 -m naspeed --dev
```

**Windows (PowerShell)**

```powershell
$env:PYTHONPATH = "src"
python -m naspeed --dev
```

Then open **<http://127.0.0.1:18321>**. Development mode serves the interface straight from `webui/` over plain HTTP on the loopback address, needs no TOS session and keeps its data in `.devdata/`.

### Use the command line

```bash
python3 -m naspeed providers                                 # list the providers
python3 -m naspeed test --provider cloudflare                # run a test
python3 -m naspeed test --provider ookla --list-servers      # list a provider's servers
python3 -m naspeed test --provider librespeed --duration quick --json
python3 -m naspeed test --provider mlab --accept-notice      # M-Lab: accept that the result is public
python3 -m naspeed --version                                 # print the version
```

Run `python3 -m naspeed test --help` to see every option: server, duration, connections, IP version and more.

### Run the tests

```bash
python3 -m unittest discover -s tests
node tests/test_startup.js        # web UI startup checks, with Node.js
```

The suite runs entirely offline: it needs no internet access and no real speed-test servers. The
[CI workflow](.github/workflows/ci.yml) runs both on every push to `main` and every pull request, with Python 3.10
and the latest Python 3, and also checks that the web UI scripts parse and the package sources are valid.

> [!TIP]
> A speed test moves a lot of data. A Quick test on a 1 Gb/s line transfers roughly 2 GB, and the volume grows with the line speed.

> [!NOTE]
> On Windows, upload speed can read higher than the real figure because the system lacks `TCP_NOTSENT_LOWAT`. Linux is not affected, so use Linux — or the NAS itself — when the upload number matters.

## Project layout

```text
NASpeed/
├── src/naspeed/
│   ├── engine/       Measurement engine: latency, transfers, worker pool
│   ├── providers/    One module per speed-test provider
│   ├── api.py        JSON API
│   ├── auth.py       TOS session and CSRF checks
│   ├── httpd.py      HTTP server over a Unix socket or TCP
│   ├── store.py      Settings and history storage
│   └── cli.py        Command-line interface
├── webui/            Web interface: plain HTML, CSS and JavaScript, no build tooling
├── tests/            Offline test suite
├── packaging/        TOS 7 service, lifecycle scripts and developer metadata
├── tools/build.py    Windows-capable TOS 7 .deb builder
├── .github/          CI and release workflows, README screenshots (sample data)
├── CHANGELOG.md      What changed in each version
└── LICENSE           GNU General Public License v3.0
```

## Security and privacy

- **Tied to your TOS session.** On the NAS the service listens on a Unix socket (mode `0660`) reachable only through the platform proxy. Every request must carry a valid session cookie and a matching CSRF token, or it is rejected with `401`.
- **Careful on TCP.** When it listens on a TCP port — as in development mode — it only answers clients on local or private networks, and only for local host names, which guards against DNS rebinding. Requests that change state need a custom header and no foreign `Origin`, which guards against cross-site requests.
- **Hardened static files.** When the service serves the interface itself — in development mode or on a TCP port — it adds a Content-Security-Policy and `X-Content-Type-Options: nosniff`, and blocks path traversal. Inside TOS the platform's own web server serves the interface files, with its own headers.
- **No telemetry, no third-party resources.** NASpeed connects only to the provider you pick and its test servers. The interface loads nothing from the internet.
- **Private data.** Settings and history are written atomically with `0600` permissions. History can be capped, switched off or deleted at any time.

## Permissions, ports and files

What NASpeed needs on your NAS, and every file it writes there. This describes the TOS 7 package as
installed, upgraded and uninstalled on a real NAS. TOS installs apps on a storage volume and links them into
`/usr/local`, so `/usr/local/naspeed` points to a folder such as `/Volume1/@apps/naspeed`. Source development
mode (`--dev`) instead listens on loopback port `18321`, keeps application data in `.devdata/` inside the repository
and writes no log file. Explicit command-line options and `NASPEED_*` environment variables can override paths.

### Permissions

| Permission | Rationale |
|---|---|
| Network: no inbound port | The interface is reached only through the TOS platform proxy, over the Unix socket `/usr/local/naspeed/run/naspeed.sock`, reachable as `/var/api/naspeed.sock` |
| Network: outbound connections | To the provider you pick and its test servers, while a test runs or a server list is loaded — see [Ports](#ports) |
| File system: `/usr/local/naspeed/data/` | Settings and test history |
| File system: `/usr/local/naspeed/logs/` | Service log, rotated |
| File system: `/usr/local/naspeed/run/` | Holds the socket the TOS proxy connects to |
| File system: `/proc/stat` and the service's cgroup `cpu.stat`, read only | Processor load during a test, to tell when the NAS rather than the line may limit the result |
| File system: `/var/api/naspeed.sock` | A symbolic link to that socket. `/var/api` is not writable by an unprivileged user on TOS, so the service creates the link with a privileged `ExecStartPre` step and removes it on stop |
| User: `naspeed` (system user) | Isolated service execution — never `root` |
| Shared folder: None | NASpeed never reads or writes your files |

### Ports

The TOS package uses **no listening TCP port**; the platform proxy connects to the Unix socket. Provider
connections below are outbound and start only when you use that provider. Hostname resolution uses the NAS's
configured DNS resolver.

| Port | Protocol | Destination | Purpose |
|---|---|---|---|
| 53 (normally) | UDP or TCP, as configured by the OS | The NAS's DNS resolver | Resolve provider and test-server hostnames |
| 443 | HTTPS | `speed.cloudflare.com` | Cloudflare test |
| 443 | HTTPS | `www.speedtest.net` | Ookla configuration and server list |
| Published by each server, usually 8080 | HTTP, or HTTPS on the same port as a fallback | The Ookla server in use | Ookla test |
| 443 | HTTPS | `librespeed.org` | LibreSpeed server list |
| Published by each server, usually 80 or 443 | HTTP or HTTPS | The LibreSpeed server in use | LibreSpeed test |
| 443 | HTTPS | `locate.measurementlab.net` | Finds the nearest M-Lab server |
| 443 | WebSocket over TLS | The M-Lab server in use | M-Lab test |

Latency is sampled with plain TCP connections to the same server and port.

### Files created at runtime

| Path (relative to `/usr/local/naspeed/data/` unless noted) | Purpose | Format | Created when | Growth bound / rotation | Lifecycle |
|---|---|---|---|---|---|
| `/usr/local/naspeed/data/` | Container for settings and history | Directory | On start, if missing | Contents listed below | Persistent |
| `settings.json` | Your settings — including the interface language and the provider of the last test — and the notices you accepted or chose not to see again | JSON, mode `0600` | First time a setting changes (a test with another provider counts) or a notice is accepted | Typically under 1 KB; not a byte quota | Persistent — survives restarts, upgrades and a normal uninstall |
| `history.json` | Saved test results | JSON, mode `0600` | First saved test, unless history is off; also when history is cleared | Capped by result count (10–1000, default 300), not bytes. A result takes about 1.7 KB: roughly 0.5 MB for 300 or 1.7 MB for 1000; actual sizes vary | Persistent — survives upgrades and a normal uninstall; delete entries or clear it from the History view |
| `<file>.tmp-<pid>` | Atomic write of `settings.json` or `history.json` | JSON | On every save | One per file being saved; renamed over the target at once | Temporary — leftovers from a crash are removed on start |
| `<file>.corrupt` | An unreadable `settings.json` or `history.json`, set aside for inspection | As the original | Only if a file cannot be read | At most one per file; replaced if it happens again | Kept for inspection — safe to delete |
| `/usr/local/naspeed/logs/` | Container for service logs | Directory | On start, if missing | Contents listed below | Persistent |
| `/usr/local/naspeed/logs/naspeed.log` | Service log | Text: `[time] [level] [component] message` | On start | Rotated at 512 KiB, keeping 2 old files (`.1`, `.2`): about 1.5 MiB total | Persistent, rotated |
| `/usr/local/naspeed/run/naspeed.sock` | Socket for the TOS platform proxy | Unix socket, mode `0660` | On start | N/A | Removed on stop; a stale one is replaced on start |
| `/var/api/naspeed.sock` | Link the TOS proxy follows to the socket above | Symbolic link, created by root | On every service start | N/A | Removed on stop |
| `/usr/local/naspeed/webui/` | The interface files that TOS serves at `/naspeed/` | Directory with seven static files | Unpacked from `webui.bz2` by the platform on installation, and again by the package's `postinst` on every upgrade | Fixed, about 169 KB; rewritten on upgrade | Static between upgrades; removed on uninstall |

Inside TOS the platform, not the NASpeed service, serves the interface from that `webui/` folder. The platform only
unpacks it on a fresh installation, which is why `postinst` refreshes it on upgrades. It sends those files without
`Cache-Control`, so a browser may keep an old copy after an upgrade; the interface then notices that its version
differs from the service's and reloads itself once. NASpeed creates no cache
or files in `/tmp`, its measurement workers write no application data, and the command line keeps its results in
memory. The service also logs to stdout, captured by the system journal under systemd. When running Python
directly from source, Python may create ignored `__pycache__/` directories; set `PYTHONDONTWRITEBYTECODE=1` to
disable them. The browser keeps only the theme, a copy of the language (to paint the first frame) and how many results a
History page shows in local storage.

### After uninstalling

Uninstalling from the App Center stops the service and removes its systemd unit, the program, the `webui/` folder,
the `/usr/local/naspeed` link and the `/var/api/naspeed.sock` link. With the option to delete the configuration files
unticked, the following stays in the app's folder on the volume, and a reinstall uses it again:

| What stays | Why |
|---|---|
| `data/` | Your settings and history |
| `logs/` | The service log |
| `run/` | Empty: the socket is removed when the service stops |
| `LICENSE` | The licence text, which the uninstaller leaves in place |
| The `naspeed` system user | It keeps owning `data/` and `logs/`, and a reinstall reuses it |

TOS keeps a few files of its own in the same folder, and `dpkg` lists NASpeed as removed with its configuration
kept (`rc`).

## Roadmap

- [x] Measurement engine: latency, download, upload and latency under load
- [x] Cloudflare, Ookla and LibreSpeed providers
- [x] M-Lab NDT7 provider *(tested against live servers over IPv4)*
- [x] JSON API and service
- [x] Web interface: test, history and settings — six languages, light and dark themes
- [x] Offline test suite
- [x] Permission, port and runtime-file reference
- [x] Installable `.deb` package for TOS 7, built and published automatically as a GitHub Release
- [x] Installation, upgrade, uninstall and real tests on a TerraMaster NAS, with the Python 3.10 that ships with TOS 7
- [x] Expired TOS sessions and outdated cached interfaces handled inside TOS
- [x] Processor-load diagnostics: every result tells whether the NAS may have limited it
- [x] Continuous integration and reproducible package builds
- [ ] Remaining hardware check: a NAS reboot
- [ ] Installation guide
- [ ] Submission to the TerraMaster App Center

## Versioning

NASpeed uses plain numeric `MAJOR.MINOR.PATCH` versions, the format the TOS App Center compares to offer updates.
The current version is **0.4.0**, a beta. Every version is described in [CHANGELOG.md](CHANGELOG.md).

- **0.x releases are betas.** Features, settings and the API may still change between them. Beta status is
  tracked separately, never as a `-beta` suffix, so every release remains a plain, comparable number.
- **1.0.0 will be the first stable release**, once the package has been validated on real TerraMaster hardware.
- The version is defined once, in [`src/naspeed/__init__.py`](src/naspeed/__init__.py). It appears in Settings,
  in `/api/info`, in `python3 -m naspeed --version` and in every saved result.
- **Releases are automated.** Pushing a raised version to `main` runs the
  [release workflow](.github/workflows/release.yml): it runs the tests, builds the `.deb` and publishes a
  GitHub Release tagged `vMAJOR.MINOR.PATCH` with the package and its SHA-256 checksum. The release is marked
  as a pre-release while `BETA` is `True` in the same file and becomes a regular release once it is `False`,
  which is planned for 1.0.0. A version that already has a tag is never released twice. The release notes start
  with that version's section of [CHANGELOG.md](CHANGELOG.md), whose first line is also the App Center release note.
- **Builds are reproducible.** Every date inside the package comes from the last commit, or from
  `SOURCE_DATE_EPOCH` when it is set, so building a release's commit again in the same environment gives the
  same bytes. A Python linked against a different zlib may compress them differently, but the files inside are
  identical.
- **Review the version before each commit and push of a change or batch.** Increment it when the changes
  warrant a new application version, not automatically for every commit. Use a patch increment for compatible
  fixes and usability or packaging improvements, and a minor increment for significant new functionality
  during the beta. Documentation-only changes, tests and internal refactors without observable changes
  normally do not require an increment.
- When incrementing the version, update the source, this README and [CHANGELOG.md](CHANGELOG.md) together, verify
  that generated package metadata and the executable report the same version, and explain the increment in the
  commit message.

## Development notes

- Code stays compatible with Python 3.10 and uses the standard library only.
- The web interface is plain HTML, CSS and JavaScript and must not load external resources.
- Line endings are LF, enforced by `.gitattributes`.
- Tests must stay offline.

## License

NASpeed is free software, released under the **GNU General Public License, version 3 only**
([`GPL-3.0-only`](LICENSE)).

You are free to use, study, share and modify it, including commercially. Redistributed copies and modified versions
of NASpeed must retain this license and notices, make the corresponding source available as GPLv3 requires, and
impose no further restrictions on recipients. Repackaging does not permit replacing NASpeed's license; independent
programs merely bundled alongside it keep their own licenses. NASpeed comes **without warranty**. The full terms
are in [LICENSE](LICENSE).

## Disclaimer

NASpeed is an independent project. It is not affiliated with, endorsed by or sponsored by TerraMaster, Ookla, Cloudflare, LibreSpeed or Measurement Lab. All product and company names are trademarks of their respective owners and are used here only to identify the services NASpeed can test against.

<div align="center">

**NASpeed** — know the speed your NAS really gets.

</div>
