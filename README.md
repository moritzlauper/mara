# MARA · Reference-guided mixing for Ableton Live

MARA helps you compare an Ableton Live set with reference tracks, spot practical mix problems, and make small, reviewable adjustments. It measures your channels, works through the set in passes, and keeps only changes that improve the selected passage without breaking its safeguards.

It is a mixing assistant, not an automatic mastering button: you choose the references, set the goal, review roles, and decide whether the result works musically.

![MARA's Mix view with reference comparison, mix history, listening controls, and latest changes.](docs/images/mara-teaser.png)

## [Download MARA → GitHub Releases](https://github.com/moritzlauper/mara/releases)

> **Early release — v0.1.0.** MARA is intended for testing on copies of Live sets. Save your project before starting, listen critically, and keep the changes you like in Ableton as usual.

## What using it looks like

1. Open your Live set and connect MARA once.
2. Add one or more finished reference tracks.
3. Tell MARA what to protect or improve — for example, “keep the Rhodes warm; separate kick and sub; leave space for a vocal.”
4. Let it analyse the relevant song section, then check the detected instrument roles.
5. Watch the mix pass, pause at safe checkpoints if needed, and listen to the loudness-matched Before/After result.
6. Inspect every kept or reverted change. Keep the result, or restore the saved parameter state.

The app records why it kept a change, such as a reduced kick/sub conflict or a closer reference balance. It does not hide changes behind a single score.

## Before you start

- Ableton Live with Max for Live.
- A saved Live set.
- One or more reference files (WAV, AIFF, FLAC, MP3, M4A, or AAC work).
- A backup or duplicate of the set you want to try.

No model account is required: **Built-in rules** handles broad balance, limiter adjustment, mono bass, and measured masking. Optional planner providers can add suggestions based on the same measured project context.

## First-time setup

1. Install and open MARA from the [latest release](https://github.com/moritzlauper/mara/releases).
2. In **Project & goal**, choose **Set up the connection**, select your Ableton User Library, and choose **Install connection**.
3. Restart Ableton Live. In **Settings → Link/Tempo/MIDI → Control Surface**, select **MixMatch**; set both Input and Output to **None**.
4. Return to MARA and choose **Set taps**. It places a transparent measurement device at the end of each relevant track, group, return, and Main chain.

Run **Set taps** again after adding tracks. The tap passes audio unchanged and measures the signal before the fader.

## A typical session

### 1. Set the goal

Choose whether the project is a beat awaiting vocals, an instrumental, or a song with vocals. Add a short goal in your own words. That is where you set priorities — for example, preserving drum transients matters more than matching a reference exactly.

![Project & goal: connection, tap coverage, project mode, and a specific mixing goal.](docs/images/01-project.png)

### 2. Choose references

Drag in one or more tracks that represent the sound and arrangement you are aiming for. A full track is more useful than a short preview. MARA builds a target from all selected references, so choose tracks that agree with each other.

![References: selected tracks, import, and library access.](docs/images/02-references.png)

### 3. Check the analysis

MARA scans the chosen song range, finds a useful passage, and asks you to review channel roles and priorities. Correct anything that is wrong before continuing — a bass, lead, and pad can need different treatment even when their frequency content overlaps.

![Analysis: role review and the recorded original preview.](docs/images/03-analysis.png)

### 4. Follow the mix pass

The app works in small phases: overall balance, channels and groups, effect checks, then a final check. It measures the affected passage after an adjustment. An attempt that does not pass the checks is reverted; accepted changes remain normal editable Live parameters.

You can pause at a safe checkpoint, alter the goal for subsequent work, or unmute the current signal to listen. The UI starts with Live output muted to avoid unexpected playback while it analyses and records.

![Mix: progress, reference comparison, listening controls, and recent changes.](docs/images/04-mix.png)

### 5. Listen and decide

Compare Before and After at matched loudness, use blind playback if helpful, and expand the change list to see exactly what changed. **Restore original** prepares a guarded restore of MARA's saved parameter state; it does not replace a proper `.als` backup.

![Result: matched listening, retained changes, and remaining reference differences.](docs/images/05-result.png)

## What MARA can help with

- Compare tonal balance, loudness, dynamics, stereo width, low-end side energy, and measured masking with selected references.
- Work on kick/sub overlap, source balance, room for future vocals, and arrangement-dependent conflicts.
- Adjust exposed Live device parameters, mixer controls, and top-level rack macros; it can also use suitable existing effects or load available native effects/plugins.
- Keep a journal of accepted and rejected attempts, plus parameter snapshots and Before/After recordings.

MARA cannot judge musical taste for you. The measurements are decision support, and a lower value in the dashboard is not a universal quality score.

## Optional planner settings

The standard workflow works with **Built-in rules**, which sends no mix-planning request to an external model service. If you want additional contextual suggestions, select a provider under **Planner settings**. Available options include Claude Code, Anthropic, Gemini, and compatible Chat Completions endpoints; keys are stored in the operating system credential store.

Provider choice changes how a plan is proposed, not how Live is controlled: every adjustment still goes through the same bounded action and verification steps. Optional reference discovery and style suggestions may use additional local models or online services. You can skip them and select reference files manually.

## Limits worth knowing

- Use on a copy of an important set and listen to the result before committing it.
- MARA controls what Live exposes: tracks, groups, returns, Main, top-level devices, and rack macros. Deeply nested device parameters are not currently addressed.
- It evaluates the passages it measures. Sparse intros and dense drops should not be treated as the same target.
- A run can be paused and resumed, but Live integration and sound quality still need real-session validation.

## Run from source

For development, you need Python 3.10+, Ableton Live, and Max for Live:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mixmatch ui
```

Open [http://127.0.0.1:8765/workflow](http://127.0.0.1:8765/workflow). The desktop app opens this workflow directly.

Useful command-line commands for advanced users:

```bash
mixmatch taps                         # Insert measurement devices
mixmatch refs ~/refs/song1.wav        # Prepare a reference
mixmatch run --ref ~/refs/ --no-suggest
mixmatch run --continue --ref ~/refs/ --no-suggest
mixmatch ab before                    # Recall saved parameters in Live
mixmatch ab after
```

## Demo UI

To browse the interface without Ableton, a model account, or project data:

```bash
python3 docs/readme_demo.py
```

Open [the project screen](http://127.0.0.1:8876/workflow?demo=project). Change `demo=project` to `references`, `analysis`, `mix`, or `result` to view the rest of the workflow. This is read-only fictional data; it neither connects to Live nor processes audio.

## Project structure

The desktop interface is **MARA**. The Python package, CLI, and Ableton control surface are named **MixMatch**.

| Area | Starting point |
| --- | --- |
| Mixing loop and checks | [agent.py](mixmatch/agent.py), [mix_quality.py](mixmatch/mix_quality.py) |
| Reference analysis | [reference.py](mixmatch/reference.py) |
| Ableton connection and measurement tap | [Remote script](remote_script/MixMatch/commands.py), [Tap](device/MixMatch%20Tap.maxpat) |
| Interface | [app.html](mixmatch/static/app.html), [app.js](mixmatch/static/app.js) |
| Packaging | [desktop/](desktop/) |

## License and status

The project is under active development. Automated tests cover the simulator and individual components; they are not a substitute for compatibility checks in real Live sessions or for listening-based mix evaluation.
