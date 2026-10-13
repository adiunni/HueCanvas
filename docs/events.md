# Musical events: audit and implementation

## Repository assessment (2026-10-12)

The existing application is Python/FastAPI with vanilla JavaScript, HTML and CSS.
NumPy/SciPy process an explicitly opened sounddevice input: a 20-second memory
buffer, worker-based tempo estimation and spectrum analysis, and callback-based
percussion, drum isolation and frequency gates. Capture does not save recordings.
An existing reactive drop heuristic requires sustained energy and percussion.

Apple Music supplies identity, playing state and artwork through AppleScript.
rekordbox supplies history-based identity and estimated deck activity via MIDI;
it has no reliable position, seek, loop or playback-rate telemetry. ArtworkSync
polls identity independently of artwork extraction. Neither adapter originally
provided a scheduling clock. No event schema, cue editor or waveform exists.

Engine owns lifecycle, state restoration, album palettes and the animation loop.
Two color bulbs support color/brightness; the two white bulbs receive only
capability-supported REST properties. EntertainmentSession already implements
HueStream/DTLS at 20 fps. Its area must contain exactly the enabled color bulbs.
The independent white worker limits brightness writes to one per bulb per two
seconds with transitions of at least one second. HueClient spaces requests by
250 ms. Local TLS settings and verified discovery must remain unchanged.

Recent history: 7e3601a restores all discovered lights after silence; e1138e6 adds
rekordbox/deck support; 120c47a adds stem-based percussion and dashboard controls.
README contains the available project notes and historical hardware reports;
no repository-local session summaries were present. Existing tests cover DSP,
capture lifecycle, Music/decks, palettes, capabilities, streaming encoding,
REST limits, restoration and the browser spectrum. Mock tests cannot validate
Music automation, physical bulbs, DTLS timing or song-level event accuracy.

## Plan

1. Preserve Engine and its workers. Add a versioned persisted cue model, a
   monotonic transport and reusable envelopes; test a manual final-impact cue.
2. Extract and cache bounded local WAV features using existing NumPy/SciPy.
   Detect section changes and rank terminal accents independently. Preserve edits.
3. Extend the dashboard with a candidate list, cue editor, preview and explicit
   cue-clock controls. No frontend replacement or automatic capture.
4. Integrate transport, de-duplication and latency compensation. Add optional
   Apple Music position telemetry; label rekordbox's clock as manual.
5. Analyze rolling live features outside the callback, expose reactive detection
   delay, and test conservative cooldown/hysteresis behavior.
6. Run synthetic and mock integration tests, then document real-audio/hardware
   checks separately. Do not open hardware or capture during automated tests.

## Try one final-impact cue

1. Start with `MOCK_MODE=true` (or explicitly run
   `MOCK_MODE=true APPLE_MUSIC_ENABLED=false REKORDBOX_ENABLED=false ./start.sh`).
   Keep your existing album palette and choose **Ambient → Static**.
2. Open **Event Detection**, enter a stable track identifier, and **Load cues**.
   Enable event effects. Leave Reduced motion on for the first audition.
   Choose Entertainment to exercise the simulated color stream, or REST for
   a deliberately slower transition. No audio input is needed for this test.
3. Choose **New manual cue**, **FINAL_IMPACT**, **CINEMATIC_IMPACT**, and set
   its timestamp to **5 seconds**. This is a demonstration time, not an
   annotation for any song. Save, then use **Preview on screen**.
4. Set the cue clock to 0 and click **Seek / apply rate & loop**. Press the
   dashboard's **Start**. Around 5 seconds, the two color bulbs share an impact
   envelope and return to their album colors; the white bulbs respond slowly.
   **Audition lights** runs the editor's effect immediately, independently of
   whether the cue is enabled or accepted. Pre-dim, if configured, precedes it.
5. Pause and resume with the header controls. Seek backward and run again to
   rearm cues; seeking forward past a cue skips it. **Restore** returns the
   captured original states and releases the Entertainment session.

