# HueCanvas
A local album-art-driven lighting dashboard for two color Hue bulbs and two warm-white bulbs. Python 3.11+; mock mode is the default. This repository itself is the project root—no extra huecanvas folder is needed.

## Setup and run
```sh
cd /Users/adiunni/Github/HueCanvas
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
For subsequent launches, run `./start.sh` or double-click **start.command** in Finder. The launcher uses this folder's `.venv`, creates it before installing packages if it is missing, and installs dependencies if HueCanvas or Uvicorn is not installed. You do not need to activate the environment. Press Ctrl+C in its Terminal window to stop the server. To use another port, run `PORT=8001 ./start.sh`.

Open http://127.0.0.1:8000. Upload a PNG/JPEG (maximum 10 MB); its palette automatically starts the mock lighting engine. Edit swatches, select a mode, and set brightness, white fill, cycle speed and transitions. Controls are responsive on mobile. The optional web fonts fall back to system fonts offline.

```sh
.venv/bin/python -m pytest -q
```

## Behavior
Four mock lights have explicit roles. Real lights are initially disabled and unassigned: choose exactly your four bulbs and assign roles. Other discovered lights remain untouched. Capability detection comes from v2 light resource fields. Warm-white bulbs never receive XY color. Color-temperature capability is reported, but this MVP leaves temperature unchanged. Names/roles/enabled choices persist in `.huecanvas/lights.json`; renaming is local to HueCanvas.

Start captures the enabled bulbs' actual pre-session state. Pause and Stop hold the current appearance; Restore writes the captured on/off, brightness and XY state. Disable a light to exclude subsequent animation commands. Identify invokes Hue's native identify action (simulated acknowledgment in mock mode).

Static, rotating, RGB crossfade, gentle breathing and manual BPM modes all use album colors. The ambient BPM mode creates a four-bar brightness envelope with a minimum four-second period. Ordinary REST animation frames occur at most once every two seconds, and each bridge request is spaced at least 250 ms apart. Transitions are at least one second. Transient transport errors retry connection once; animation failures back off to 30 seconds. The dashboard reports errors. REST preview is an illustration using the palette, not measured bulb output or an accurate colorimetric simulation.

## Connect real Hue hardware
For the requested local setup, only the bridge LAN IP and its application key are needed:

```dotenv
MOCK_MODE=false
HUE_BRIDGE_IP=192.168.1.2
HUE_APPLICATION_KEY=your_generated_username
```

Restart the server after editing `.env`. Local bridge requests use HTTPS with certificate verification disabled by default (`HUE_TLS_VERIFY=false`). HTTPS still encrypts traffic, but this mode does not authenticate the bridge certificate. This policy applies only to private LAN bridge addresses; public discovery continues to use verified HTTPS.

The IP field accepts a bare IP, `IP:443`, or `https://IP:443`. The app sends the key in the `hue-application-key` header to `/clip/v2/resource/device/` and `/clip/v2/resource/light`. Existing `HUE_BRIDGE_ID` and `HUE_CA_CERT` settings are ignored in this local mode, so the PEM file is optional.

