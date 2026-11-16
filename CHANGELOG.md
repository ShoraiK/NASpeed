# Changelog

Notable changes in each NASpeed version, newest first. Every 0.x version is a beta.

Each section opens with a one-line summary that the package build copies into the App Center release note, so keep that line plain text and no longer than 300 characters. The release workflow adds the whole section to the GitHub release notes, which turn every line break into a visible one, so write each paragraph and bullet on a single line.

## 0.3.2

A maintenance release from a full review of NASpeed. A test cut short by a restart of the service now says so instead of staying on screen as running, a few failures report their real cause, and code that nothing used is gone.

- If the NASpeed service restarts during a test, the web interface reports that the test stopped for that reason. Until now the test stayed on screen as if it were still running.
- When deleting a result or the whole history fails, the History view says why, in place of the line that gives how many results it keeps. Until now nothing happened.
- A test that fails while it opens its connections reports the real cause instead of an internal error.
- With Cloudflare, a malformed reply while NASpeed reads the connection details no longer stops the test: it falls back as it already did for network errors. The command line reports an unreachable Cloudflare as unavailable instead of failing.
- The command line prints a dash instead of "None" for a latency it could not measure.
- During a test, the service copies the speed trace only when it gains a point instead of on every update.
- Answers to the browser's preflight requests list only the methods the API uses: GET, POST and OPTIONS.
- Cleanup with no visible change: code that nothing used is gone, the providers share one helper for their short requests to a test server, and the web interface keeps repeated code in shared helpers.
- Five new tests cover these fixes.

## 0.3.1

Automatic mode keeps adding connections while the speed keeps rising, so a service that limits each connection no longer holds the result down. The M-Lab notice now comes before every M-Lab test until you hide it, and Settings describes every provider.

- During the warm-up, automatic mode tries twice as many connections and keeps doubling them, up to the limit set by the NAS's CPU count, while each step still raises the speed by at least half of what the new connections would add at the old speed per connection. The minimum for the speed observed still applies.
- A service that limits each connection, as Cloudflare's upload sometimes does, now gets more connections instead of the count its first second called for. Against a local server that reads each connection at 25 Mbit/s, an upload went from 99 Mbit/s with 4 connections to 787 Mbit/s with 32.
- Lines below 150 Mbit/s now finish with 8 connections instead of 4, and their warm-up, which is discarded, lasts about a second longer.
- Before every M-Lab test, the web interface says what M-Lab publishes as open data and keeps indefinitely: your public IP address, the date and time, and the results. Tick *Don't show this again* to stop it, and turn it back on in *Settings > Providers*. Until now it appeared only until you accepted it once.
- *Settings > Providers* describes each of the four providers, with a link to its website: what it is, how it measures and its main drawback.

## 0.3.0

Results now show the load on the NAS processor and warn when the NAS, rather than your line, may have limited the speed. Upgrades also refresh the interface reliably.

- Each download and upload records the processor load during its measurement window: the whole processor, its busiest core and NASpeed's own share, plus how often the platform held NASpeed back where Linux reports it.
- The result, the history and the command line warn when a saturated processor, a saturated core or a CPU quota may have kept the result below the line's speed. The CSV export gains the load and that reason per direction.
- The interface checks that it is talking to the NASpeed service, and that both are the same version. After an upgrade it reloads once instead of running an outdated copy from the browser cache.
- Upgrades replace the whole interface folder, so files removed from the interface no longer linger on the NAS.
- The App Center listing describes NASpeed in each of its 26 languages, and its release note comes from this file.
- Package builds are reproducible: every date inside the package comes from `SOURCE_DATE_EPOCH` or the last commit, so building the same commit again in the same environment gives the same bytes.
- The tests run on every push to `main` and on every pull request, with Python 3.10 and the latest Python 3.
- The details under each figure wrap between items instead of in the middle of one.

## 0.2.2

First package that installs on TOS 7. It fixes the service socket, refreshes the interface on upgrades and remembers your language and provider.

- The service socket lives in `/usr/local/naspeed/run/` and is linked into `/var/api`, which only root can write, by a privileged step before the service starts. The unit no longer depends on a `naspeed` group, which TOS does not create.
- Upgrades refresh the interface files: TOS only unpacks them on a fresh installation.
- An expired TOS session shows a sign-in notice instead of a broken page.
- The interface language and the provider of the last test are saved on the NAS. The first launch uses English and LibreSpeed.
- Better scrolling inside the TOS window, clearer provider descriptions and clearer history controls.
- 0.2.1 was never released; its fixes are part of this version.

## 0.2.0

First package published as a GitHub Release. It does not install on TOS 7, so use 0.2.2 or newer instead.

- Pushing a raised version to `main` runs the tests, builds the `.deb` and publishes it as a GitHub Release with its SHA-256 checksum.
- Known problem: the service unit requires a `naspeed` group and write access to `/var/api`, and a `chown` in the installation scripts fails, so the TOS App Center cannot install the package.

## 0.1.1

Groundwork for the TOS 7 package and better keyboard navigation. Never released.

- An offline Debian package builder that also runs on Windows, with lifecycle scripts and package tests.
- Keyboard navigation through the whole interface.

## 0.1.0

The first version of the service, the measurement engine, the four providers and the web interface. Never released.

- Latency, download, upload and latency under load, measured the same way for every provider, with one worker process per TLS connection.
- Cloudflare, Ookla, LibreSpeed and M-Lab NDT7 providers.
- A JSON API tied to the TOS session, a web interface with test, history and settings in six languages, and a command line.
