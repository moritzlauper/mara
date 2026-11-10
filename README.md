# MixMatch

A mixing agent for Ableton Live. It listens to your project while it plays,
compares it with reference tracks and adjusts the mix step by step: volumes,
EQ, width, bass in mono, compression and the limiter on the master. It works
in rounds (listen, compare, decide, adjust, check) and undoes every round that
made things worse.

A run is complete by default:

1. It plays the song once and notes where each channel plays and how loud.
2. It diagnoses the mix at the song's loudest 16 bars, using the references
   as an important direction while preserving the track's own character.
3. It goes through every channel, group and return, most important first,
   each looped where it plays: level, EQ, masking, width, dynamics, sends, or
   nothing if it sits well.
   Loudness does not count here, a quiet part need not sound like the drop.
4. It checks the master once more at the loudest part.

`--quick` does only step 2, `--minutes` sets an upper time limit.

Nothing has to be exported. Everything happens in the open Live set.

> Status: first version. It is tested against a built-in simulator of Live,
> not yet inside a real Live set. Expect rough edges on the first run.

## How it works

```
Ableton Live                                     MixMatch app (Python, on your Mac)
──────────────────────────────                   ───────────────────────────────────────
«MixMatch Tap» on each channel ── measurements ─▶ loudness, spectrum, width, masking
and on the master  (UDP 9878)                    compare with the references
                                                 Claude decides 1–4 small steps
«MixMatch» remote script ◀────── parameters ──── apply, wait, measure again
(TCP 9877)                                       worse? undo
```

* **Tap** (`device/MixMatch Tap.amxd`): a Max for Live audio effect. It lets
  the sound through unchanged and sends 10 measurements per second: 31
  third-octave bands, mid/side, side below 150 Hz, peaks.
* **Remote script** (`remote_script/MixMatch`): exposes the set to the app:
  tracks, devices, parameters, cue points, transport. It makes no decisions.
* **App** (`mixmatch/`): understands what each channel is, measures, compares,
  lets Claude plan, applies the steps in small increments and checks them.

### Understanding the channels

At the start every channel gets a role (kick, bass, lead vocal, pad, …) and a
priority, from the track name, instrument/sample names inside racks, played MIDI
notes and how it sounds. Drum racks are identified from their actually played
pads: a rack playing only DS Kick is a kick, only DS Cymbal is cymbals; a mixed
kit can be a drums bus. A Drum Buss effect does not define the instrument. The result
is written to `roles.json`; correct it there if needed, e.g.

```json
{ "Synth 3": { "role": "lead_synth", "priority": 4, "note": "the hook, keep it upfront" } }
```

When continuing a stopped run, corrected roles are rechecked even if those
channels were already processed. Unchanged channels keep their place in the run.

### Instruments masking each other

Masking is measured from simultaneous tap frames (about 100 ms apart), with
the channel and group faders accounted for. Alternating notes, silence and
quiet tails do not count as collisions. The detector protects the channel
with the higher role priority, including when it is quieter than its masker.
It reports the strongest frequency regions, simultaneous activity and any
observed drop in the background level during foreground activity. That drop
is evidence of existing separation, not proof of a particular sidechain setup.
All detected pairs count in the score; channel passes see their own pairs first.

For repeated intermittent collisions, the planner can use `duck`: a dedicated
native Compressor on the background, triggered by the foreground's Post FX
signal. The detector uses RMS, a 2 ms attack, 50–250 ms release and no makeup
gain. Its parallel dry/wet mix bounds the attenuation to at most 3 dB, even
when a trigger is louder than the measured window. Existing disabled or
sidechain compressors are left alone; duckers are not stacked. Failed setup
removes the new device, and a rejected mixing round removes it too. The
before/after switch bypasses it for the original mix.

Sustained conflicts use small static EQ cuts on the lower-priority channel;
the rule planner now handles other instrument pairs as well as kick/bass and
limits cumulative masking cuts to about 4 dB. The AI receives the same guidance.
This is broadband sidechain ducking, not frequency-selective dynamic EQ.

The detector remains a coarse heuristic: 31 bands at 10 Hz cannot establish
perceptual masking or resolve individual transients. The UI therefore calls
it *potential masking*. Missing or poorly aligned tap data never establishes
a collision, and a round cannot claim improvement by losing the observations
of a previously detected pair.

Sidechain creation requires the updated MixMatch remote script in Live
(reinstall using the setup steps below and reload Live). Older scripts still
use temporal detection and EQ correction, but do not offer the `duck` action.

### References

Give the references as audio files (WAV, AIFF, FLAC, MP3). They are measured
the same way as your mix, using their loudest part (drop or chorus). You can
give several: the target is their average, and where they disagree with each
other the tolerance gets wider, so no single song dictates the sound.

Reference tracks can also live inside the Live set: a track with «ref» or
«reference» in its name (e.g. «REF Wish You Well») counts as a reference as
soon as an audio clip is on it. MixMatch reads the clip's file directly, so the
track can stay muted, and it never mixes that track.