For actual four-bulb output, opt in with `MOCK_MODE=false`, retain the existing
application-key/TLS setup, and enable/assign the four bulbs. Entertainment also
needs the existing client key and area containing exactly the two color bulbs.
REST works without an Entertainment key and cannot reproduce a fast impact.
Brightness-only mode continues to use REST and preserves all existing colors.

## Authorized local files and review

Choose a mono/stereo PCM or float WAV (8–192 kHz, 2 seconds–20 minutes,
up to 128 MB), then **Analyze track**. Compressed formats are intentionally not
part of this implementation. The browser player uses a local object URL; the
analysis upload goes to the local FastAPI process. Neither the upload nor live
PCM is saved as an audio recording. Feature arrays, SHA-256, duration, algorithm
version and cues are cached under `.huecanvas/events/`. Analysis runs in a worker;
the scheduler reads a small separate cue document. Identical bytes and algorithm
versions reuse the cache. Give different mixes/edits different track identifiers.

Extraction resamples to 12 kHz and uses 85 ms FFT windows at 20 ms hops. It caches
RMS, bass energy, spectral flux, onset strength/density, section novelty, boundary
energy change, tail energy and silence, plus median/MAD/percentile normalization.
Beat candidates use repeated onset intervals; downbeats are a strongest-phase
4/4 hypothesis, **not** reliable meter recognition. Drops require sustained
multi-second contrast, bass/rhythm return, structural change and repeated attacks.
Final impacts rank unusually strong accents with sustained reduced activity
afterward; another comparable later accent disqualifies an earlier one. Search
uses tail evidence rather than a fixed distance from file end. Fades, ordinary
percussive outros and tracks without a distinct hit are allowed.

Candidates start pending. **Accept**, **Reject**, **Edit**, timing, effect and
enable controls are in the list/editor. Saving a manual cue accepts it by default;
saving an edited pending candidate still requires acceptance. Acceptance is an
explicit override of the confidence filter. Reanalysis preserves manual cues,
edited timing, effect settings, disabled cues and rejected candidates, matching
their original positions as well as edited positions. Rejections are retained
as records so reanalysis cannot silently recreate them.

The player sends play/pause/seek/rate updates to the cue clock; it does not start
the lighting engine or audio capture. Start lights separately. A configured loop
is enforced by the local player and scheduler. Manual clock buttons only control
the lighting timeline. They never seek or play an external music application.
Loop boundaries must be at least a second apart; cues at the exclusive loop end
do not fire. Offline cue playback requires this clock or position telemetry.

Apple Music position following is optional, using the existing half-second
metadata poll with new position/duration fields. An unavailable/paused player
pauses lighting that was running; resumed playback can resume it. Explicit header
Pause/Stop/Restore cancels that automatic resume. Track changes clear active
effects, live history and the old cue list. The rekordbox history/MIDI adapter
still has no reliable song position or playback-rate telemetry: use live
detection or the explicit manual clock. No playhead is inferred from its history.

## Effects, live detection and timing

All five presets use the album palette and respect the existing brightness
ceilings. Enabled event mode uses 65% of those ceilings as its baseline, leaving
headroom for an impact. CINEMATIC_IMPACT has a coordinated attack/hold/decay and
optional known-cue pre-dimming. DROP_RELEASE adds shallow rhythmic brightness
and palette motion. BUILD_RAMP rises gradually. FINAL_RESOLUTION and OUTRO_FADE
settle into the configured resting brightness with the current album palette;
add a manual FINAL_RESOLUTION cue after a final impact when that is desired.
Reduced motion removes rhythmic motion and lengthens attacks. No new preset
strobes. Existing Pulse/Flash modes remain separately selected dashboard looks.

Effects use monotonic time anchors, not cumulative timer steps. Pause freezes an
active envelope; resume shifts its anchor. A seek or track change cancels it;
backward seeks/loops explicitly rearm cues for another pass. Suspended-process
backlogs over a second are skipped. Color event output is capped at 20 fps through
the existing Entertainment implementation. White lamps keep their separate
worker and use at least a one-second attack, two-second hold and four-second
decay. REST writes remain at most once per bulb every two seconds with at least
one-second transitions; a REST effect is a slow interpretation of the envelope.
Final decays can finish before the existing silence restoration takes over.