Use the `username` returned by `POST /api` as `HUE_APPLICATION_KEY`. The separate `clientkey` is for Entertainment streaming and is not needed for REST control. The pairing label `hue_canvas` is valid and does not need to match any later request. The JSON field is lowercase `devicetype`, for example `{"devicetype":"hue_canvas"}` ([official pairing guide](https://developers.meethue.com/develop/get-started-2/)). No new pairing is needed if you already have a working application key.

If you have no key, press the physical bridge button and click Pair with the bridge IP filled in. HueCanvas uses that same `hue_canvas` label and saves the returned application key in `.huecanvas/credentials.json` with mode 0600. The file and `.env` are ignored by Git.

Assign COLOR_LEFT/RIGHT to the two color bulbs and WHITE_LEFT/RIGHT to the warm-white bulbs; select **Enable bulb** for only those four lights. Clear a role before reassigning it.

### Optional verified TLS
To verify the bridge certificate instead, set:
```dotenv
HUE_TLS_VERIFY=true
HUE_BRIDGE_ID=your_16_character_bridge_id
HUE_CA_CERT=hue.pem
```
Use a trusted Hue CA from the official [HTTPS guidance](https://developers.meethue.com/develop/application-design-guidance/using-https/) (developer login may be required). Relative PEM paths resolve from the project root. Verified mode checks the certificate chain and bridge identity, and fails closed on verification errors; it never falls back to unverified TLS.

Bind to loopback. This local tool has no user authentication and is not intended to be exposed to the internet. Cross-origin browser writes are rejected.

## Optional macOS Music
Set `APPLE_MUSIC_ENABLED=true` and restart. Grant macOS Automation access for Music/System Events when requested. Track identity is checked on a half-second cadence using Music's persistent ID and metadata. A track change updates the title, cancels unfinished artwork work for the old track, and starts loading the new cover. Artwork is delivered through live events before palette extraction completes, so clustering and the ordinary dashboard poll no longer delay the cover. Tracks on the same album can have different artwork and are checked separately.

Raw covers are cached on disk by track identity; palettes are cached by artwork hash. Missing/downloading covers retry after two seconds. Export verifies the expected track ID, preventing an old request from reading a newer track's cover. Apple Music may still delay metadata or artwork availability; this is near-immediate delivery once Music provides the image, not zero-latency track detection. PNG/JPEG covers are preserved and other image formats are normalized. Manual upload stays available.

A new track clears the old audio tempo/drop history. Percussion effects continue responding to new attacks while BPM collects a fresh window. This integration follows Apple Music, not Spotify or browser artwork.

## Extension boundaries
`app/hue/entertainment.py` implements HueStream v2 XY/brightness frames over DTLS 1.2 PSK, using OpenSSL through `ctypes`. Beat effects stream at 20 fps to the enabled color bulbs only. Warm-white lamps default to a slower music-driven brightness response over REST, or optional steady fill. Their worker runs separately from the color stream. MIDI clock, Ableton Link and DJ triggers remain future adapters.

## Hardware verification checklist
Mock tests verify extraction (including dark themes), color conversion, triangle gamut clipping, capability-safe payloads, transitions, modes, state restoration and HTTP upload/control behavior. They do not establish real hardware behavior.

On your actual bridge: verify the selected TLS mode and application-key authentication; refresh and inspect capabilities; identify and assign all four bulbs; upload representative artwork; confirm only the enabled bulbs change; confirm white bulbs only change brightness; test the ordinary modes at conservative brightness; confirm smooth transitions; test beat effects separately; pause, stop and restore; disconnect/reconnect the bridge and observe error/backoff behavior. Independently validate macOS Music permissions, artwork retrieval and album changes. Verify streaming area release on Pause, Stop, Restore, audio stop and application shutdown.

## Troubleshooting real lights
- Stop the previous server with Ctrl+C and restart after changing `.env`. The dashboard must show **LIVE HUE**, not **SIMULATION**. Only one process can listen on port 8000.
- An empty `HUE_APPLICATION_KEY` requires physical-button pairing in the dashboard. A bridge ID is not an application key. A generated key is saved to `.huecanvas/credentials.json`.
- Select **Enable bulb** for your four bulbs. Real bulbs start disabled. Start now reports an error if none are enabled.
- If the bridge is unreachable, check that it is powered and the Mac shares its LAN; check VPN routing and System Settings → Privacy & Security → Local Network for the app launching the server. Discovery can identify a bridge even when direct LAN access is blocked.
- Run `.venv/bin/python examples/check_bridge.py` from the same terminal used to launch the server for a read-only device-endpoint and authentication check. The diagnostic does not print keys or change lights.

## Browser requests and the Hue application key
The browser sends control requests to FastAPI on localhost (for example `POST /api/control/start`). Python then sends bridge requests such as `PUT https://<bridge_ip>/clip/v2/resource/light/<light_id>` with the `hue-application-key` header loaded from `.env`. That header is set on the shared HTTPX client in `app/hue/client.py`, so it is attached to device discovery, light discovery and light updates.

Browser DevTools only shows the browser-to-FastAPI requests. It cannot display the separate Python-to-bridge requests. The key is kept on the server and is not returned in state responses or embedded in frontend JavaScript. The diagnostic script tests the authenticated bridge endpoint from Python without printing the key.

## Starting and changing colors
Real bulbs start disabled. In **Your lights**, click **Enable all four** (shown only when exactly four bulbs are discovered) or check **Enable bulb** individually. The selection count confirms what Start will control; Start is disabled with no selected bulbs. Assign the left/right roles to choose each bulb's palette position.

**Static album palette** applies the colors once. For continuous color changes choose **Slow color crossfade** or **Rotating album palette**, then Start. Breathing and BPM modes keep the album colors and vary brightness. A single-color artwork produces identical swatches, so add different colors manually if you want visible color movement.

Start waits for pending browser settings saves. Saved bulb selections survive a temporary discovery failure and are reapplied after Refresh or the next Start. Identify is independent of bulb enablement; it can work even when no bulbs are selected for animation.

Hardware spot check (October 10, 2026): the actual bridge returned four connected bulbs, two with XY color support. It accepted Start and three-second REST transitions; both color bulbs reported palette colors and subsequent color movement. The pre-test on/off, brightness and XY state was restored. This spot check does not validate every mode, long-running reconnection behavior or Music integration.

## Automatic BPM and beat effects
1. In **Audio MIDI Setup**, create a **Multi-Output Device** containing your speakers/headphones and **BlackHole 2ch**. Select it as the Mac sound output, so Music audio reaches both your speakers and BlackHole. Follow the driver's [official multi-output setup](https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device). HueCanvas does not change system audio routing automatically.
2. Keep the matching `HUE_APPLICATION_KEY` and `HUE_CLIENT_KEY` in `.env`. `HUE_CLIENT_KEY` is the `clientkey` from the same `POST /api` response, generated with `{"devicetype":"hue_canvas","generateclientkey":true}`. A working application key alone is enough for REST, but DTLS streaming needs both keys. Restart after editing `.env`.
3. Create an Entertainment area in the Hue app with exactly the two color bulbs. HueCanvas automatically selects a matching inactive area (your **Music area** is compatible). Stop other Hue Sync applications before using it. `HUE_ENTERTAINMENT_AREA` can select a specific area's UUID.
4. In HueCanvas, choose **BlackHole 2ch** under **Listen to your music**, then click **Detect BPM**. Allow audio-input access for the launching app if macOS requests it. Tempo and periodicity confidence appear after 8–20 seconds. No recording is saved; analysis keeps at most 20 seconds in memory.
5. Choose **Detected system audio BPM** as the tempo source (Detect BPM selects it automatically), then **Beat · gentle brightness pulses** or **Beat · sharp high / low flashes**, and click Start. Enable your two color bulbs; enable the two white bulbs and choose **White response → Music brightness** or **Steady white fill**.

Both gentle and sharp effects support **Pulse pattern → Together / Staggered** and **Beat trigger → BPM clock / Drums & percussion**. Gentle stagger smoothly hands the brightness peak from one color bulb to the next. Sharp switches between peak and **Pulse floor**, a percentage of peak brightness. **Pulse every** chooses 1, 2 or 4 clock beats or accepted percussion attacks. Sharp flashes are capped at three per second.

**Palette movement → Cycle and blend album colors** makes both beat modes change color while pulsing. **Blend to the next color every** sets beats per blend; without a BPM estimate, the Animation cycle speed is used. Choose **Keep each bulb's album color** to disable cycling. White lamps follow music brightness, and Staggered alternates their targets over slower beat groups once tempo is available.

Automatic detection estimates tempo from spectral onsets, autocorrelation and regular onset intervals, and aligns pulses to the measured beat phase. The supported analysis range is 55–190 BPM. Rhythm-free sections and unusual beat patterns may produce no estimate or a half/double-tempo interpretation; select Manual BPM to override it. While listening, two seconds of sustained silence (after the one-second audio energy window drains) restore the enabled bulbs to their pre-Start on/off, brightness and XY state and release the Entertainment area. Capture stays active and playback resumes after sustained audio returns; BPM-clock streaming waits for a reliable tempo. Short gaps keep playing, and gaps between percussion attacks stay at the pulse floor until the silence timeout. Manual playback without audio capture continues normally. Stop listening pauses an audio-driven streaming effect, including percussion triggers even when Manual BPM is selected. Pause, Stop and Restore release the Entertainment area; Restore also returns the original bulb state.

OpenSSL with DTLS-PSK support must be available. On this Mac, `/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib` is used; another installation can be supplied with `HUE_OPENSSL_LIB`. Credentials stay in process memory and are not passed as shell arguments. Cloud discovery certificate verification is unchanged.

Verification: 141 tests pass, covering bass-priority thresholds, boundary comparisons and rearming, spectrum frequency and volume calibration, color-preview rendering, capture opt-in, percussion timing, drop rejection, gentle staggering, palette cycling, artwork delivery and skip cancellation, synthetic drum patterns from 60–180 BPM, silence/noise rejection, sample-rate conversion, beat envelopes, the flash-rate cap, frame encoding, area selection, cleanup after streaming failure, mock brightness/white-fill behavior, audio stop and capture lifecycle. LiveBPM also detected a synthetic 120 BPM signal delivered through the installed BlackHole driver by a separate playback process. CoreAudio stream creation and closure occur on the server's main thread. Tempo and on-demand spectrum analysis run in workers; live onset and threshold summaries run in the capture callback. The actual bridge accepted a DTLS 1.2 PSK handshake and 30 steady HueStream frames; its Music area became active and was released afterward, with the pre-test bulb state restored. Full playback timing should be checked with the actual songs and your chosen audio routing.

## Live spectrum and color preview
Launch HueCanvas, choose **BlackHole 2ch** in **Listen to your music**, then click **Start listening** in **See the sound** (or **Detect BPM** below). Play music through your configured Multi-Output Device. The frequency graph appears within a short audio window, without waiting for a BPM estimate. Starting capture does not start a lighting effect.

The graph spans 20 Hz–20 kHz, limited by the input sample rate, on a logarithmic frequency axis with a fixed −90 to 0 dBFS amplitude scale. Bass (20–250 Hz), midrange (250 Hz–2 kHz), and treble (2–20 kHz) cards show integrated band RMS levels; bars show peak amplitudes, so their dB values can differ. The strongest frequency is shown above the preview. Silence and stalled capture clear the visual rather than holding an old spectrum.

Choose **From the album palette** to color the bands from the current artwork, or edit the three band swatches to try your own mapping. The **Live color blend** combines those colors in proportion to band RMS and dims with input volume. These are local visual preview controls; they do not send color changes to the bridge. The spectrum shows mixed frequencies rather than separated instruments, so a band may include drums, vocals, and other sounds.

The spectrum shares the existing explicitly started input stream and sends only derived levels to the browser. FFT analysis runs outside the audio callback and lighting loop. The graph refreshes up to ten times per second; hidden tabs stop requesting updates. No raw audio is saved or sent to the browser. Stop listening ends capture and clears the graph.

## Frequency levels with bass priority
After starting BlackHole listening, choose **Beat · gentle brightness pulses** or **Beat · sharp high / low flashes**, then **Beat trigger → Frequency levels · bass priority**, and click **Start**. No BPM estimate is required. Together/Staggered, palette cycling, pulse division, and timing delay apply to these crossings too. White bulbs keep their slower music-brightness worker.

Defaults implement the requested strict comparisons:
- Bass **above −15 dBFS** creates a pulse.
- If bass is **below −70 dBFS**, mids **above −20 dBFS** can create a pulse.
- Between −70 and −15 dBFS bass blocks mids and neither band creates a pulse. Exactly −70 dBFS does not enable mids; equality at a trigger threshold does not fire.

All three levels are adjustable in the beat controls; bass fallback must remain below the bass trigger. A band fires once on entry above its threshold and rearms when it falls at least 2 dB below its trigger. A sustained note therefore does not generate repeated flashes. If mids were already loud while blocked by bass, their first eligible crossing can fire when bass falls below the fallback. Sharp flashes retain the three-per-second cap; gentle pulses retain their smooth decay and stagger.

Thresholds compare **band RMS**, matching the spectrum cards (bass 20–250 Hz; mids 250 Hz–2 kHz), rather than a single spectrum bar's peak amplitude. The detector uses the same calibrated 8192-sample Hann window, updated every 10 ms once full. At 48 kHz this spans approximately 171 ms; its smoothing, plus capture and bridge latency, can delay responses compared with the percussion onset trigger. Absolute dBFS levels depend on the player's volume and mix. Use the spectrum cards and live gate readout to tune values for your music. Spectrum previews remain independent of actual palette settings.

This level gate is separate from **Drums & percussion**, which detects spectral changes and broadband attacks, and from **Drop response**, which requires a sustained energy lift with percussion. Crossing −15 dBFS alone is not evidence of a musical drop. Drop accents remain controlled by the existing drop setting.

## Drums, percussion, and drop accents
After clicking **Detect BPM**, choose gentle or sharp pulses and set **Beat trigger → Drums & percussion**. No stable BPM estimate is needed to start. Detection uses short spectral changes, broadband attacks and an adaptive novelty baseline rather than bass energy alone. Sustained bass is rejected. This is an onset heuristic for a mixed signal, not instrument source separation: noisy vocals or other transients can trigger it, and quiet or tonal percussion can be missed. It uses the spectral novelty approach described in [aubio's primary documentation](https://aubio.org/doc/0.4.6/specdesc_8h.html).

**Hit sensitivity** controls quieter attacks. Together pulses the color bulbs together; Staggered advances through COLOR_LEFT, COLOR_RIGHT, then other enabled color bulbs by ID. Gentle percussion pulses decay smoothly and can follow rolls; sharp flashes keep the three-per-second limit and ignore old attacks. Existing bass trigger values remain accepted by the API for compatibility, but are no longer offered by the dashboard.

**Drop response → Accent pulse and advance palette** estimates a drop when percussion coincides with a sustained energy lift above an established quieter section. It requires at least 150 ms of confirmation and has an eight-second cooldown; isolated hits and steady noise are rejected by tests. A confirmed drop advances the palette and widens the pulse while preserving the chosen stagger pattern and brightness ceiling. Recognition is reactive, may miss or misidentify musical drops, and does not predict future drops. **Keep the regular pulse** disables the accent.

Capture requests the device's low-latency mode. Events use [PortAudio's input sample timestamps](https://files.portaudio.com/docs/v19-doxydocs/structPaStreamCallbackTimeInfo.html); late frames advance the envelope instead of restarting a pulse at the wrong time. **Light timing delay** adds up to 250 ms if BlackHole hears a beat before your speakers play it. It cannot remove capture/bridge delay. Streaming remains 20 fps, so scheduling can add up to roughly 50 ms, plus hardware/network latency. White bulbs follow slow groups, not individual percussion attacks.

Verification (October 11, 2026): the actual BlackHole driver delivered five synthetic percussion attacks in opposite-polarity stereo; all five were detected, with timestamp errors of approximately 1.1–1.2 ms. This validates onset timestamps through the virtual driver, not visible bulb synchronization with songs. A temporary simulation server delivered the current Apple Music artwork through live events and opened/closed low-latency BlackHole capture. A read-only Music check fetched metadata in approximately 391 ms and artwork in 150 ms.

## White lamps and brightness-only music
Your two LWA025 Hue White bulbs expose dimming but no Entertainment renderer service. Brightness is supported; fast Entertainment streaming is not. Hue also advises against continuous fast updates over the [REST API](https://developers.meethue.com/new-hue-api/), so the white lamps use a slower music envelope.

Select **Music · brightness only · all bulbs** to make every enabled dimmable bulb change brightness while preserving its existing color and color temperature. This mode uses only the bridge IP and application key; no Entertainment area or client key is required. Choose Detected system audio BPM and start BlackHole listening, or select Manual BPM. **Main brightness** sets the color bulbs' peak, **White peak / fill** sets the whites' peak, and **Pulse floor** sets the low level as a percentage of peak.

With audio listening active, **White response** defaults to **Music brightness** in every lighting mode, including Static, Crossfade and Rotate. Fast beat effects also support manual BPM without capture. Select **Steady white fill** to hold the whites steady instead. When BPM is available, the REST envelope alternates high and low over whole groups of beats: at 120 BPM it changes target every four beats (two seconds). At other tempos, whole beat groups keep the interval at least two seconds. Without an audio BPM estimate, brightness follows recent audio energy. Percussion triggers use the detected tempo or audio energy for the whites even if the color-effect tempo source is set to Manual. Sustained silence or missing input restores the original state rather than holding the music brightness.

REST brightness updates are limited to one per bulb every two seconds, with one-second transitions. These lamps therefore follow musical phrases rather than individual bass hits or fast drum-roll strobes. Their independent background worker runs even when the color mode is Static, and regular color updates do not overwrite its brightness targets. It also does not block the 20 fps color stream. Pause, Stop and Restore stop both workers; Restore returns the original on/off, brightness and XY state. Stop listening pauses audio-driven brightness-only mode too.

Tests cover beat grouping, audio-energy fallback, per-bulb rate limits, brightness-only payloads, white-only operation without an Entertainment area, concurrent color frames during white REST updates, white background updates in all regular color modes, silence restoration, automatic resume, silent startup, restoration retry, and explicit Pause/Restore cancelling automatic resume.

Hardware brightness check: the updated white background worker in Static mode drove both LWA025 lamps to approximately 30% and 12% brightness. Simulated silence restored their original on/off and brightness state, and color-bulb states stayed unchanged. This used a controlled envelope on the actual bridge, not live-song timing. Both white bulbs are enabled in the local saved preferences. Earlier checks also confirmed gentle 30% and 18% REST transitions. Their original state was restored, and color-bulb states remained unchanged. BPM tracking now preserves a continuous beat anchor across successive measurements, so groups of beats and staggered patterns can continue across analysis windows.

## BlackHole shows zero or cannot find a beat
BlackHole receives audio only when the player sends output to it. HueCanvas now checks the Mac's actual default output and the active members of aggregate devices. A device merely named Multi-Output is not enough: it must include BlackHole and speakers/headphones, and it must be selected for sound output. Click **Use Multi-Output for Mac sound** to select an existing compatible device. This explicit action changes the Mac sound output; opening HueCanvas or starting capture does not change routing automatically. **Check routing** refreshes the diagnosis.

The input panel now distinguishes:
- **Not listening:** click Detect BPM; launching the server does not begin capture.
- **Frames arriving, input silent:** play music and check its output route. Zero input is not a BPM-estimator failure.
- **Capture stalled:** no callbacks are arriving; restart listening and check permissions or device availability.
- **Audio present, no stable BPM:** the sound reaches HueCanvas but has no reliable tempo estimate yet. Percussion effects and music brightness can still follow the signal, or select Manual BPM.
- **Processing/open error:** the error remains visible; Restart listening can reopen capture without restarting the whole server.

Raw input level in dBFS, per-channel levels and received-frame counts are exposed in audio status. Analysis uses the stronger stereo channel with hysteresis, preserving sound from one-sided or opposite-polarity stereo sources that averaging could cancel. Silence is diagnosed within about half a second; BPM still requires eight seconds of usable audio. Callback errors are surfaced rather than silently leaving a stream that appears active.

If using rekordbox or another DJ app, its selected output may override macOS: select the compatible Multi-Output Device in that app, or send a dedicated output to BlackHole. An aggregate containing only speakers does not feed BlackHole. Follow the driver's [official setup](https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device) for device ordering, clock source and drift correction if playback itself stalls.
