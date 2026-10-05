# Architecture

## Processes

```
control.vbs ─► gui/control/app.py (the control panel: pywebview window, page gui/control/web/, Python side api.py)
```

```
start.vbs ─► engine/main.py (main window, Tk in the main thread; dispatcher in a background thread)
               └─► engine/batch.py   one process per configN in "run", single-instance per config (named mutex)
                     └─► engine/dataset.py   one per folder: prepares test-dataN, opens gui/window.py, runs:
                           └─► engine/run.py   the actual stage chain for that dataset
```

All children belong to a Windows job object owned by `main.py`. Closing the main window stops every batch,
pipeline and dataset window, and each child also watches the parent's PID as a fallback.

- **Configs are snapshotted at start.** The batches read a copy under `logs/config-snapshots/<time>`, so edits during a run
  take effect only on the next start.
- **`configs/live.json` is the exception.** It holds values like free CPU cores, failover thresholds and broken-file
  limits. The dispatcher validates every save and publishes accepted values to `logs/live_effective.json`, and running
  pipelines read them on the fly.

## Dataset state

`test-dataN/` keeps everything a dataset needs to resume:

| Path | Content |
|---|---|
| `in/` | copies of the source images (the source folder is never modified); a second-pass `dataN-other` is **moved** here and its empty source folder removed |
| `work/chunk_*/_state.json` | per-file state: which stages checked it, their answers and confidence, errors, `broken` mark |
| `cache/<model>.json` | raw model answers keyed by file identity, so a restart or key switch never re-asks |
| `out/` | `N. <stage>/Title/Character` copies per stage, `0. ALL` (final), `Other`, `status_snapshot.json` |
| `logs/pipeline_events.jsonl` | the event stream the dataset window renders |

A dataset is **completed** when the snapshot says `completed` and `work/` is gone. Then it is **merged** when
`tools/add/add.py` writes `DONE.txt`. Completed folders get a green check-mark folder icon via `desktop.ini`
(`engine/marks.py`), so the state is visible in Explorer without renaming anything.

## Stage selection

Each stage receives only unresolved files. Two optional fields narrow that set further:

- `"from": 0.10` — the previous stage's confidence must be at least this value;
- `"where": "[AI-GF] and [AI-K3]"` — a boolean expression over earlier stages. `[X]` means stage X checked the file,
  did not identify it, and its confidence was at least this stage's `from`.

Expressions are parsed by a small recursive-descent parser (`engine/config.py: parse_where`) and validated before
anything runs.

## API client and failover (`engine/client.py`)

A stage's `"api"` is an ordered list of `provider/key` routes (optionally with a different model per route).
`ApiClient` keeps a `RouteState` per route and reacts to each kind of failure:

| Situation | Reaction |
|---|---|
| HTTP 402 or a quota message | Sleep `failover_quota_recheck_minutes`; the same file goes to the next route |
| `failover_errors` failures in a row, or errors for `failover_minutes` without success | Sleep `failover_recheck_minutes`; after waking, one probe request either restores the route or puts it back to sleep |
| Empty or unparsable answer | Counted against the **file**, not the route. If 5 of the last 10 answers are garbage, the model "rattles" and the route sleeps |
| The model says there is no image (the gateway dropped it) | A route failure, detected by `NO_IMAGE_RE`. Such answers are never cached |
| All routes asleep | The stage waits for the earliest wake-up instead of marking files broken |
| All routes asleep for `fallback.after` minutes | The stage switches to its `"fallback"` model and routes for the rest of the run; the event renames the stage in the table |

Every switch is logged with its reason to the dataset log and the main journal. The "all asleep" timer is reset only
by a successful answer, not by a probe of a waking route — otherwise a recheck interval equal to `after` would keep
the fallback from ever firing.

## Manual stage controls

The dataset window writes requests to its own `logs/control.json`; the pipeline checks them between files, while
waiting for sleeping routes (every 2 s) and during a pause:

| Button | Request | Effect |
|---|---|---|
| ← | `back` | `StageBack`: return to the previous stage, which processes only files untouched by it **and** by the current stage (`ds.exclude_checked_by`); then the current stage resumes. Disabled when the previous stage is complete |
| → | `skip` | `StageSkipped`: the current stage is dropped for this run |
| ↻ | `retry` | All sleeping routes of the stage are woken and probed right away |
| ↔ | `switch` | All routes of the current provider sleep, the other providers' routes are woken and tried at once. Disabled when the stage has one provider (`layout.json` → `providers`) |

## Second pass (`dataN-other`)

`tools/other.py` moves every image from the collection's "Other" (recursively) into new `dataN-other` folders.
`prepare.py` builds their batches from `configs/other/template.json`: at least five AI stages, no `from`/`where`,
`"batches": 4` and `"rotate_ai": true` — each batch starts the AI chain at a different stage, and providers are
interleaved in the template, so the four batches load different models and gateways at any moment.
`"previous": "keep"` preserves answers of stages that `prepare` had to disable.