### Beats without vocals

If your project has no audible vocal channel (or with `--mode beat`), MixMatch
uses beat mode while comparing against the full reference songs.

Muted vocal tracks, including tracks inside muted groups, do not select song
mode and are skipped in the channel pass. Auto mode also switches to beat when
the full song scan proves all vocal channels silent. On resume, track mutes are
checked again; `--mode song` and `--mode beat` remain explicit overrides.

The vocal range (200 Hz–5 kHz) may sit up to 4 dB below the references, leaving
space for vocals without pushing instruments up to imitate a voice. Level is
matched using the bands outside that range; loudness and dynamics still come
from the full songs.

### Safety

* Small steps only: at most ±2 dB volume, ±3 dB EQ, +2 dB limiter per round.
* The limiter gain stops at +5 dB, a clipper in front of it at +4 dB; past
  that, the loudness gap is accepted.
* Installed plugins are used where they help: FabFilter Pro-L 2 as the master
  limiter, Classic Clipper before it, Pro-C 2 and others when Claude asks for
  them. Plugins that show Live no parameters (Pro-Q 3) cannot be controlled.
* Automated parameters (filter sweeps, build-ups) are never touched.
* Every round is measured against its stated purpose. A verified reduction in
  masking, peak overload or stereo bass can justify a small reference setback.
  Moves with no verified benefit, lost layers, excessive dynamics loss or too
  much spectral drift are undone; Claude sees the reason in the history.
* Touch a fader or knob yourself and that parameter is locked for the rest of
  the run.
* The limiter ceiling is fixed at −1 dBFS.
* Every round is logged in `runs/<date-time>.jsonl`.
* `--assist` asks before every round.

## Setup (Mac, Live Suite)

1. **App**

   ```bash
   git clone <this repo> && cd mixmatch
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[stems]"          # without [stems] if you never mix beats
   # Planner: with Claude Code installed and logged in, MixMatch plans through it on your
   # subscription (Opus for the master, Haiku for the channels). Otherwise it uses an API key:
   export ANTHROPIC_API_KEY=sk-ant-…  # Claude Haiku 5.5; MIXMATCH_MODEL for another model
   # Without either, a rule-based planner is used. Choose with --planner auto|claude-code|api|rules.
   ```

2. **Remote script**: copy `remote_script/MixMatch` to
   `~/Music/Ableton/User Library/Remote Scripts/`, restart Live, then in
   Settings → Link, Tempo & MIDI pick «MixMatch» as a control surface (input and
   output: None). The status bar shows «MixMatch listening on port 9877».

3. **Taps**: copy `device/MixMatch Tap.amxd` and `device/mixmatch_tap.js`
   into a folder of your User Library (e.g. `User Library/MixMatch`). Then
   `mixmatch taps` puts the tap as the **last** device on every channel, group,
   return and on the master (run it again after adding tracks). You can also
   drag it onto the channels by hand. If Live
   does not open the .amxd, open `device/MixMatch Tap.maxpat` in Max (create an
   empty Max Audio Effect in Live, edit, paste the patch) and save it as a
   device.

4. **Sections** (optional): set locators (cue points) at the starts of the
   parts, e.g. Intro, Drop, Break, Drop 2, End. MixMatch works through them one
   by one. Without locators it uses the current loop. With `--focus loud` it
   plays the range once, finds the loudest 16 bars (`--focus-bars`) at the
   master and works only there, the same way the references are measured at
   their loudest part.

## Use

```bash
mixmatch refs ~/refs/song1.wav ~/refs/song2.mp3       # analyse references once (and split vocals)
mixmatch taps                                         # put the tap on every channel and the master
mixmatch check                                        # connection, taps, roles; writes roles.json
mixmatch run --ref ~/refs/                            # complete run (folder = all audio files in it)
mixmatch run --ref ~/refs/ --quick                    # only the master against the references
mixmatch run --ref a.wav --ref b.wav --mode beat      # beat without vocals
mixmatch run --ref a.wav --focus loud                 # work on the loudest 16 bars (drop, chorus) only
mixmatch run --ref a.wav --assist                     # suggest, ask before each round
mixmatch demo                                         # try the loop on the built-in simulator
```

The song plays while MixMatch works; each round needs a few bars of playback,
so a full mix takes roughly 5 to 20 minutes.

## What to expect

The Before/After controls play recorded 16-bar excerpts in the browser, with
Play/Pause, seeking and volume. Before stays available while Live mixes
silently; After appears when that run has an after snapshot and audio recording.
Equal loudness is on by default once both excerpts are available. It measures
integrated loudness with pyloudnorm (BS.1770), then attenuates the louder excerpt
to the quieter one for playback, including blind comparison. The original WAVs,
mix parameters, waveform dynamics and the player's volume setting are preserved.
Both use the same passage. Continued runs carry the original Before recording
forward. Blind random also plays these files without changing the Live set.
Preview recording temporarily disconnects Live's hardware output rather than
muting the recorded signal, then restores the mix, routing and transport.