Live analysis is opt-in, reuses the existing explicitly opened input, and runs
at 4 Hz outside the callback. It uses a rolling median/MAD baseline, sustained
confirmation, hysteresis, per-type cooldown and duplicate suppression. Enable
**Detect live candidates**, then use **Listen** in Audio setup. Candidates list
their input timestamp, estimated confidence, feature evidence and total detection
delay. **Trigger live effects automatically** is a separate opt-in and requires
the lighting engine to be running. The header Pause/Stop stops output; pausing
only the cue clock does not disable separately enabled live triggers. Accepted
scheduled cues suppress equivalent live triggers near their boundaries.

The new live path supersedes the older drop-accent response while automatic live
event effects are enabled. Drops need at least 0.75 seconds of sustained support;
live final impacts need an observed tail and remain provisional (a breakdown
could precede another section). A live ending can be a pause or a gap. These are
reactive detections, never predictions. Confidence values are heuristic scores,
not calibrated probabilities. Worker processing time excludes the confirmation
window; each candidate's detection delay includes confirmation and input age.

**Advance known cues** compensates estimated color output latency (0–1000 ms).
It cannot make a live detector anticipate unknown audio. Live effects begin on
confirmation and can optionally wait for the next existing beat-clock boundary
or an additional positive delay. Calibrate by auditioning against audible
playback and adjusting the known-cue advance; record separate observed color and
white response. There is no automatic physical latency measurement. Streaming
cadence, network latency, browser telemetry, Music scripting and slow REST lamps
all limit synchronization accuracy. Screen previews illustrate envelopes, not
measured light output or colorimetry.

## Exact reference recording workflow

No recording of **Swedish House Mafia — Don't You Worry Child (extended version)**
was supplied with this task. No copyrighted audio was downloaded and no event
time for that recording has been invented. The following workflow requires the
user's authorized copy of the exact desired edition.

1. Copy `docs/reference-annotations.template.json` to a local working file.
   Record the actual release/mix identifier, exact decoded WAV duration and
   SHA-256 (for example, `shasum -a 256 '/path/to/authorized.wav'`). A conversion
   to WAV can change padding; annotate and hash the same WAV that is analyzed.
2. Choose that WAV in Event Detection, use a distinct track identifier, and
   listen before looking at automatic candidates. Locate the actual final attack
   with the player; **Use playhead time**, then refine the cue by ear. Record
   `position_seconds` in the annotation JSON, with your uncertainty/tolerance.
   Annotate other requested section types too. If there is no distinct hit,
   remove that event rather than guessing a timestamp.
3. Analyze and review the file. Use the comparison helper for a repeatable report:

   ```sh
   .venv/bin/python examples/check_reference_events.py '/path/to/authorized.wav' '/path/to/annotations.json'
   ```

   It verifies the WAV hash and duration, rejects missing timestamps, and reports
   signed timing errors, matched annotations and unmatched candidates. It does
   not open capture or a bridge. Keep the manual annotation before reanalysis.
4. Audition the accepted cue in mock mode first, then perform the opt-in hardware
   check below. Record cue error separately from visible lamp latency.

## Validation and remaining checks

Automated tests cover the eight requested synthetic scenarios, versioned cue
persistence/overrides, API review/cache/preview, deterministic envelopes,
pause/seek/rate/loop/latency boundaries, stream frame caps, slow white-only payloads,
palette preservation and the existing app regression suite. The Node dashboard
harness checks editor/acceptance/transport/preview handlers with a simulated DOM;
it does not constitute a browser visual rendering test.

This change has **not** been validated with real Hue hardware, physical DTLS
streaming, live Music scripting, microphone/BlackHole capture, or the reference
recording. Earlier README hardware notes describe earlier code only. To validate
this implementation on hardware: use conservative brightness and reduced motion,
audition in Static, inspect all four bulbs, compare REST versus Entertainment,
measure color/white latency, pause/seek/loop, stop listening, verify silence
restoration and area release, and finish with Restore. Verify Music position
following against audible playback independently. Real-song false positives and
missed events still require a manually annotated evaluation set.