Neighbour stages of such a dataset use `engine/global_order.py`: the global sequence is every ordinary
`test-dataN` in order, each in its `manifest.json` order; answers come from `out/0. ALL`; a second-pass file is
found in that sequence by SHA-256 and counts as unresolved until a stage of its own dataset identifies it.

## Broken files

A file is marked broken and routed to "Other" in these cases:

- it does not open as an image;
- a stage failed on it in `broken_after_runs` consecutive runs, while other files passed;
- the pipeline process crashed on it `broken_after_crashes` times (`logs/current.json` + `crashes.json`);
- it still has a stage error at the end of a run, so that the dataset can always finish.

## Windows (`engine/winlayout.py`)

Shared state lives in `logs/windows.json` and is guarded by a named mutex.

- **Setups 1–3** set the window size (pixels, percent of the screen, or `auto` from column widths) and the fonts.
- **Arrangement modes** decide placement:
  - `left-right`: fill the left column top to bottom, then the right one;
  - `center`: a single centred column.
  - On the second round, windows never cover slot 0 (the main window), unless only one window fits in a column.
- **Buttons in the main window** bump a generation counter, and every window applies the change. Moving or resizing
  a window by hand greys out the active setup or mode button until it is pressed again.

## Control panel (`gui/control/`)

A pywebview window (Edge WebView2) showing a plain HTML/CSS/JS page — no framework, no build step — with pages
Overview, Pipelines, Second pass, Configs, Names, API & keys, Tools and a "Tasks" dock. The page calls Python through
`window.pywebview.api` (`gui/control/api.py`) and polls once a second for task output and pipeline state. The Python
side reuses the engine rather than duplicating it:

- **Tasks.** Every tool is started as a subprocess with `ANIME_SORT_GUI=1`. `tools/console.py` then prints questions as
  a marker line `\x1eASK\t<question>\t<options>`; the panel shows them as buttons and writes the answer to stdin, so
  the same tools work both in a console and in the panel. Tools that rewrite the collection run one at a time.
- **Pipelines.** Started exactly like `start.vbs` (via `explorer.exe`, not as a child). "Stop all" writes
  `logs/main_control.json`, which the dispatcher picks up and closes the main window gracefully.
- **Configs.** Batch templates are edited as forms (stage table with drag-and-drop order, stage panel, draggable API
  lists, fallback) or as raw JSON. "Check" builds a batch with `prepare.build_configs` (all routes assumed working) and validates it with
  `engine/config.check_one`, the same code the pipelines run. `"//"` comment keys survive editing.
- **Overview.** Dataset states come from the same code as the main window (`gui/stats.py`); thumbnails are 96 px JPEGs
  cached in `logs/thumbs/`; KPI sparklines come from `logs/history.json` (one point per hour).
- **Secrets.** Key values never cross into the page: the API reports only "present / empty"; pasting a key writes the
  file on the Python side; removing a key sends it to the Recycle Bin.
- **Modes.** `tools/modes.py` knows where each mode keeps its template (`configs/main/`, `configs/other/`) and migrates
  the old flat layout; `prepare.py --mode main|other` plans only that mode's folders and saves probe results to
  `logs/probes.json` for the overview.

## Collection tools (`tools/`)

| Tool | Role |
|---|---|
| `prepare.py` | Probes every route of the templates with a tiny image or text request, plans N batches (≤ `max_batches`, ≤ working keys of the first AI stage, or exactly `batches`), rotates the primary key — and with `rotate_ai` the AI stage order — per batch. Non-working routes stay at the end of the list; a stage whose main model has no working route but whose fallback has one switches to the fallback after a minute |
| `agent/config_agent.py` | OpenRouter-only agent: surveys every key × model (a key without balance is cut after its first answer) and OpenRouter credits, asks an LLM for new templates under explicit rules, builds and validates them like real configs, feeds errors back up to twice, and asks before writing |
| `add/add.py` | Merges completed datasets into the collection. SHA-256 de-duplication (the collection is hashed once per run), journal `folders.json` |
| `fix_name/` | Applies `names.json` rename rules (`fix_name.py`) and keeps the file grouped by target title (`regroup.py`) |
| `agent/names_agent.py` | OpenRouter-only LLM agent with its own key: proposes rules for duplicate titles and characters, validates them (source exists, no chains, no conflicts) and asks before writing |
| `cleanup/small_titles.py`, `other.py` | Titles with ≤ 15 images are dissolved: a character (full name) found in a bigger title moves into the biggest such title, the rest goes to "Other". Everything in "Other" can go to the second pass as `dataN-other` |
| `watch/` | Background watchers that keep folders numbered by size; they pause while bulk tools run (`tools/.busy`) |