MARA uses a mix-first policy (`mix-first-v1`). Claude receives a six-region
spectrum overview with the strongest source channels, roles, user notes and
measured conflicts. Each round states one measurable priority, its evidence,
what character to preserve and how the reference is relevant. An arrangement
or timbre difference is not automatically a defect, and a remaining loudness
gap does not require exhausting the limiter or clipper.

Fresh measurements verify the chosen benefit. Reference-only moves must still
improve the overall measurement; mix-health improvements may trade a small
amount of reference proximity. Fixed passage baselines bound cumulative tonal,
level and dynamics changes across channel passes and checkpoint resumes. An
effect is not bypassed just because the reference score improves without it.
The distance display remains a measurement, not a verdict on musical quality.
These protections are heuristics; PLR and spectral energy cannot prove how a
change sounds. Existing stopped runs load this policy on continuation, without
discarding the retained mix changes or rescanning the song.

Reliable: loudness like the references, tonal balance of the master, bass in
mono, clipping, kick/bass separation, rough balance between channels. Harder:
compression settings (attack and release are only approximated from the
measurements) and taste decisions. MixMatch measures; it does not hear. Claude
plans from the numbers, the channel roles and the history of earlier rounds,
and explains each step. The last bit of character and depth is still yours.

## Finding references

```bash
pip install -e ".[style]"                                  # CLAP, runs locally
mixmatch suggest-refs my-beat.wav --note "PS2-style DnB"  # style, tempo, references to buy
mixmatch suggest-refs my-beat.wav --library               # also: similar songs in ~/Music
```

MixMatch listens to an export of your track with CLAP, an audio model that
places music and descriptions in the same space (LAION's music checkpoint,
which MixMatch sets up by itself on first use through uv, about 2.5 GB; the
general CLAP model when that is not possible): it names the closest styles,
estimates the tempo and, with `--library`, ranks the songs in a folder by how
close they sound (cached, so a library is listened to once). Claude then
suggests real tracks to mix against, with Bandcamp and Beatport search links.
Buy the ones you like and put the WAVs into `references/`; MixMatch does not
download music itself.

### Find suggestions, then choose

`mixmatch run` without `--ref` finds its references itself (`--auto-ref`):
it records the loudest 16 bars from Live's master (a temporary track with
input «Resampling», monitoring off, deleted afterwards), names the style with
CLAP, and picks the closest mastered songs from `references/` and your
library. It also writes a list of real tracks to get, with Bandcamp links
(`runs/…-buy.md`). It saves the song scan and pauses before making mix changes.
Choose your tracks in the interface, or continue with `mixmatch run --continue --ref chosen.wav --no-suggest`.
`--jamendo` adds Creative Commons tracks from Jamendo (`JAMENDO_CLIENT_ID`
in `env.local`, which MixMatch reads by itself).

## Interface

```bash
mixmatch ui        # opens http://127.0.0.1:8765
```

The top-level `references/` is a shared reference library, detected automatically
even when a run works inside a song folder. Its files appear under References,
where «Use this reference» selects one for the current song. They also take part
in reference discovery. Adding a file to the library does not start a mix.

Each track has its own folder `songs/<track>/` with its selected references, roles,
description and runs. «New track» starts an empty one for the set that is open
in Live; MARA notices when Live has another set open and offers to switch or
start a new track. Before a run, a checklist shows whether Live is connected,
whether every channel has a tap, the references and the description.
References of earlier tracks can be picked again under References → Used
before. On the first start, `runs/`, `roles.json` and `mixmatch.json` move into
the first track; the shared `references/` stays in place.

Choose «I already have reference tracks» to add your tracks and skip discovery,
or «Find reference suggestions for me» to prepare the song and review suggestions
before mixing. «Continue mixing with references» resumes a paused mix from its
checkpoint. Silent mode mutes Main before starting playback. At the end it stops
playback and waits for measured effect tails to fade before restoring the volume.
If silence cannot be confirmed within eight seconds, Main stays muted.

For a blind before/after comparison after the run, click **Blind random · B**
or press **B** with your eyes closed. A focused blind button also responds to
Space. Every press independently chooses the original or processed mix;
the same version can occur twice. Neither the active version nor a parameter
count is shown until you click **Reveal**. Both snapshots of the latest run
must exist, and comparison is disabled while MARA is mixing.

Start, stop and resume runs, follow what MixMatch does and why (distance to
the target and where it comes from, every round kept or undone), see key,
chords and style of the song, the references used and the ones to get (with
Spotify and Bandcamp links), which effects were removed or bypassed, and edit
the channel roles.

## Development

```bash
pip install -e ".[dev]"
pytest                       # simulator, executor, reference analysis, the loop with a fake Claude
python3 device/build_tap.py  # rebuild the tap device after changing it
```

`mixmatch/sim.py` contains a small fake Live (tracks, mixer, EQ Eight, Utility,
Limiter, browser) with a crude sound model, enough to test the loop end to end.
