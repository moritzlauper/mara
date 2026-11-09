# MixMatch

A mixing agent for Ableton Live. It listens to your project while it plays,
compares it with reference tracks and adjusts the mix step by step: volumes,
EQ, width, bass in mono, compression and the limiter on the master. It works
in rounds (listen, compare, decide, adjust, check) and undoes every round that
made things worse.

A run is complete by default:

1. It plays the song once and notes where each channel plays and how loud.
2. It matches the master to the references at the song's loudest 16 bars.
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
priority, from the track name, the devices on it and how it sounds. The result
is written to `roles.json`; correct it there if needed, e.g.

```json
{ "Synth 3": { "role": "lead_synth", "priority": 4, "note": "the hook, keep it upfront" } }
```

### References

Give the references as audio files (WAV, AIFF, FLAC, MP3). They are measured
the same way as your mix, using their loudest part (drop or chorus). You can
give several: the target is their average, and where they disagree with each
other the tolerance gets wider, so no single song dictates the sound.

### Beats without vocals

If your project has no vocal channel (or with `--mode beat`), MixMatch compares
your beat with the references **without their vocals**:

* With Demucs installed (`pip install "mixmatch[stems]"`), each reference is
  split into vocals and instrumental once (cached). Tonal balance, width and
  dynamics come from the instrumental.
* The loudness target comes from the full songs, since beats are usually
  mastered as loud as songs. Use `--loudness-from instrumental` to change that.
* Without Demucs, the vocal range (200 Hz–5 kHz) only counts where your beat is
  louder than the references, so the missing vocals are not "fixed" by pushing
  instruments into that range.

### Safety

* Small steps only: at most ±2 dB volume, ±3 dB EQ, +2 dB limiter per round.
* The limiter gain stops at +5 dB, a clipper in front of it at +4 dB; past
  that, the loudness gap is accepted.
* Installed plugins are used where they help: FabFilter Pro-L 2 as the master
  limiter, Classic Clipper before it, Pro-C 2 and others when Claude asks for
  them. Plugins that show Live no parameters (Pro-Q 3) cannot be controlled.
* Automated parameters (filter sweeps, build-ups) are never touched.
* Every round is measured; a round that makes the distance to the target worse
  is undone, and Claude sees that in the history.
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
   export ANTHROPIC_API_KEY=sk-ant-…  # without a key a rule-based planner is used
   # Planner model: Claude Haiku 5.5 by default; MIXMATCH_MODEL=claude-opus-5-5 for another one
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

Reliable: loudness like the references, tonal balance of the master, bass in
mono, clipping, kick/bass separation, rough balance between channels. Harder:
compression settings (attack and release are only approximated from the
measurements) and taste decisions. MixMatch measures; it does not hear. Claude
plans from the numbers, the channel roles and the history of earlier rounds,
and explains each step. The last bit of character and depth is still yours.

## Development

```bash
pip install -e ".[dev]"
pytest                       # simulator, executor, reference analysis, the loop with a fake Claude
python3 device/build_tap.py  # rebuild the tap device after changing it
```

`mixmatch/sim.py` contains a small fake Live (tracks, mixer, EQ Eight, Utility,
Limiter, browser) with a crude sound model, enough to test the loop end to end.
