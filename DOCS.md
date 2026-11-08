# VisualWeaver — Documentation

> A self-hosted AI chat application with Retrieval-Augmented Generation (RAG) powered by Redis vector search, supporting Ollama, Claude, OpenAI, Qwen, Mistral, Groq, and Gemini as LLM providers.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Core Concepts](#core-concepts)
4. [Chat Interface](#chat-interface)
5. [RAG Instances](#rag-instances)
6. [LLM Providers](#llm-providers)
7. [Ingesting Content](#ingesting-content)
8. [Multi-RAG Parallel Queries](#multi-rag-parallel-queries)
9. [Semantic Cache](#semantic-cache)
10. [Storage Connections](#storage-connections)
11. [Settings Reference](#settings-reference)
12. [Keyboard Shortcuts](#keyboard-shortcuts)
13. [Keeping an answer](#keeping-an-answer)
14. [Analytics](#analytics)
15. [API Reference](#api-reference)
16. [Architecture](#architecture)
17. [Configuration File](#configuration-file)
18. [Backup and Restore](#backup-and-restore)
19. [Running and Maintenance](#running-and-maintenance)
20. [Optional Dependencies](#optional-dependencies)
21. [Acknowledgments & License](#acknowledgments--license)

---

## Overview

VisualWeaver is a single-server, self-hosted application that lets you:

- Chat with Ollama, Claude, OpenAI, Qwen, Mistral, Groq, or Gemini
- Build named **RAG knowledge bases** from files, PDFs, web pages, and `llms.txt` manifests
- Store embeddings in **Redis** with HNSW vector indexing for fast semantic search
- Query **multiple RAG instances in parallel** across different Redis servers
- Cache repeated queries with a **semantic cache** that understands paraphrase similarity
- Render rich responses: Markdown and tables, syntax-highlighted code, LaTeX math (KaTeX), exact **function graphs** (math.js), **data charts** (Chart.js), **diagrams** (Mermaid, Graphviz), **geometric constructions** (JSXGraph), **maps** (Leaflet), **3-D plots** (Plotly), **molecules** (SmilesDrawer), **sheet music** (abcjs) and **inline SVG**
- Monitor retrieval quality per-instance with live analytics

<p align="center">
  <img src="screenshots/graph-and-formula.jpg" alt="LaTeX formulas, a comparison table, and a function graph rendered inline" width="49%">
  <img src="screenshots/notation-and-svg.jpg" alt="Sheet-music notation and an SVG geometry diagram rendered inline" width="49%">
</p>

---

## Quick Start

### Prerequisites

- Python 3.11 or 3.12 — `install.sh` uses the newest supported interpreter it finds, or the one given in `PYTHON=` (macOS also needs Homebrew + Xcode Command Line Tools)
- Redis 8 with the search/query engine — set up automatically by `install.sh` (or the `redis:8` service in Docker); no separate install needed
- At least one LLM backend: Ollama locally, or an API key for any supported cloud provider

### Install and run

```bash
./install.sh     # Python venv + a self-contained Redis 8 in ./.redis
./start.sh       # starts Redis, then the app
# → Open http://localhost:8420
```

Or with containers — one command chooses the storage (Redis, MongoDB or both), pulls the prebuilt multi-arch app image and the stores, starts them, waits until they are ready and prints the address:

```bash
./docker.sh up           # asks: redis | mongodb | both  (or ./docker.sh up mongodb)
./docker.sh status       # address, versions
./docker.sh stop         # stop later; everything is kept
```

Plain Compose works too: `COMPOSE_PROFILES=mongodb docker compose up -d` (unset = Redis, `mongodb`, `both`). To open it by a NAS or host name rather than `localhost` or an IP address, put that name in `VISUALWEAVER_ALLOWED_HOSTS` in `.env` (`./docker.sh up` records this machine's names itself). See the README's Docker section for every command.

On first launch a `config.json` is created (with defaults) in the per-platform data directory — `~/Library/Application Support/VisualWeaver` on macOS, `~/.local/share/visualweaver` on Linux — not in the repo. Configure Redis and your LLM provider via **Settings** before chatting.

### First run

Until a provider has a usable model, the welcome screen says so and offers the two places that fix it rather than suggestion prompts that cannot work. The message names the actual obstacle — Ollama running but with nothing pulled, or a hosted provider whose key is missing or was rejected.

While something is still missing, the welcome screen also shows a **Get started** checklist:

- **Storage** — whether the storage holding the conversations can be reached; if not, **Open Status** shows why.
- **Model** — whether the selected provider can answer: Ollama is running and has a chat model, or a hosted provider has its API key. If not, one button opens that provider's settings.
- **Knowledge** — **Add documents** opens **Settings → RAG** with the knowledge base the chat searches already chosen as the target (`default` on a new install) and the drop zone ready. It is ticked off once a knowledge base holds documents.

Each item says its state in words (done, needs attention, to do, waiting) as well as with its mark. The checklist disappears once all three are done; **✕** hides it for good in this browser.

With [sign-in](#sign-in-through-a-reverse-proxy-multiple-users) on, setting up a model and adding documents are for administrators. Someone who is not an administrator sees **Ask your administrator to set up a model** instead of a button when no model can answer, and **Ask your administrator to add documents** when no knowledge base holds documents yet; documents alone do not bring the checklist up for them.

### Status strip

A line under the message box shows, at all times:

- **Storage** — the storage holding the conversations: reachable or not (checked every 30 seconds).
- **Model** — the selected provider and model, or why it cannot answer (Ollama not running, no chat model, no API key). Ollama is checked every 30 seconds, a hosted provider every 5 minutes.
- **Server** — the connection to the VisualWeaver server: *connected*, *reconnecting…*, *unreachable* once it has been away for 15 seconds (it keeps retrying; **Retry now** tries at once), *offline* when the device has no network, or — with sign-in on — *signed out — reload to sign in* when the server refuses the page because the sign-in has ended. Signed out, the page stops reconnecting and offers **Reload**; a message waiting to be sent goes back into the message box. While the server is unreachable or the user is signed out, Storage and Model show as unknown.

Clicking **Storage** opens **Settings → Status**; clicking **Model** opens the provider's settings (for an administrator; for anyone else with sign-in on, Model only shows the state, with *ask your administrator to set up a model* when it cannot answer). Each item states its state in words as well as with a mark and a colour, and screen readers hear a short announcement when one changes. On a phone, an item that is fine shows only its mark and name; one that is not also says why.

A message sent while the connection is being re-established waits and is sent when it is back; the strip says so. If the server is not back within 10 seconds, the message returns to the message box and is not added to the conversation.

<p align="center">
  <img src="screenshots/features/first-run.png" alt="The welcome screen with no model configured, showing a setup card with Open provider settings and Check system status" width="76%">
</p>

---

## Core Concepts

### RAG Instance

A named, isolated knowledge base. Each instance has its own Redis search index (`rag:{name}:idx`) containing vectorised text chunks. You can have as many instances as you like, each storing different domain knowledge (e.g. `product-docs`, `support-kb`, `internal-wiki`).

### Active RAG

The instance currently selected in the topbar dropdown. All queries are routed to this instance unless **parallel mode** (🔀) is enabled.

### Enabled / Disabled

Each instance can be toggled on or off without deleting it. Disabled instances are excluded from both single-instance and parallel queries.

| State | Single-instance mode | Parallel 🔀 mode |
|---|---|---|
| **Active** (selected in dropdown) | ✅ Searched | ✅ Searched |
| **Enabled** (not active) | ❌ Skipped | ✅ Searched |
| **Disabled** | ❌ Skipped | ❌ Skipped |

### Hybrid Search

Every RAG query runs two searches simultaneously and merges them using **Reciprocal Rank Fusion (RRF)**:

1. **Vector KNN** — semantic similarity via HNSW cosine distance. Finds paraphrases and related concepts.
2. **BM25 full-text** — keyword matching using Redis's built-in text index. Finds exact and near-exact terms even when their cosine score is borderline.

RRF formula: each result receives `1/(60 + rank)` for every list it appears in. Chunks ranking highly in both lists get the strongest boost.

Hybrid search is on by default and can be toggled in **Settings → RAG**.

### Semantic Cache

Before querying the LLM, every question is checked against a cache of previous (question, answer) pairs using cosine similarity. If a sufficiently similar question has been answered before, the cached response is returned instantly — no LLM call needed. RAG chunks from the original query are also stored in the cache and re-served with cache hits.

---

## Chat Interface

### Sending messages

Type in the input box at the bottom. Press **Enter** to send, **Shift+Enter** for a new line.

### Stopping a response

Click the red **■** stop button that appears during streaming to abort the current response. The partial text already generated is kept in the chat.

### Image attachments

Click the 📎 button or drag an image directly onto the input box. Images are sent to the model as multi-modal content. The vision badge (👁 Vision) appears in the topbar when the selected model supports images.

### Voice input

Click the 🎤 microphone button to toggle speech-to-text. Transcribed text is added to the input field in the browser's language. Voice input uses the browser's own speech recognition, and some browsers send the audio to their vendor's online service to transcribe it, as the 🎤 tooltip says.

### Token count

The top bar shows the token size of **the conversation you are currently viewing**, split into three colour-coded pills: **input** (↑, the full prompt each turn sent — system prompt, re-sent history and retrieved context included), **output** (↓, the model's replies) and **total** (Σ). These are the provider's own billed counts, stored with each answer. A leading `~` means at least one turn had no reported counts and was estimated instead (≈ characters ÷ 4) — cache hits and stopped answers never reach a provider, so they are always estimated.

The pills reset when you switch conversations. For the running total across **every** conversation, see the Token Usage card in [Analytics](#analytics).

### Prompt templates

Select a system-prompt template from the dropdown left of the input box to change how the model answers (e.g. "Redis Expert", "ELI5", or any custom template you've created).

### Message actions

Hover any AI message to reveal its action bar.

**Output** — 📋 **Copy** (plain text), 📝 **MD** (raw markdown to the clipboard), ⬇ **.md** (download), 📄 **PDF** (print/save), ⌥ **Raw** (toggle the rendered answer against the markdown behind it).

**Actions**

| Button | Action |
|---|---|
| 👍 / 👎 | Rate the answer — stored as feedback with the question, the model and the sources used |
| 🔖 | Bookmark the message: it is listed under **Bookmarks** in the sidebar until the page is reloaded |
| ↻ | Regenerate; a version switcher appears so attempts can be compared |
| ⑂ | Fork the conversation here — a new session containing everything up to this answer |
| 💾 | [Keep this answer](#keeping-an-answer) — index it into a knowledge base so it outlives the cache and the conversation (administrators only, with sign-in on) |

### Question actions

Hover any of **your** earlier questions to reveal its action bar:
- 📋 **Copy** — copies the question text to the clipboard
- ↻ **Rerun** — asks it again (a close-enough answer may be served from the semantic cache)
- ↺ **Force rerun** — asks it again while bypassing the cache, for a fresh answer from the model

### Cached message actions

Messages served from the semantic cache have two additional buttons:
- **🗑 Uncache** — Removes this one answer from the cache. Future identical queries will go to the LLM instead. With sign-in on, only an answer cached for you can be removed (an administrator can remove any).
- **↺ Re-run fresh** — Re-sends the original query to the LLM, bypassing the cache. The new response automatically replaces the cached one.

### Rich content rendering

Answers are rendered inline: the model writes a short, declarative block and the browser draws it. The full set of supported types:

| Fence | Renders | You write | Engine |
|---|---|---|---|
| *(none)* | Markdown | Full GFM: headings, bold, tables, blockquotes, lists | marked |
| ` ```<language> ` | Code | Ordinary code | highlight.js |
| ` ```latex ` / ` ```math ` / `$…$` | Math formula | LaTeX | KaTeX |
| ` ```plot ` | **Function graph** (exact) — also parametric, polar, vector-field, contour and implicit forms; optional live parameter sliders | `y = a*sin(b*x)`, `x = -5 .. 5`, `param: a = 0.5 .. 3 (1)`; `x = cos(t)` + `y = sin(2*t)`; `r = 1 + cos(theta)`; `field: -y, x`; `contour: x^2 - y^2`; `implicit: x^2 + y^2 = 4` | math.js |
| ` ```chart ` | **Data chart** — bar, line, pie, doughnut, scatter, radar | Chart.js JSON | Chart.js |
| ` ```mermaid ` | **Diagram** — flowchart, sequence, class, state, ER, Gantt, mindmap, quadrant chart, XY chart, sankey, block, kanban | mermaid syntax | Mermaid |
| ` ```dot ` | **Auto-laid-out graph** — dependency/call graphs | Graphviz DOT | Viz.js |
| ` ```geometry ` | **Geometric construction** | JSON: `boundingbox` + `elements` | JSXGraph |
| ` ```fractal ` | **Fractal** — Mandelbrot/Julia (drag a box or click to zoom), IFS chaos game, L-system curves, strange attractors; 21 presets ([full list](#fractals-and-attractors)) | JSON: `type` + parameters | plain canvas (no library) |
| ` ```map ` | **Map** with markers / GeoJSON | JSON: `center`, `zoom`, `markers` | Leaflet + OpenStreetMap |
| ` ```plot3d ` | **3-D surface / scatter** | Plotly JSON | Plotly |
| ` ```molecule ` | **Chemical structure** | a SMILES string | SmilesDrawer |
| ` ```molecule3d ` | **3D structure**, rotatable/zoomable | XYZ format: atom count, comment line, then `Element x y z` per atom | 3Dmol.js |
| ` ```plotly ` | **Statistical chart** — histogram, box, violin, heatmap, contour, sankey, treemap, sunburst, candlestick, waterfall, funnel | Plotly figure JSON (`data` + `layout`) | Plotly |
| ` ```ode ` | **Phase portrait** — direction field and trajectories of a 2-D system; optional live parameter sliders | `dx = y`, `dy = -sin(x)`, `start: 1, 0`, `param: …` as in `plot` | math.js |
| ` ```scene ` | **3-D scene** of primitives — drag to rotate, wheel to zoom, shift-drag to pan | JSON `objects` (box, sphere, cylinder, cone, torus, plane, polyhedra) with `position`, `rotation`, `color` | three.js |
| ` ```sequence ` | **DNA / RNA / protein sequence**, or an alignment with the differing columns marked | FASTA (`>name` + residues) or a bare sequence | plain SVG (no library) |
| ` ```phylo ` | **Phylogenetic tree** — a phylogram with a scale bar, or a cladogram | Newick | plain SVG (no library) |
| ` ```reaction ` | **Reaction scheme** drawn from structures | reaction SMILES `reactants>agents>products` | SmilesDrawer |
| ` ```circuit ` | **Electrical schematic** on a grid | JSON `components` (`type`, `from`, `to`, `label`); y grows upward | plain SVG (no library) |
| ` ```gantt ` | **Project schedule** — dates, durations, dependencies | mermaid gantt syntax (no leading `gantt` line) | mermaid |
| ` ```timeline ` | **Dated event sequence** | mermaid timeline syntax (no leading `timeline` line). Times of day are written as normal — `2024-01-01 00:00 : Sunrise` — in periods, section labels and accessibility lines alike | mermaid |
| ` ```network ` | **Force-directed graph**, draggable nodes | JSON `{"nodes":[…],"edges":[{"from":…,"to":…}]}` | vis-network |
| ` ```geojson ` | **Geographic features** with click popups | a GeoJSON FeatureCollection (coordinates are `[lng,lat]`) | Leaflet |
| ` ```abc ` | **Sheet music**, with a Play button | ABC notation | abcjs |
| ` ```calc ` | **Computed arithmetic** — units, dates, matrices | one expression per line, e.g. `5 km/h to m/s` | math.js |
| ` ```solve ` | **Symbolic algebra** — derivative, simplify, expand, roots, evaluate | `derivative: x^3 + 2x` | math.js |
| ` ```stats ` | **Descriptive statistics** + linear regression | `data: 4, 8, 15` (or `x:` / `y:` lines) | math.js |
| ` ```table ` | **Sortable / filterable table**, computed totals, CSV export | JSON: `columns`, `rows`, optional `total` | native |
| ` ```diff ` | **Line diff** (LCS) of two texts | `--- before` / lines / `--- after` / lines | native |
| ` ```regex ` | **Runs a pattern** against samples, highlights matches + groups | `pattern:` / `flags:` / `test:` lines | native |
| ` ```truth ` | **Truth table** for a boolean expression (≤10 vars) | `(A and B) or not C` | math.js |
| ` ```svg ` (or `xml` / raw `<svg>`) | Custom vector graphic | SVG markup (sanitised) | native |

Notes:

- Every rendered *figure* (chart, diagram, plot, map, molecule, score, SVG, LaTeX) carries a **Source** toggle and **Copy** button, so the underlying markup is always inspectable; SVG additionally offers **⬇ PNG**. The Source pane is editable: change the markup or spec and press **▶ Apply** to re-render the figure in place, or **↺ Reset** to return to the original. While a figure is maximized, both wait until it is restored. Edits are local to the page — the stored message is unchanged, and a reload shows the original again. Ordinary Markdown and code blocks are not figures — code blocks get a plain **Copy** button.

<p align="center">
  <img src="screenshots/rendering/editable-source.png" alt="A plot card whose Source pane has been edited to add a parameter and a second function, then re-rendered with Apply" width="66%">
</p>
- A raw `<svg>…</svg>` with no code fence is detected and rendered too.
- **Computed, not asserted:** `calc`, `solve`, `stats`, `truth`, `table`, `diff` and `regex` are evaluated in your browser, not produced by the model. The model states *what* to compute (an expression, a data list, two texts); the browser returns the exact result — so unit conversions, column totals, derivatives and truth tables don't inherit the model's arithmetic mistakes. `calc`/`solve`/`stats`/`truth` use math.js (already loaded); `table`/`diff`/`regex` use no library at all.
- **Interactive `plot`:** declare a parameter with `param: a = <lo> .. <hi> (<init>)` and the block renders a slider; dragging it re-evaluates the formula and redraws instantly, with no round trip to the model.
- **Interactive `ode`:** the same `param:` sliders re-integrate a phase portrait as you drag.
- **`scene` navigation:** drag to rotate, wheel to zoom, shift-drag to pan; **⟲ Reset view** returns to the starting camera and **⬇ PNG** saves the current view. Under the canvas, **Rotate**, **Tilt** and **Zoom** sliders show the current view and drive it, and **↻ Spin** turns the scene continuously until you touch it.
- **Lazy loading:** the heavier renderers — Chart.js, Mermaid, Viz.js, JSXGraph, Leaflet, Plotly, SmilesDrawer, 3Dmol.js, three.js and highlight.js — are fetched only the first time a block of that type appears, so they cost nothing at page load. Markdown, sanitising, math and music (marked, DOMPurify, KaTeX, math.js, abcjs) load with the page because they are needed for ordinary answers.
- **Maximize and save as image:** every visual card (chart, diagram, plot, map, geometry, molecule, 3D molecule, 3-D scene) has an ⛶ **Maximize** button that opens it full-viewport, and most also have a ⬇ **PNG** button. Not offered on `map` (no rasteriser wired up) or the plain-data lanes (`table`, `diff`, `regex`, `calc`, `solve`, `stats`, `truth`) — those aren't rasterised images.
- **Third-party requests:** all renderer libraries are served from a public CDN (cdnjs, plus jsDelivr for SmilesDrawer, Viz.js and the Noto Music font that draws music notation), so rendering is not fully offline. Without the font, diagrams show common symbols (♩ ♪ ♬ ○) in place of notation it would draw. In addition, ` ```map ` fetches map tiles from `tile.openstreetmap.org` at view time — the only lane that sends *content-derived* data (the requested coordinates) to a third party. For an air-gapped deployment, vendor the libraries and serve them locally.
- **Safety:** SVG and diagram output is sanitised (DOMPurify) before insertion, and ` ```geometry ` accepts data only — never function or code strings.

### Rendering examples

| | |
|---|---|
| ![Mermaid diagram](screenshots/rendering/mermaid.png) | ![Data chart](screenshots/rendering/chart.png) |
| ![Graphviz graph](screenshots/rendering/dot.png) | ![Geometric construction](screenshots/rendering/geometry.png) |
| ![Map](screenshots/rendering/map.png) | ![3D surface plot](screenshots/rendering/plot3d.png) |
| ![Molecule](screenshots/rendering/molecule.png) | ![3D molecule](screenshots/rendering/molecule3d.png) |

| | |
|---|---|
| ![Timeline](screenshots/rendering/timeline.png) | ![Gantt](screenshots/rendering/gantt.png) |
| ![Network](screenshots/rendering/network.png) | ![GeoJSON](screenshots/rendering/geojson.png) |
| ![Lorenz attractor](screenshots/rendering/fractal.png) | ![Hilbert curve](screenshots/rendering/fractal-curve.png) |
| ![Box-zoom on a Mandelbrot set](screenshots/rendering/fractal-zoom.png) | ![Sierpinski triangle as nested outlines](screenshots/rendering/fractal-sierpinski.png) |
| ![3-D scene](screenshots/rendering/scene.png) | ![Phase portrait](screenshots/rendering/ode.png) |
| ![Circuit schematic](screenshots/rendering/circuit.png) | ![Sequence alignment](screenshots/rendering/sequence.png) |

### Fractals and attractors

The ` ```fractal ` lane computes the iteration in the browser on a plain canvas.
Naming a preset is all a question needs:

| Group | Presets |
|---|---|
| Escape-time | `mandelbrot`, `julia` — drag a box to zoom into it (kept to the card's proportions), or click to zoom in and shift-click to zoom out |
| Chaos game (IFS) | `fern` (Barnsley — a point cloud) |
| Nested subdivision | `sierpinski` (nested triangles), `carpet` (nested squares) — a **Depth** slider divides the figure into smaller copies of itself |
| Curves (L-system) | `koch`, `dragon`, `levy`, `arrowhead`, `gosper`, `plant`, `frond` (an L-system fern), `tree` |
| Space-filling curves | `hilbert`, `peano`, `moore` |
| Strange attractors | `lorenz`, `rossler`, `thomas`, `halvorsen`, `clifford`, `dejong` |

Common spellings reach the same preset — `Hilbert Curve`, `flowsnake`,
`Koch snowflake` and `lorenz_attractor` all resolve.

Every rendered fractal also carries a slider under the canvas that changes how far it iterates and re-renders in place, with no round trip to the model: **Iterations** for Mandelbrot and Julia, **Depth** for the L-system curves and the self-tiling `sierpinski`/`carpet` (each level replaces the figure with smaller copies of itself), **Iterations** for the Barnsley fern and other chaos-game IFS, and **Steps** for the strange attractors. On a Mandelbrot or Julia card, **⟲ Reset zoom** returns to the starting view.

Every option is optional:

- `palette` — `viridis` (default), `fire`, `ocean` or `mono`.
- `order` — recursion depth of a curve, from 1 up to a per-curve maximum:
  `peano` 4, `gosper` 5, `moore` 6, `hilbert`, `koch`, `plant` and `frond` 7, `arrowhead` 10,
  `tree` 13, `dragon` and `levy` 14. An order above 14 is reduced to 14; between
  a curve's own maximum and 14 it reports `L-system exceeds 120000 symbols`
  instead of drawing.
- `iter` — escape-time detail (16–1000), or the number of points along an
  attractor's orbit (500–200,000). `steps` is accepted as a synonym.
- `plane` — `xy`, `xz` or `yz`: which projection to draw of `lorenz`, `rossler`,
  `thomas` or `halvorsen`. `clifford` and `dejong` are already flat and ignore it.
- An attractor's own coefficients: `sigma` / `rho` / `beta` for `lorenz`, `a`–`c`
  for `rossler`, `a`–`d` for `clifford` and `dejong`, `b` for `thomas`, `a` for
  `halvorsen`. `dt` sets the integration step of the four continuous systems, bounded relative
  to each one's own default — a fiftieth of it to four times it — because they
  differ by two orders of magnitude in stiffness. A larger value is reduced to
  that ceiling rather than rejected.
- `c` — for `julia` only, the complex constant as `[re, im]`.
- `center`, `zoom` — for `mandelbrot` and `julia`. `points` — for `ifs`.
  `axiom`, `rules`, `angle`, `depth` — for a hand-written `lsystem`.

So `{"type":"hilbert","order":5}` draws a fifth-order Hilbert curve,
`{"type":"lorenz"}` draws the Lorenz butterfly, and
`{"type":"lorenz","plane":"xy","palette":"fire"}` draws it from above.

Anything the presets do not cover is written out in full as
`{"type":"ifs","maps":[…]}` or
`{"type":"lsystem","axiom":…,"rules":…,"angle":…,"depth":…}`. An L-system's rule
strings use letters and `+ - [ ]` only: `F` and `G` draw a step, `f` moves without
drawing, `+` and `-` turn, `[` and `]` open and close a branch.

### Working with tables and charts

Every Markdown table an answer produces gets a toolbar: click any header to sort
(numbers, currency and dates sort by value, not alphabetically), filter rows as
you type, and copy or download the visible rows as CSV. The header stays put
while the body scrolls.

![Sortable table](screenshots/rendering/table-sort.png)

Line, scatter and bubble charts support wheel-zoom (hold Ctrl) and drag-to-pan,
with a **Reset zoom** button once you have moved. Every chart also has a **Data**
button that reveals the series behind it as one of those sortable tables — so you
can check the numbers a chart was drawn from.

![Chart data view](screenshots/rendering/chart-data.png)


### RAG context inspector

After each AI response that used RAG, a **📚 N matched chunks** badge appears in the message metadata row.

- By default the inspector is **collapsed** — click the badge to expand it
- To open it automatically after every answer, enable **Show RAG Matches in Answers** in **Settings → General**
- Cache hits also show their associated RAG chunks (stored at cache-write time)

### Citations

When an answer is grounded in your knowledge base, the retrieved passages are numbered and the model marks each claim with the passage it came from — `[1]`, `[2]`, and so on.

**Each marker is a button.** Point at one, or move the keyboard focus to it, and a card opens beside it with that source: its name or address (a link for web pages), the knowledge base it came from, its match score (and the reranker's score when reranking is on) and the retrieved passage itself. Escape closes the card; on a touch screen a tap opens and closes it. Click a marker, or use **Show in sources** on the card, to open the RAG inspector, expand that source and highlight it. From the keyboard, **Enter** or **Space** on a marker moves into its card: **Tab** goes between the card's link and **Show in sources** (staying in the card), and **Escape** closes it and returns to the marker. A bracketed number inside a code sample is left alone — `arr[1]` is an array index, not a reference.

A number the answer was not given a source for — `[7]` when five passages were retrieved — is shown struck through with a dashed outline: it is a broken citation and opens nothing.

Cards work the same in a reopened conversation: they read the passages stored with each answer.

### Grounding badge

Every answer that ran a knowledge-base search gets a badge saying how well it is backed by its sources:

| Badge | Meaning |
|---|---|
| **● Grounded** | Every substantive paragraph, list item and table row cites a source, and every citation number points to a real source. |
| **◐ Partly grounded** | Some of it is cited, but not all of it — or a citation number points to no source. |
| **○ Uncited** | The answer was given sources and cites none of them. |
| **– Nothing retrieved** | A knowledge base was searched and nothing matched, so there was nothing to cite. |
| **○ Cites missing sources** | A knowledge base was searched and nothing matched, yet the answer cites source numbers — sources it was never given. |
| **⚠ Search failed** | The knowledge base could not be searched (its storage could not be reached, or it was indexed with a different embedding model), so nothing was retrieved. |
| **– No documents used** | No knowledge base was searched for this answer. |
| **∅ Not in your documents** | Documents only was on and nothing matched (see below). |

How it is decided: code blocks, inline code and display maths are ignored; headings, horizontal rules and table header rows are not counted; a paragraph, list item or table row with fewer than six words, or one that ends with a colon (a lead-in such as "The steps are:"), does not need a citation. In scripts written without spaces between words (Chinese, Japanese, Thai, Lao, Khmer, Myanmar) every two characters count as one word. A number beyond twice the number of sources is read as a reference quoted from a document, and left as plain text, when it stands inside a quotation (`"…"`, `“…”`, `«…»`) or opens a line of a reference list (`[12] Smith, J. …`); any other number with no source behind it is shown struck through as a broken citation and counts against the answer. Hover the badge for the counts. Answers in Documents only mode carry **· Documents only** on the badge.

Each version of a regenerated answer (‹ n/N ›) is shown with its own sources, citations and badge.

`[2]` and **#2** always refer to the same passage, in a live answer, a reopened conversation and a cached one alike. The inspector lists passages best-match-first, which is not necessarily the order they were numbered in.

<p align="center">
  <img src="screenshots/features/citations-click.png" alt="An answer whose [2] marker has been clicked, opening and expanding source 2 in the matched-chunk inspector" width="88%">
</p>

<p align="center">
  <img src="screenshots/rendering/citations-scope.png" alt="An answer with [1] and [2] citation markers, the matched-chunk inspector, and a source-scope chip above the composer" width="88%">
</p>

### Scoping a question to one document

Click **⌖ only this** on any chunk in the inspector — or the ⌖ button in the document list — to restrict the *next* question to that source. A chip appears above the composer showing the active scope; click ✕ to clear it. The filter is applied inside the search itself (both the vector and keyword halves), not by discarding results afterwards, so scoped questions still return the best matches *within* that document.

### When nothing matches

If retrieval runs and finds nothing above the similarity threshold, the answer is marked **📭 No KB match** and the model states plainly that it is answering from general knowledge rather than your documents. When the knowledge base searched holds no documents at all, the badge says so instead — **📭 “name” is empty**. Click the badge to open Settings → RAG.

<p align="center">
  <img src="screenshots/rendering/no-kb-match.png" alt="An answer marked with a No KB match badge" width="88%">
</p>

An answer with no badge at all was produced without RAG (all instances disabled, or none selected).

### Documents only

Turn on **📚** beside the template picker (a **📚 Documents only** chip appears above the message box; its ✕ turns it off) to have answers come **only** from your documents:

- The model is told to answer from the retrieved passages — and from a document you attached to the message — and nothing else: no general knowledge. If the passages do not contain the answer, it says so; if they cover only part of the question, it answers that part and says which part the documents do not cover. Every statement it takes from the passages is cited.
- When the search finds nothing (and no document is attached), the model is not asked at all. The answer is a fixed **Not found in your documents** message, marked **∅ Not in your documents**. When no knowledge base is selected for the question, the message says that instead. These answers are not cached.
- When the search itself fails — the storage holding the knowledge base cannot be reached, or the knowledge base was indexed with a different embedding model than the one now selected — the answer is a fixed **Search failed: …** message naming the knowledge base and the reason, marked **⚠ Search failed**, instead of "not found": the documents were not looked at. The model is not asked and nothing is cached.
- A follow-up that finds nothing on its own wording ("shorten that", "explain point 2") is answered from the sources of the previous answer, when that answer cited them: the model gets those passages under the same rules, and the source list shows them again under **↩ N sources from the previous answer** (the badge's tooltip says so too). Such an answer is not cached. This happens only when all of these hold — otherwise the fixed "not in your documents" answer is given, without calling the model:
  - the question is asked against the same knowledge bases as the previous answer (switching the selection, a `#` mention or a project with other knowledge bases counts as a change), and only passages from those knowledge bases — and matching **⌖ only this**, when set — are used;
  - the question brings in nothing of its own: each of its words is an instruction or a reference ("shorten", "explain", "table", "that", "point 2") or appears in the previous question, answer or its sources. "What is the capital of France?" after a question about a warranty is a new question, not a follow-up. Only English instruction words are known; in other languages a follow-up counts as one when its words appear in the previous question, answer or sources.

The setting belongs to the conversation: it is kept with each answer, and a reopened conversation comes back in the mode its latest answer used. New conversations start from **Settings → RAG → Documents only for new conversations**. Answers given in Documents only mode and normal answers are cached separately, so one is never replayed as the other.

### Projects

A project gathers conversations about one subject and gives them their settings: a name (with an optional icon and colour), the **knowledge bases** to search, a **prompt template**, a **provider and model**, and a **Documents only** default. Each one is optional — a project without knowledge bases leaves the selection in use, one without a template keeps the template in use, and so on.

- **The switcher** under **＋ New Chat** shows *All conversations*, *No project*, or one project. With a project shown, the list holds only its conversations (still grouped by date) and **New Chat** starts a conversation in it, with its knowledge bases, template, model and Documents only setting. A chip above the message box names the project of the conversation you are in.
- **＋** beside the switcher creates a project; **✎** (shown while a project is picked) edits it. **Delete project** asks first, and never deletes a conversation: the project's conversations — every user's — are kept and become conversations without a project.
- A conversation belongs to at most one project, set when its first message is sent.
- Rows of conversations in a project show the project's icon (or a dot in its colour) instead of 💬.
- **Settings → RAG → Advice measured on** reads the retrieval advice for one project's knowledge bases only.

With sign-in on, projects are created, changed and deleted by administrators and are visible to everyone; anyone can start their own conversations in one.

**Each conversation keeps its own knowledge-base selection.** The knowledge bases a conversation searches, its template and its model are stored with it. Reopening it — in this browser or another — brings them back, instead of whatever was selected last. A change made inside a project's conversation stays with that conversation; a change made in a conversation without a project also becomes this browser's choice for new conversations, as before. A new conversation without a project starts from that choice. In the knowledge-base picker, a project's selection of several knowledge bases shows as **⧉ name, name**.

### Mentioning a knowledge base with #

Type **#** in the message box to pick knowledge bases for the question you are writing: a list of the enabled knowledge bases opens above the box and narrows as you type (**↑/↓** move, **Enter** or **Tab** picks, **Esc** closes it; a click or tap picks too). Each pick becomes a **# name** chip above the box (its **✕** removes it) and the typed `#…` leaves the question. That question then searches **only** the mentioned knowledge bases — with Documents only too — and the question shows them under it. The conversation's own selection is unchanged, and the next question searches it again. A `#` inside a word (`C#`, `issue#4`) does not open the list.

### Finding an earlier answer

**Shift+⌘/Ctrl+F** opens VisualWeaver's own search. Plain ⌘/Ctrl+F is left to the browser, whose find covers the rendered page.

- Searches message text **and the retrieved source passages** stored with each answer
- **All conversations** widens it beyond the current one; conversations stored only on the server are loaded on demand, and any that cannot be loaded are named
- Reports the match count, highlights each hit in context, and names the conversation a result came from
- Clicking a result opens that conversation at the message

<p align="center">
  <img src="screenshots/features/search.png" alt="The search overlay showing five matches across three conversations, including a hit inside a retrieved source" width="80%">
</p>

### Notifications

Messages appear bottom-right. **Errors stay until dismissed**; everything else clears itself after a few seconds. Hovering pauses the countdown, and ✕ dismisses immediately.

- A message repeated while it is still on screen collapses into one entry with a count
- At most five are shown at once; the full history is kept regardless
- **Settings → Logs → Notifications** lists every message raised this session, newest first, with an error count. It is cleared on reload

<p align="center">
  <img src="screenshots/features/notifications-toasts.png" alt="Three stacked messages: a red error, a green success and a blue info, each with a dismiss button" width="52%">
</p>

<p align="center">
  <img src="screenshots/features/notifications-log.png" alt="The Notifications list in the Logs tab showing five messages with timestamps and severity pills" width="88%">
</p>

### Session management

Sessions are listed in the left sidebar, newest first, under **Pinned**, **Today**, **Yesterday**, **Previous 7 days**, **Previous 30 days** and **Older**. Click a session to switch to it. Point at a row for its actions (on a phone, open the conversation and reopen the sidebar — its row then shows them): **✎** renames it in place (Enter saves, Esc cancels), **📌** pins it at the top of the list, and **✕** deletes it (with confirmation). Names and pins are stored with the conversation, so they survive a reload; a name you give is never replaced by the automatic title.

**How long conversations are kept.** Conversations are kept until you delete them — including ones this browser does not list (see below). **Settings → General → Conversations → Keep conversations** can limit that to **30 days** or **1 day** after a conversation's last message (a value must be `0` or at least `3600` seconds: a Settings save or config import with a smaller one is refused, and a smaller one written in `config.json` is used as `3600`); the rows then say *expires in …*. Pinned conversations are always kept. A change applies to every stored conversation when you save: **Forever** lifts every expiry; a limit gives each conversation at most that long from the moment you save (one with less time left keeps it), and a longer limit than before gives every conversation the new limit from that moment.

The foot of the sidebar holds the actions that apply to the conversation you are in — **Export Chat** as `.md` or `.txt`, and **🗑 Clear Chat**, which empties the current conversation after a confirmation and leaves every other one alone.

Conversations are **persisted in the app-data store (see [Storage Connections](#storage-connections)) and restored on reload** — closing the tab or restarting the browser no longer loses them, and the conversation you were last in is reopened automatically.

**Which conversations the sidebar lists.** With sign-in off, the sidebar lists the conversations started or opened in *this* browser — the browser keeps their ids (the 1,000 most recently opened, plus every pinned one). Conversations stored from another browser, or from this one before its site data was cleared, are not listed, but they are kept like any other: **Settings → General → Conversations** counts them, **Show them in the sidebar** adds them to this browser's list, and **Delete them…** removes them from the server after a confirmation. With [sign-in](#sign-in-through-a-reverse-proxy-multiple-users) on, the sidebar lists every conversation of the signed-in user, in every browser.

Each message records the provider and model that produced it, and the knowledge bases it was asked against. Each conversation also keeps its project, its knowledge-base selection and its template (see [Projects](#projects)). Sessions are automatically titled from the first message.

### Regenerating an answer

**↻ Regenerate** re-asks the question that *that* answer belongs to and replaces it **in place**, rather than appending a second copy of the question. Earlier attempts are kept: a **‹ 1/2 ›** control appears in the message metadata row so versions can be compared. Regenerating an answer that has later turns after it will discard those turns, and asks for confirmation first.

---

## RAG Instances

### Creating an instance

1. Open **Settings → RAG**
2. Click **＋ New Instance**
3. Enter a name (letters, numbers, hyphens), pick a colour, and choose **Storage** — the built-in Redis, or a Redis or MongoDB connection added under **Settings → Storage**
4. Click **Create**

### Instance controls

Each card in the instance list exposes:

| Button | Action |
|---|---|
| **● On / ○ Off** | Enable or disable the instance |
| **📄 Documents** | Browse the documents in the instance; scope a question to one, or delete one |
| **⬇** | Export the instance to a `.zip` file (includes all chunks and embeddings) |
| **⬆** | Import from a previously exported `.zip` |
| **✦ Dedupe** | Deduplicate — remove exact-duplicate chunks to reduce storage footprint |
| **🧹** | Clear all chunks (wipes data, keeps the instance) |
| **🗑** | Permanently delete the instance |

### Managing individual documents

**📄 Documents** lists every source in the instance with its chunk count and ingest date.

<p align="center">
  <img src="screenshots/rendering/documents.png" alt="The document manager listing each source URL with its chunk count, a scope button and a delete button" width="88%">
</p>

- **⌖** scopes the next question to that document (see *Scoping a question to one document*)
- **🗑** removes just that document's chunks, leaving the rest of the instance untouched

Deleting a document also releases its de-duplication fingerprints, so the same file or URL can be re-ingested afterwards — which is the supported way to refresh a single stale document without wiping and rebuilding the whole instance.

The date column shows `—` for documents ingested before this metadata was recorded; they are otherwise fully functional and gain a date when re-ingested.

### Exporting and importing

Exports produce a ZIP containing `meta.json` and `chunks.json`. Embeddings are included as base64, so re-embedding is not needed on import — making it fast.

When importing into an existing instance, the current endpoint assignment and colour are preserved; only the chunk data is replaced.

### Deduplication (Chunk Optimization)

Click **✦ Dedupe** on any instance card to remove exact-duplicate chunks. Two chunks are considered duplicates if their text is identical after lowercasing and whitespace normalisation.

---

## LLM Providers

All providers are configured in **Settings → Providers** — a unified accordion UI where each provider is a collapsible card. Click any card header to expand it; the currently active provider is highlighted.

### Provider overview

| Provider | Type | Notes |
|---|---|---|
| ⚡ Ollama | Local | Any model installed locally; vision auto-detected |
| ✦ Claude | API | Anthropic native SDK; `claude-opus-4-6`, `claude-sonnet-4-6`, etc. |
| ◆ OpenAI | API | OpenAI native SDK; GPT-4o, GPT-4.1, o-series |
| 🟣 Qwen | API | Alibaba DashScope (OpenAI-compatible), `qwen-max`, `qwen-plus`, etc. |
| ⚡ Groq | API | Ultra-fast inference (OpenAI-compatible), Llama / Mixtral / Gemma |
| ◐ Mistral | API | EU-hosted (OpenAI-compatible), free "Experiment" tier; `mistral-large-latest`, `mistral-small-latest` |
| ✦ Gemini | API | Google AI native SDK; chat-capable `gemini-*` models, listed live from your key |

### Choosing a provider and model

The top bar carries one model picker, reading `● Ollama / gemma4:31b-mlx`. Opening it
lists every model grouped by provider, with a status dot on each group — green when the
provider is reachable, red when it is configured but failing. Type to filter across all
of them. Choosing a model switches the provider with it, so the pair is always
consistent.

Providers with no API key are collapsed into a single row at the bottom that links
straight to **Settings → Providers**; they stay listed rather than being hidden, so a
provider you have not set up is still visible as something you could use.

Each provider's **default** model is marked with its own colour and a `default` badge.
That is the model you get on switching to a provider you have not chosen one for — on
Gemini and Mistral it is the free-tier model.

A provider offering more than eight models shows the first few behind a **Show all N**
row. The order is: the model in use, then the provider's default, then `-latest`
aliases, then stable names, with pinned dated snapshots last. The model in use and the
default are always shown.

Models are filtered to those that can hold a conversation. Where a provider reports
per-model capabilities, that report is used: Gemini models without `generateContent`
and Mistral models without `completion_chat` are not offered, which excludes embedding,
OCR, moderation, text-to-speech, image, robotics and live-audio models from the list.

The 👁 marker on a model means it accepts image attachments, which enables the 📎
button in the composer.

The **Use** button inside a provider card in **Settings → Providers** still switches
provider too.

### Environment variables

API keys can be supplied via environment variables — they are never written to `config.json`:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."
export DASHSCOPE_API_KEY="sk-..."
export MISTRAL_API_KEY="..."
export GROQ_API_KEY="gsk_..."
export GEMINI_API_KEY="AIza..."
```

### OpenAI-compatible endpoints

Any provider using the OpenAI-compatible API (OpenAI, Qwen, Groq, and many others) can be pointed at a custom base URL.

| Provider | Default base URL |
|---|---|
| OpenAI | `https://api.openai.com` |
| Qwen (DashScope) | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Mistral | `https://api.mistral.ai/v1` |
| Groq | `https://api.groq.com/openai` |
| LM Studio | `http://localhost:1234` |
| Together AI | `https://api.together.xyz` |

### Native SDK usage

Each provider uses its official Python SDK for streaming:

| Provider | SDK | Streaming method |
|---|---|---|
| Claude | `anthropic` | `AsyncAnthropic.messages.stream()` |
| OpenAI | `openai` | `AsyncOpenAI.chat.completions.create(stream=True)` |
| Qwen | `openai` (DashScope) | Same as OpenAI with custom `base_url` |
| Mistral | `openai` (Mistral endpoint) | Same as OpenAI with custom `base_url` |
| Groq | `openai` (Groq endpoint) | Same as OpenAI with custom `base_url` |
| Gemini | `google-genai` | `client.aio.models.generate_content_stream()` |

---

## Ingesting Content

### From files — with live progress

1. **Settings → RAG → Ingest Documents**
2. Select a target RAG instance
3. Drop files onto the upload area or click to browse
4. Supported formats: `.txt`, `.md`, `.csv`, `.pdf` (needs PyMuPDF), `.docx` (needs python-docx), `.xlsx` (needs openpyxl)

Files are processed one by one via a streaming SSE connection. The progress bar advances per-file and shows the file name and chunk count in real time.

- **✕ Cancel** stops before the next file. Everything already indexed is kept — this is a stop, not a rollback — and uploads that were never reached are removed
- Reopening **Settings → RAG** re-attaches to an ingest still running on the server and keeps following it. If several are running, the others are offered
- On a first run the panel reports that the embedding model is downloading (about 90 MB) before indexing starts
- A file the indexer reports as failed — an unsupported type, a scanned PDF with no extractable text — counts towards errors, not successes

<p align="center">
  <img src="screenshots/features/ingest-progress.png" alt="File ingestion in progress showing the per-file status and a Cancel button" width="80%">
</p>

### From a URL

1. **Settings → Web Sources**
2. Enter a URL and configure:

| Setting | Description |
|---|---|
| **Depth** | 0 = page only; 1–3 = follow links N levels deep |
| **Max Pages** | 0 = unlimited; set a cap to avoid runaway crawls |
| **Respect robots.txt** | Honour crawl restrictions |
| **Local links only** | Stay within the same domain |

3. Click **🕷 Start Crawl** — progress streams in real time

### llms.txt manifests

URLs ending in `llms.txt` (e.g. `https://redis.io/llms.txt`) are treated as curated link manifests. All linked pages are fetched regardless of domain. This is the fastest way to ingest entire documentation sites.

**Presets** for common sources:
- 🟥 Redis Docs (llms.txt)
- 🐍 Python Docs
- 🦜 LangChain

### Chunking — sentence-aware

Text is split into overlapping chunks that **respect sentence boundaries**. The chunker splits on `.`, `!`, and `?` first, then groups sentences into windows of approximately `chunk_size` words.

Content with no sentence punctuation — CSV and spreadsheet rows, Markdown tables, code — is split on line boundaries instead, so a large table cannot become one enormous chunk. A chunk is never allowed to exceed twice `chunk_size`.

Configure in **Settings → RAG**:

| Setting | Default | Description |
|---|---|---|
| **Chunk Size** | 180 words | Target words per chunk |
| **Chunk Overlap** | 32 words | Words of context carried into the next chunk |

> **Chunk size is bounded by the embedding model.** Each model can only encode a fixed number of tokens (`intfloat/multilingual-e5-small` handles 256, roughly 190 English words) and silently truncates anything longer — the excess text is still stored and shown, but is **not represented in the vector**, so it cannot be found by semantic search. VisualWeaver warns in the log when the configured chunk size exceeds what the active model can encode, and lowers a saved value that is already over the limit (logging the change).

---

## Multi-RAG Parallel Queries

Click the **🔀** button in the topbar (next to the RAG dropdown) to enter parallel mode.

In parallel mode:
- **All enabled** RAG instances are queried simultaneously
- Each instance can live on a **different Redis server** — queries fan out concurrently
- Results from all instances are merged and re-ranked by score
- The top-K chunks (across all knowledge bases) are injected into the LLM context
- Each chunk in the RAG inspector shows which instance it came from (🗄 instance-name)

To return to single-instance mode, click 🔀 again or select a specific instance from the dropdown.

### No retrieval (No RAG)

Select **⊘ No RAG** from the top-bar dropdown to answer the next messages with **no** retrieval at all — the model replies from its own knowledge, with no knowledge-base search and no grounding or citation instructions. This is distinct from querying a single instance or **✦ All RAGs** in parallel; pick a specific instance (or All RAGs) again to resume retrieval.

---

## Semantic Cache

The cache intercepts queries before they reach the LLM. If a semantically equivalent question exists in the cache (above the similarity threshold), the stored answer — including associated RAG chunks — is returned immediately.

A cached answer is replayed only under the same conditions it was given under: the knowledge bases searched, the provider and model, the prompt (base instruction and template), a source scope, Documents only, and whether it is a drawing. Projects add no condition of their own — their knowledge bases, template and model already are these — so two projects, or a project and a conversation without one, share an answer only when all of them match. A question restricted with [# mentions](#mentioning-a-knowledge-base-with-) is cached under the knowledge bases it searched.

| Setting | Default | Effect |
|---|---|---|
| **Enabled** | ✅ | Toggle the cache on/off |
| **Similarity Threshold** | set by the embedding model | Higher = stricter match required for a cache hit |
| **TTL** | 3600 s | Seconds before a cached entry expires |

### The similarity threshold is set by the embedding model

Left unset, the threshold follows whichever embedding model is configured:

| Embedding model | Threshold |
|---|---|
| `all-MiniLM-L6-v2` | 0.93 |
| `multilingual-e5-small` | 0.98 |
| `multilingual-e5-base` | 0.98 |
| `bge-m3` | 0.93 |
| anything else | 0.98 |

These are not interchangeable. The models place unrelated text at very different
similarities — two questions about completely different subjects score about 0.10 apart
under `all-MiniLM-L6-v2` and about 0.80 apart under `multilingual-e5-base` — so the same
number is a permissive setting under one model and admits unrelated questions under
another. Changing the embedding model therefore resets the threshold along with the
cached entries, which are also not comparable across models.

Each value is the lowest that admitted no unrelated question in the set at
`tests/fixtures/cache_calibration.py`, on the basis that the two mistakes do not cost the
same: a wrong hit answers a question that was never asked, while a miss costs one model
call. You can still set the value yourself, and an explicit setting is kept until the
embedding model changes.

**What this cannot do.** Two questions that differ in one decisive word — *enable* against
*disable*, *Q1* against *Q2*, *back up* against *restore* — score higher than most genuine
rewordings, in every model tested. No threshold separates them, so the values above are
chosen to exclude them and give up cache hits to do it. Raising the hit rate by lowering
the threshold trades directly against answering the wrong question.

The cost of that is not evenly spread. Under `multilingual-e5-base`, unrelated questions
already sit near 0.80, so the safe value is 0.98 and only near-identical wording matches —
the cache behaves close to an exact-match cache. Under `all-MiniLM-L6-v2` and `bge-m3`,
which spread their scores much further apart, the safe value is 0.93 and genuine rewordings
match. If cache hit rate matters to you more than multilingual retrieval, that is the
trade-off to weigh.

### Requests to draw something

A question that asks to *see* something — a chart, a diagram, a plot — is cached, but is
matched only when the question is the same question, not a similar one. Similarity does not
work there: a drawing request is mostly scaffolding around one word that carries the whole
meaning, so *"chart of CO2 emissions 1990-2020"* and *"…2000-2020"* score 0.9867 apart, and
*"plot f(x) = sin(x)"* and *"sin(2x)"* score 0.9790 — both above any threshold that would
still match anything. Asking the identical question again is served from the cache;
anything else generates fresh.

Cache hits appear with a green **⚡ Cached XX%** badge showing the match score.

Answers that were not served from the cache say which: **🔍 Live** when nothing similar was
stored, **🚫 Not cached** for a request that is never cached (an attachment, or a chart or
diagram, which is always generated fresh), and **↻ Regenerated** when the cache was
deliberately bypassed.

### Managing individual cache entries

Every cached message has two action buttons:

- **🗑 Uncache** — Removes just that one cache entry. Future queries that would have matched it will go to the LLM instead. With sign-in on, only an entry cached for you can be removed (an administrator can remove any).
- **↺ Re-run fresh** — Bypasses the cache for this query only, re-generates a fresh response, and the new answer automatically updates the cache.

Clear the entire cache in **Settings → Cache → 🗑 Clear Cache**.

### Where the cache is stored

The cache is kept on the same storage connection as the app's own data — Redis or MongoDB — and behaves the same on both: the same similarity threshold, TTL, matching rules and analytics.

- **Redis** needs the Search module (Redis 8, Redis Stack or Redis Enterprise). Without it the cache is disabled and a warning is logged.
- **MongoDB** needs vector search (MongoDB Atlas, or MongoDB 8.3+ with the search process, such as the built-in MongoDB). The cache uses the collection `vw_cache` and one search index, `vw_cache_vector`, in the connection's database. On a connection that also holds RAG instances that makes three search indexes, the limit of Atlas's free tier; where no further search index can be created, the cache is disabled and a warning is logged. On a new database the index needs a moment to build (about a second on the built-in MongoDB); until then questions are answered live and new answers are stored. A new answer can be matched within about a second of being stored.

Each connection keeps its own cache entries. When the app's data moves to another connection, the cache uses the entries stored there — none, the first time — and the entries on the previous connection stay where they are.

---

## Storage Connections

A RAG instance is stored on the built-in store unless it is assigned to another **storage connection**: a further Redis server, or a MongoDB database — self-hosted or MongoDB Atlas.

The built-in store is the connection named `default`: the built-in Redis, or the built-in MongoDB on an installation that chose MongoDB (`storage.default_type` in `config.json`). When both are installed, the second one is the built-in connection `local-mongodb` (or `local-redis`). Built-in connections cannot be removed.

### Where the app keeps its own data

Conversations, the token-usage tally, retrieval stats, watched-folder state and the semantic cache live on one connection with vector search — the built-in store unless another is chosen (`storage.app`, set with `POST /api/storage/app`). Moving to another connection copies conversations (each keeps its remaining lifetime; one already on the new store is replaced), and the usage tally, retrieval stats and watched-folder state become exactly those of the previous store. The previous store keeps its copy, and the semantic cache starts empty on the new one. Answers finishing during the move wait for it and are saved on the new store. A connection that cannot be reached, or has no vector search, cannot be chosen; the connection holding the app data cannot be removed or change type. When the previous store cannot be reached any more, the move can be made without copying.

### Adding a connection

1. **Settings → Storage → Connections for RAG instances**
2. Click **＋ Add connection** and choose **Redis** or **MongoDB**
3. Redis: name, host, port, password, database index and TLS. MongoDB: name, server, username, password and database. The server is the Atlas cluster address (`cluster0.xxxxx.mongodb.net`) or `host:27017`, and the connection string is built from it; a whole connection string works too. The Atlas string from **Connect → Drivers** (`mongodb+srv://user:<db_password>@cluster0…`) can be pasted as it is: the username and password move to their fields, and the placeholder leaves the cursor in **Password**
4. **🔌 Test** checks the values before saving; **Add connection** saves them

MongoDB's search process stops building indexes when the disk holding them is over 85 % full, resumes below 80 %, and stops updating existing ones above 90 % — searches then miss new documents without an error. For the built-in MongoDB that disk is the container runtime's (the Docker / Colima VM disk on macOS); `./start.sh`, `./install.sh` and `./docker.sh` warn from 80 %.

A connection must be able to search vectors to hold an instance:

| Kind | Requirement |
|---|---|
| Redis | The Search module — Redis 8, Redis Stack or Redis Enterprise |
| MongoDB | MongoDB Atlas, or MongoDB 8.3+ with the search process (`mongot`), such as the built-in MongoDB the installer sets up |

A connection without vector search is shown as **No vector search** and cannot be chosen for an instance.

### Assigning an instance

When creating a new RAG instance, the **Storage** dropdown lists the built-in store and every connection, with its kind and address.

### MongoDB layout

All instances on one MongoDB connection share these collections in its database: `vw_chunks` (chunks with their embeddings), `vw_meta` (instance metadata), `vw_hashes` (deduplication), `vw_urls` (crawled pages), `vw_counters` (chunk ids) and `vw_instances` (the instance names), plus two search indexes on `vw_chunks`: `vw_vector` (vector search) and `vw_text` (keyword search). A connection holding the app data also has `vw_kv` (conversations and other values, with their expiry) and `vw_hash` (the usage tally and watched-folder state). Retrieval is hybrid on MongoDB as on Redis — a vector leg and a keyword leg fused with Reciprocal Rank Fusion — and scores use the same cosine scale, so the similarity threshold means the same on either. Search indexes build in the background: until they are ready, searches on a new connection return nothing, and freshly ingested chunks become searchable once the server has indexed them. Changing the embedding model rebuilds the vector index for the new model; chunks embedded with the previous model are searchable again after re-ingesting.

### How routing works

- An instance's metadata records its connection; `rc_for_instance(name)` looks it up on the built-in connection first, then on each other connection
- Queries, ingestion, exports, document deletes and deduplication go to the connection that holds the instance
- Exports are the same format on every kind of storage, so an export from one imports into another
- Parallel 🔀 mode fans out to every instance's storage concurrently using `asyncio.gather`

---

## Settings Reference

Settings are organised into tabbed groups. See [SETTINGS.md](SETTINGS.md) for a detailed explanation of every individual field and its implications.

### Group 1 — Infrastructure

#### 💡 Status
Live health check for all connected services. Shows Redis version, Ollama availability, and API key validity. Local checks (Redis, Ollama) run every 30 seconds; cloud provider key checks run every 5 minutes.

#### 🗄 Storage
Shows the built-in Redis (host, port, db, password, SSL — set up by the installer and read-only here) and its memory monitor, and manages the connections RAG instances can be stored on — further Redis servers and MongoDB databases.

### Group 2 — Knowledge

#### 📚 RAG
- **Chunk Size** and **Chunk Overlap** (sentence-aware chunking)
- **Top-K** — number of chunks returned per query
- **Similarity Threshold** — minimum cosine score for a chunk to be sent to the LLM
- **⚡ Hybrid Search** — combines vector KNN with BM25 full-text search (recommended: on)
- RAG instance management: create, delete, toggle, export, import, deduplicate
- File ingestion panel with live per-file streaming progress

#### 🌐 Web Sources
URL crawler with depth and page-cap controls. Real-time progress log. Saved web source list.

#### ⚡ Cache
Enable/disable semantic cache, threshold, TTL, analytics, and clear button. With [sign-in](#sign-in-through-a-reverse-proxy-multiple-users) on, the analytics list only your own cached questions; below the counters, a line gives the number of cached answers of all users and of each user (by an anonymous tag), without their questions.

### Group 3 — Providers

#### 🤖 Providers
All seven providers in a single accordion tab. Each provider has a collapsible card showing its configuration fields, connection test button, and a **Use** button to set it as the active provider.

The card header shows:
- A status dot (green = connected, red = error, grey = not configured)
- Provider name and category tag (Local / Free / API)
- Currently selected model name
- Expand/collapse chevron

### Group 4 — App Config

#### 🔧 General
- **Theme** — light / dark / auto
- **Embedding Model** — select and re-index if changed
- **Max Image Dimension** — for vision model attachments
- **📚 Show RAG Matches in Answers** — auto-expand RAG inspector
- Config import/export/reset

#### 💬 Templates
Create, edit, and delete named system prompts.

#### 🔐 Security
VisualWeaver has **no login page of its own**: out of the box it is a single-user app with no authentication, and the password field here is stored but **not currently enforced** — access is not gated on it. To give several people their own conversations and keep Settings to administrators, turn on [sign-in through a reverse proxy](#sign-in-through-a-reverse-proxy-multiple-users). `install.sh` asks whether the web UI is for this machine only or any device on the network and records the answer in `.visualweaver.env` (`VISUALWEAVER_HOST=127.0.0.1` or `0.0.0.0`); change it later with `./changeAccess.sh local` or `./changeAccess.sh lan` and restart, and put a reverse proxy with HTTPS + auth in front before exposing it beyond a trusted network (see `deploy/docker-compose.https.yml`). Because there is no login, the app refuses requests that another website makes from your browser (another site's page cannot use the chat or change settings); a link or frame on another website opens only the page itself (`/`), and any other address of the app opened from another site is refused (`403`). It answers requests — from browsers and from scripts alike, so also an old browser that sends no `Origin` or `Sec-Fetch-*` header — only for host names an outsider cannot own: IP addresses, `localhost` (and names ending in `.localhost`) and this machine's own names (its host name, its short name and `<short name>.local`; `./docker.sh` records them for the container). `GET /api/health` from a client that is not a browser answers under any name (container health checks). Names any device on the network can answer for — other names ending in `.local`, `.lan`, `.internal` or `.home.arpa`, and other single-word names — are refused unless listed. To reach it under any other name, such as a reverse proxy's domain or a NAS's name, list that name in `VISUALWEAVER_ALLOWED_HOSTS` (comma-separated; `*.example.com` matches subdomains; `*` turns the check off); `deploy/docker-compose.https.yml` sets it next to the Caddy domain. Behind a reverse proxy the `Host` header VisualWeaver receives must be an allowed name; a proxy that replaces it with its upstream address passes the public name in `X-Forwarded-Host`, which is used only on a connection from a trusted proxy address (`auth.trusted_proxies` / `VISUALWEAVER_TRUSTED_PROXIES`, the loopback addresses when unset — with sign-in on or off) and must be an allowed name too.

### Group 5 — Diagnostics

#### 📊 Analytics
Session-level cache hit/miss rates, latency histogram, message statistics, and per-instance RAG performance table.

#### 📋 Logs
Every message raised this session (see [Notifications](#notifications)), then the last 200 ingestion events — filterable by status, instance, or source URL.

### Unsaved changes

The panel mixes two kinds of control, and says which is which:

- **Staged until you press Save Settings** — API keys, RAG and cache tuning, web sources, watched folders, prompt templates, crawl defaults, a **Reset Defaults**, and the provider saved as the default
- **Applied as you change them** — RAG instances and storage connections. The sections that behave this way say so in place

Editing a staged control marks the panel **Unsaved changes**, and closing it then — with Cancel, ✕, Escape or a click outside — asks, and **Discard changes** puts every staged edit back. A provider or model you switch to stays selected in this browser either way; saving also makes it the default for other browsers.

<p align="center">
  <img src="screenshots/features/unsaved-settings.png" alt="A dialog asking whether to discard unsaved settings, explaining which changes are staged and which were already applied" width="62%">
</p>

### Sign-in through a reverse proxy (multiple users)

Sign-in is **off by default**: VisualWeaver is then the single-user app described everywhere else in this document. Turned on, an authenticating reverse proxy — [Authelia](https://www.authelia.com), [Authentik](https://goauthentik.io) or [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) — signs people in, and passes the signed-in user to VisualWeaver in a request header. VisualWeaver keeps no passwords of its own.

**Settings.** The `auth` section of `config.json`, or environment variables (an environment variable wins). Read when the app starts — restart after a change. A Settings save or a config import never changes it.

| `config.json` key | Environment variable | Default | Meaning |
|---|---|---|---|
| `auth.mode` | `VISUALWEAVER_AUTH` | `off` | `header` turns sign-in on |
| `auth.user_header` | `VISUALWEAVER_AUTH_USER_HEADER` | `Remote-User` | The header holding the user name. Authelia: `Remote-User`; Authentik: `X-Authentik-Username`; oauth2-proxy: `X-Forwarded-Email` (or `X-Forwarded-User`). User and group names are compared exactly (`Root` is another user than `root`; see `auth.case_insensitive_names`); a name that Unicode compatibility normalisation (NFKC) changes — look-alike characters such as `K` (Kelvin sign) or fullwidth letters — is refused |
| `auth.groups_header` | `VISUALWEAVER_AUTH_GROUPS_HEADER` | `Remote-Groups` | The user's groups, separated by `,` or `\|`. Authentik: `X-Authentik-Groups`; oauth2-proxy: `X-Forwarded-Groups` |
| `auth.name_header` | `VISUALWEAVER_AUTH_NAME_HEADER` | `Remote-Name` | Optional display name shown in the sidebar. Authentik: `X-Authentik-Name`; oauth2-proxy: `X-Forwarded-Preferred-Username` |
| `auth.trusted_proxies` | `VISUALWEAVER_TRUSTED_PROXIES` (comma-separated) | `127.0.0.1`, `::1` | Addresses or CIDR ranges of the proxy. The headers above (and `X-Forwarded-Host`) are believed **only** on a connection from one of these. A range is written with its network address (`10.0.0.0/24`, not `10.0.0.5/24`); an IPv4-mapped form (`::ffff:10.0.0.5`) is the IPv4 address; a range wider than `/24` (IPv4) or `/64` (IPv6) is warned about at start |
| `auth.admin_users` | `VISUALWEAVER_ADMIN_USERS` (comma-separated) | none | User names with administrator rights |
| `auth.admin_groups` | `VISUALWEAVER_ADMIN_GROUPS` (comma-separated) | none | Groups whose members have administrator rights |
| `auth.case_insensitive_names` | `VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES` (`1`/`true`/`yes`/`on`) | `false` | `true` compares ASCII user and group names without regard to case (`Root` and `root` are then one user, also for `admin_users` and `admin_groups`); other characters stay exact. Turn it on only when your proxy itself treats names that differ only in case as the same account |

**Reach VisualWeaver only through the proxy.** Whoever can open a connection to VisualWeaver's port from a trusted proxy address can claim to be any user. So:

- with the proxy on the same machine, bind VisualWeaver to the loopback interface (`./changeAccess.sh local`, or `VISUALWEAVER_HOST=127.0.0.1`) and keep the default `trusted_proxies`;
- in Docker, do not publish VisualWeaver's port, give the proxy container a fixed address and list only that address (the examples in `deploy/signin/` do this);
- list only the proxy's own address in `trusted_proxies` — never a network your users' machines are on;
- if you start uvicorn yourself, add `--no-proxy-headers` (`start.sh` and the `visualweaver` command do): otherwise uvicorn reports the address from a proxy's `X-Forwarded-For` header instead of the proxy's own, and the proxy's requests are refused.

`start.sh` and the app's log say at every start whether sign-in is on, and warn when the app is bound to a network address while only the loopback is a trusted proxy (a proxy on another machine is then refused), when it is bound to a network address at all (make sure only the proxy can reach the port), and when no administrator is set (nobody can change Settings).

**What is refused.**

| Request | Answer |
|---|---|
| The user header from an address that is not a trusted proxy | `403`, logged with the address |
| No user header (from anywhere) | `401` — except `GET /api/health`, the liveness probe |
| The user header twice, an empty or over-long name, or a name NFKC normalisation changes | `400` |
| Any request while the `auth` settings cannot be used — an `auth` value that is not an object, an unknown `mode`, an address that does not parse, a range with host bits set, ranges that together cover every address (`0.0.0.0/0`, `::/0`, `0.0.0.0/1` + `128.0.0.0/1`) | `503` until the settings are fixed; `GET /api/health` alone answers `200` with `status: "misconfigured"` and the reason, so `start.sh`, `./docker.sh up` and the container health check see the app is running, and both scripts print **Sign-in: MISCONFIGURED** |
| Any request while `config.json` cannot be read, or cannot be parsed and it — or any `config.json.corrupt-…` copy beside it — contains an `auth` key in any letter case (also when `config.json` is missing while such a copy is beside it) | `503` until `config.json` is restored, or `VISUALWEAVER_AUTH` is set (`off` or `header`) — `GET /api/health` answers `200` with `status: "misconfigured"` as above; a Settings save made meanwhile keeps the `auth` section undecided. A `config.json` that cannot be parsed and has no `auth` key — an empty file or one cut short before its `auth` section included — is moved aside and the app runs on the built-in defaults, with `/api/health` reporting `degraded` and the log naming the kept copy; restore it rather than re-entering settings. `config.json` may be UTF-8, or UTF-8 or UTF-16 with a byte-order mark |
| A signed-in user who is not an administrator, on an administrator's endpoint | `403` |
| The chat WebSocket in any of these cases | the connection is refused |

Requests from another website and unknown host names are refused before any of this, as described under [Security](#-security).

**Administrators.** A user listed in `admin_users`, or a member of a group in `admin_groups`. Only an administrator can save Settings, export or import the config, move the app data, add or remove storage connections, create, delete, reset, enable/disable, import, export and deduplicate knowledge bases, ingest files, text and web pages, remove documents, manage web sources and re-crawls, clear the answer cache, reset the analytics counters, pull or delete Ollama models, test unsaved API keys and connections, read the ratings and the ingestion log, and open the API documentation (`/docs`). Every signed-in user can chat with any knowledge base, attach files, manage their own conversations, rate answers, remove a cached answer they were given, and see Status, Analytics and About (`GET /api/config` then leaves out the `auth` section, the storage hosts and connection strings, the web sources, scheduled re-crawls and watched folders, and the providers' addresses) — in Settings, someone who is not an administrator sees only those three tabs and no Save button; the other tabs are not shown at all, and a link to one of them opens Status. For them the [Get started checklist and the status strip](#first-run) say *ask your administrator* where an administrator gets a button into Settings, and an answer has no **💾 Keep this answer**. The sidebar shows who is signed in, with an **admin** badge for administrators.

**Conversations belong to the user who started them** (recorded as `owner`). Listing, opening, renaming, pinning, forking, deleting and continuing a conversation — over the chat WebSocket and `POST /api/chat` with a `session_id` as well — only ever reach the caller's own; another user's conversation id is answered exactly like an id nobody has used. The sidebar lists all of a user's conversations, in every browser they sign in from. Conversations stored before sign-in was turned on have no owner and are listed for administrators only.

**Knowledge bases are shared.** Every signed-in user can search every knowledge base, so their documents reach everyone through answers, citations and the document lists.

**The answer cache.** Every answer is cached for its user only, and replayed only to them; answers cached while sign-in was off are not used. A user removes only cached answers given to them (**Re-run fresh** replaces only their own); an administrator can remove any. The cached questions themselves (Settings → Cache) are shown to administrators only; everyone else sees how many there are.

**Ratings** record the signed-in user as `user`.

#### Examples (`deploy/signin/`)

| File | What it is |
|---|---|
| `docker-compose.signin.yml` | VisualWeaver, Redis and Caddy with HTTPS; Caddy at the fixed address `172.31.240.10`, the only trusted proxy; VisualWeaver's port not published. Add your Authelia (or Authentik) service to it |
| `Caddyfile.authelia` | Caddy `forward_auth` to Authelia (`authelia:9091`, `/api/authz/forward-auth`) |
| `Caddyfile.authentik` | Caddy `forward_auth` to an Authentik outpost (`authentik-outpost:9000`) |
| `docker-compose.oauth2-proxy.yml`, `oauth2-proxy.cfg` | oauth2-proxy as the reverse proxy, for any OIDC provider; oauth2-proxy at the fixed address `172.31.241.10` |

The VisualWeaver settings for Caddy + Authelia, as in `docker-compose.signin.yml`:

```bash
VISUALWEAVER_AUTH=header
VISUALWEAVER_TRUSTED_PROXIES=172.31.240.10      # Caddy's address, and nothing else
VISUALWEAVER_AUTH_USER_HEADER=Remote-User
VISUALWEAVER_AUTH_GROUPS_HEADER=Remote-Groups
VISUALWEAVER_AUTH_NAME_HEADER=Remote-Name
VISUALWEAVER_ADMIN_GROUPS=vw-admins             # and/or VISUALWEAVER_ADMIN_USERS=alice
VISUALWEAVER_ALLOWED_HOSTS=visualweaver.example.com
```

The Caddy site removes any copy of the identity headers a browser sent, asks Authelia, and only then proxies — `route` keeps that order:

```caddyfile
visualweaver.example.com {
	route {
		request_header -Remote-User
		request_header -Remote-Groups
		request_header -Remote-Name
		request_header -Remote-Email
		forward_auth authelia:9091 {
			uri /api/authz/forward-auth
			copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
		}
		reverse_proxy visualweaver:8420
	}
}
```

For Authentik use `Caddyfile.authentik` and `VISUALWEAVER_AUTH_USER_HEADER=X-Authentik-Username`, `VISUALWEAVER_AUTH_GROUPS_HEADER=X-Authentik-Groups`, `VISUALWEAVER_AUTH_NAME_HEADER=X-Authentik-Name`. For oauth2-proxy: `VISUALWEAVER_AUTH_USER_HEADER=X-Forwarded-Email`, `VISUALWEAVER_AUTH_GROUPS_HEADER=X-Forwarded-Groups`, `VISUALWEAVER_TRUSTED_PROXIES=172.31.241.10`, with `pass_user_headers = true` in `oauth2-proxy.cfg`.

A local install with Caddy on the same machine needs only, in `config.json`:

```json
"auth": { "mode": "header", "admin_users": ["alice"] }
```

with VisualWeaver bound to `127.0.0.1` (`./changeAccess.sh local`) and the Caddy site proxying to `127.0.0.1:8420`.

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Enter` | Send message |
| `Shift+Enter` | New line in input |
| `⌘/Ctrl+Enter` | Send message |
| `⌘/Ctrl+K` | Open Settings |
| `⌘/Ctrl+Shift+F` | Search conversations |
| `⌘/Ctrl+F` | Your browser's own find — VisualWeaver does not intercept it |
| `Esc` | Close panel |

---

## Keeping an answer

Answers are otherwise tied to where they appeared: the semantic cache expires on `cache.ttl`
(one hour by default), the conversation goes when it is deleted or its
[retention](#session-management) runs out, and **🔖 Bookmark** on a message lasts only until the
page is reloaded.

**💾** on any answer opens a review dialog and then indexes the answer into a RAG instance,
where it has no expiry and is retrievable in later answers.

- **What is stored.** The question and the answer together (the question carries the
  wording a later search will match on), plus the sources the answer cited, as a markdown
  list. Everything is editable before saving.
- **Where.** A dedicated `saved-answers` instance, created on first use; pick a different
  one in the dialog. A saved answer is retrieved and cited *exactly* like a source document,
  and being phrased in the language of the question it can rank above the document it came
  from — keeping it in its own instance means it can be switched off from the top-bar
  selector when an answer must be grounded only in real sources.
- **Undoing it.** Each saved answer is one document named
  `answer://<date> <title>`, listed under **Settings → RAG → Documents** and deletable on
  its own.
- **Re-saving.** Chunks are deduplicated per source, so saving an unchanged answer under
  the same title again stores nothing and says so.

<p align="center">
  <img src="screenshots/tutorial/10c-keep-answer.png" alt="The Keep this answer dialog with editable title, question and answer, a toggle to append the cited sources, and a knowledge-base selector" width="66%">
</p>

---

## Analytics

Open **Settings → Analytics** for live performance statistics.

### Session-level metrics

- **Queries this session** — messages sent from this browser tab since the page was loaded
- **Cache hits this session** — how many of those were served from the semantic cache
- **Hit rate this session** — cache hits / queries
- **Avg latency this session** — mean end-to-end response time
- **Latency Distribution** — histogram across five buckets: <200ms, 200–500ms, 500ms–1s, 1–3s, >3s

### RAG Performance — per instance

| Column | Description |
|---|---|
| **Queries** | Total searches run against this instance |
| **Hits** | Queries that returned ≥1 chunk above the similarity threshold |
| **Hit Rate** | Hits / Queries — colour-coded (green ≥80%, yellow ≥50%, red <50%) |
| **Avg Score (hits)** | Mean cosine similarity of the top-1 chunk on hit queries |
| **Avg Best Raw** | Mean cosine similarity of the top-1 KNN result before threshold filtering |
| **Avg Chunks** | Mean number of chunks returned per query |

#### Diagnosing low hit rate

If **Avg Best Raw** is high (e.g. 0.65+) but **Hit Rate** is low, your `similarity_threshold` is too strict. Lower the threshold in **Settings → RAG**.

What counts as "strict" depends on the embedding model, and the useful range is lower than it looks. With `intfloat/multilingual-e5-small`, genuinely relevant question↔passage pairs typically score **0.35–0.75**; a threshold of 0.75 clears almost nothing. If the vector half of hybrid search appears to contribute little — or turning `hybrid_search` off returns nothing at all — the threshold is the first thing to lower.

#### Reranking

With reranking enabled (**Settings → RAG → Rerank retrieved chunks**), a cross-encoder re-scores the retrieved candidates before they reach the model. Because a reranker can only improve on the original ordering if it is given more candidates than you intend to keep, VisualWeaver widens retrieval to `rerank_candidates` (default 40) whenever reranking is on, then cuts back to `top_k` afterwards. With the reranker off, retrieval fetches only `top_k` and no extra work is done. The cross-encoder runs in full precision, so its scores are the same on a CPU and a GPU. Setting `reranker.half_precision` to `true` in `config.json` runs it in half precision on a GPU (CUDA or Apple silicon) — faster, but the scores change slightly (by up to about 0.007), so two candidates that close can swap places; on a CPU it stays in full precision.

A **⚠ threshold?** warning pill appears automatically on any instance where raw score ≥ 0.60 but hit rate < 50%.

### Token Usage — all-time

The **🔢 Token Usage** card is the running total across **every** conversation, not just the one on screen — the top-bar pills are per-conversation and reset when you switch. One row per provider and model:

| Column | Description |
|---|---|
| **Input** | Fresh input tokens |
| **Cached** | Prompt-cache reads |
| **Cache write** | Prompt-cache creation |
| **Output** | Generated tokens |

The four columns are disjoint, so they add up to the model's total. **Cached** and **Cache write** appear only when a provider reports them — currently Claude; other providers show just Input and Output.

Rows are ordered by total tokens, heaviest first.

VisualWeaver reports tokens and no money. Rates differ by provider, by model and over time, and change without notice, so any figure the app derived would be a guess presented as an amount — work the cost out from these counts against your provider's own bill.

Counts come from the provider and only from answers it actually produced. Cache hits and stopped answers never reach a provider, so they are absent here — which is why the sum of the top-bar pills across your conversations will read higher than this total.

**↺ Reset tally** zeroes the counters after a confirmation. Conversations and their stored per-turn counts are not affected; only the cumulative total is cleared. **⬇ Export CSV** at the foot of the tab includes these rows.

---

## API Reference

All endpoints are served at `http://localhost:8420`. With [sign-in](#sign-in-through-a-reverse-proxy-multiple-users) on, every endpoint but `GET /api/health` needs the proxy's user header, and the ones that change settings, storage or knowledge bases need an administrator.

### Sign-in

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/me` | Who is signed in: `{"auth": true, "user": "<display name>", "id": "<user name>", "admin": false}`. With sign-in off: `{"auth": false, "user": null, "id": null, "admin": true}` |

### Config

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config` | Fetch runtime configuration |
| `POST` | `/api/config` | Save configuration |
| `GET` | `/api/config/export` | Download config as JSON file |
| `POST` | `/api/config/import` | Upload and replace config |

### Chat

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/chat` | Non-streaming chat — same semantics as the WebSocket handler, for clients that don't use WebSockets. Body: `content` (required), plus optional `session_id`, `provider`, `model`, `system_prompt`, `rag_instance`/`rag_instances`, `source_filter`, `images` (base64 data URIs), `file_context`, `use_cache`, `documents_only` (`true` = answer only from the documents, see [Documents only](#documents-only)), and `kb_select`, `template`, `project` (stored with the conversation, as on the WebSocket). Returns `{session_id, response, chunks, cache_hit?, rag_used?, documents_only?, not_found?, search_failed?, reused_sources?}` — `not_found: true` is the fixed "not in your documents" answer, given without calling the model; `search_failed: true` means the knowledge-base search failed (with Documents only, `response` is then the fixed "Search failed: …" answer); `reused_sources: true` marks a Documents only follow-up answered from the previous answer's sources |
| `POST` | `/api/chat/upload-file` | Extract text from an attached file (TXT/MD/CSV/PDF/DOCX/XLSX) for the next chat turn — not stored or indexed. Multipart `file`; returns `{filename, chars, truncated, text}` |

### Files

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/files/image?path=...` | Serve an image a tool wrote for an answer. Only files inside the `images` folder of the data directory are served (symlinks and `..` resolved first); any other path answers 404, whether or not the file exists. `path` is percent-encoded. A tool result naming an image anywhere else is shown as the raw tool call |

### Status

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | App status: `{status, app, version, license, source, provider, services: {...}}`. `status` is `"degraded"` only if `config.json` failed to parse and the app runs on the defaults. While sign-in is misconfigured, or a damaged `config.json` configured it, this is the only endpoint that answers: `200` with just `{status: "misconfigured", app, detail, license, source}` (`detail` is the reason); every other request is `503` — Redis reachability is checked separately below |
| `GET` | `/api/status/redis` | Ping the built-in connection and refresh every connection's health: `{ok, type, version, search_available, endpoint_health}` (`type` is `redis` or `mongodb`) |
| `GET` | `/api/status/redis/{endpoint}` | Ping a named connection (`default` is the built-in one) |
| `GET` | `/api/status/ollama` | Test Ollama server — `{ok, models}` (how many models it lists), or `{ok: false, error}` when it cannot be reached |
| `GET` `POST` | `/api/status/claude` | Verify the Claude API key. `GET` checks the configured key; `POST {"key": "..."}` tests an unsaved one — the key travels in the body so it never reaches an access log or browser history |
| `GET` `POST` | `/api/status/openai` | Verify the OpenAI API key. `GET` checks the configured key; `POST {"key": "..."}` tests an unsaved one — the key travels in the body so it never reaches an access log or browser history |
| `GET` `POST` | `/api/status/qwen` | Verify the Qwen API key. `GET` checks the configured key; `POST {"key": "..."}` tests an unsaved one — the key travels in the body so it never reaches an access log or browser history |
| `GET` `POST` | `/api/status/mistral` | Verify the Mistral API key. `GET` checks the configured key; `POST {"key": "..."}` tests an unsaved one — the key travels in the body so it never reaches an access log or browser history |
| `GET` `POST` | `/api/status/groq` | Verify the Groq API key. `GET` checks the configured key; `POST {"key": "..."}` tests an unsaved one — the key travels in the body so it never reaches an access log or browser history |
| `GET` `POST` | `/api/status/gemini` | Verify the Gemini API key. `GET` checks the configured key; `POST {"key": "..."}` tests an unsaved one — the key travels in the body so it never reaches an access log or browser history |

All six key-based provider checks answer `{ok: true}` or `{ok: false, error}`. When no key
is set at all they add `configured: false`, which distinguishes a provider that was never
set up from one that is set up and failing.

### Models

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/ollama/models` | List Ollama models (vision-detected) |
| `POST` | `/api/ollama/pull` | Pull a model onto the Ollama server (`{"model": "..."}`; NDJSON progress stream) |
| `DELETE` | `/api/ollama/models/{name}` | Remove a model from the Ollama server |
| `GET` | `/api/claude/models` | List Claude models |
| `GET` | `/api/openai/models` | List OpenAI models (live + static fallback) |
| `GET` | `/api/qwen/models` | List Qwen models |
| `GET` | `/api/mistral/models` | List Mistral models |
| `GET` | `/api/groq/models` | List Groq models (live + static fallback) |
| `GET` | `/api/gemini/models` | List Gemini models |
| `GET` | `/api/embedding/models` | List the embedding-model registry: `{models: [{id, field, active, ...}], active_id, default_id}`. Distinct from `/api/rag/{instance}/embedding-models` below, which shows models actually in use by one instance's chunks |

### Storage Connections

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/redis/endpoints` | List the built-in connection (`default`, Redis or MongoDB) and every other connection, each with `type` (`redis` or `mongodb`) and `builtin`, plus `host` for MongoDB; passwords redacted |
| `POST` | `/api/redis/endpoints` | Add or replace a connection — `{name, type:"redis", host, port, db, password, ssl}` or `{name, type:"mongodb", uri, database, username, password}`. A built-in connection keeps its type |
| `DELETE` | `/api/redis/endpoints/{name}` | Remove a connection. `400` for a built-in connection and for the connection holding the app data |
| `POST` | `/api/redis/test` | Test values not yet saved (either body shape above) — returns `{ok, version, search_available, error}` without touching config |
| `POST` | `/api/redis/endpoints/{name}/test` | Test a saved connection |
| `GET` | `/api/redis/endpoints/{name}/discover` | List RAG instances already stored on a connection that are not yet registered |
| `POST` | `/api/redis/endpoints/{name}/register` | Register discovered instances found by `discover` — body `{instances: [...]}` |
| `GET` | `/api/redis/memory` | Memory usage of the built-in connection; for a built-in MongoDB, `{type:"mongodb", used, used_human, data, data_human}` (storage and data size) |
| `GET` | `/api/redis/all-stats` | Per-connection stats for the Analytics panel — memory, clients, keyspace, cluster and replication for Redis; size and object count for MongoDB; and each connection's RAG instances |

### Storage

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/storage` | `{app, default_type, connections: [{name, type, builtin, label, host, version, ok, vector_search, is_app, instances}]}` — every connection: the built-in `default`, the other built-in ones, then the added ones. `label` is `Built-in Redis` / `Built-in MongoDB` for built-in connections. `ok` comes from the last health check (a connection not checked yet is checked on the spot); `vector_search` and `version` are `null` when the connection cannot be reached; `instances` counts the RAG instances it holds |
| `POST` | `/api/storage/app` | Keep the app data on another connection — `{connection}`, optionally `"copy": false` to switch without copying (for a previous store that is gone). Returns `{ok, app, copied: {conversations, usage, stats, watch}}`; `copied` is `null` when the app data was already there or nothing was copied. `400` with a `detail` for an unknown connection, one that cannot be reached, one without vector search, or a copy that failed (nothing is switched then) |
| `GET` | `/api/components` | What this installation runs: `{app: {name, version, python}, storage: [{connection, label, type, builtin, ok, product, version, search, error?}], libraries: [{name, version}], runtime?: {containers}}`. `search` names the search engine and its version where known (`Redis Query Engine 8.8.0`, `MongoDB Search 1.78.0`, `Atlas Vector Search`), `available`, or `not available`; `runtime` appears when a built-in MongoDB exists. Results are reused for a minute |

### RAG Instances

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rag/instances` | List all instances across all endpoints |
| `POST` | `/api/rag/instances` | Create instance (`{name, color, endpoint}` — `endpoint` names the storage connection, default `default`; the older key `redis_endpoint` is also accepted). Names are unique across connections |
| `DELETE` | `/api/rag/instances/{instance}` | Delete instance and all chunks |
| `POST` | `/api/rag/{instance}/toggle` | Enable / disable |
| `POST` | `/api/rag/{instance}/reset` | Clear chunks, keep instance |
| `POST` | `/api/rag/{instance}/optimize` | Remove exact-duplicate chunks |
| `GET` | `/api/rag/{instance}/chunks` | Preview stored chunks |
| `GET` | `/api/rag/{instance}/documents` | List indexed documents: `{total, documents: [{source, doc_id, chunks, ingested_at}]}` — the basis of the Documents panel |
| `DELETE` | `/api/rag/{instance}/documents?source=...` | Remove every chunk belonging to one source document (optional `endpoint`). Its dedup hashes are released, so the same document can be re-ingested afterwards |
| `GET` | `/api/rag/{instance}/sources` | Sorted list of unique source identifiers in the instance |
| `GET` | `/api/rag/{instance}/embedding-models` | Which embedding model(s) this instance's chunks were built with — `{models: [{id, chunks, label}], mixed}`; `mixed: true` if chunks span more than one model |

### Ingestion

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/rag/{instance}/ingest/files` | Upload and ingest files (returns JSON array) |
| `POST` | `/api/rag/{instance}/ingest/files/stream` | Upload and ingest files with SSE progress. First event is `{job, total}` — pass `job` to `/api/ingest/cancel` to stop it. A `{stage: "model", message}` event precedes the first file while the embedding model is being loaded. Per-file events carry `{file, status, chunks, error, index, total}`, where `status` is `ok`, `skipped` or `error` — a file the indexer *reports* as failed (an unsupported type, a scanned PDF with no extractable text) counts towards `errors`, not `ok`. Enforces `VISUALWEAVER_MAX_UPLOAD_MB` per file, as the non-streaming route does |
| `POST` | `/api/rag/{instance}/ingest/url` | Crawl URL (non-streaming) |
| `GET` | `/api/rag/{instance}/ingest/url/stream` | Crawl with SSE progress. Each event carries `{url, status, chunks, error, pages_done, discovered, queued, resolved}`. `discovered` is how many URLs the frontier has admitted and `resolved` how many have reached a terminal state (indexed, skipped, blocked or errored), so progress can be shown when `max_pages` is `0`. Divide `resolved` by `discovered`, not `pages_done` — a URL is admitted before the robots, already-indexed and duplicate checks, any of which end it without an index |
| `POST` | `/api/rag/{instance}/ingest/text` | Index a block of text directly — body `{text, source}`. `source` is the label the Documents view groups on and the per-document delete addresses, so anything stored here can be found and removed on its own. Returns `{chunks, duplicate}`; `duplicate: true` with `chunks: 0` means every chunk was already stored under that same source. Creates the instance's index if it does not exist. Same size cap as the file routes |
| `GET` | `/api/rag/logs` | Last 200 ingestion events |
| `GET` | `/api/ingest/active` | List running and recently-finished file-ingest jobs: `[{job, instance, endpoint, total, index, current, files, ok, errors, started, done, cancelled, start_ts}]`. `started: false` is a job whose stream was dropped before it began — it is expired automatically and should not be attached to. Like `/api/crawl/active`, this reflects the app's no-authentication posture: it names the files being indexed to anyone who can reach the port |
| `POST` | `/api/ingest/cancel` | Stop a running file ingest — body `{job}`. Stops before the next file; files already indexed are kept. `404` if no job with that id is running |

### Crawler Control

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/crawl/active` | List active and recently-finished crawl states: `{url, instance, pages_done, discovered, queued, resolved, max_pages, chunks, errors, blocked, skipped, paused, done, params}`. Keyed on the fragment-stripped seed URL, which is how `/api/crawl/pause` and `/api/crawl/cancel` address a crawl |
| `POST` | `/api/crawl/pause` | Pause/resume a running crawl by URL — body `{url, paused}` (default `paused: true`). Visited pages, the queue, and indexed chunks are kept |
| `POST` | `/api/crawl/cancel` | Cancel a running crawl by URL — body `{url}`. Unlike pause, the crawl task is stopped for good |

### Scheduled Re-crawl

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/recrawl/sources` | List scheduled sources: `[{url, instance, depth, last_crawled}]` |
| `POST` | `/api/recrawl/sources` | Schedule a URL for periodic re-crawl — body `{url, instance, depth}`. Re-adding an existing URL replaces its entry |
| `DELETE` | `/api/recrawl/sources?url=...` | Remove a URL from the schedule |
| `POST` | `/api/recrawl/trigger` | Re-crawl every scheduled source immediately, ignoring `interval_minutes` |

#### SSE file ingest events

```jsonc
// Per-file progress
{ "file": "manual.pdf", "status": "ok", "chunks": 128, "index": 0, "total": 3 }
{ "file": "faq.txt",    "status": "ok", "chunks": 42,  "index": 1, "total": 3 }
{ "file": "broken.csv", "status": "error", "error": "...", "index": 2, "total": 3 }

// Completion sentinel
{ "done": true, "total": 3 }
```

### Export / Import

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rag/{instance}/export` | Download ZIP (chunks + embeddings) |
| `GET` | `/api/rag/{instance}/export/stream` | Download the same export as newline-delimited JSON instead of a ZIP — one `{"_t":"meta",...}` line, then one `{"_t":"chunk",...}` line per chunk, then `{"_t":"done"}` |
| `POST` | `/api/rag/{instance}/import` | Upload ZIP into instance |

### Cache

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/cache/stats` | `{count, entries: [{query, ttl}]}`. With sign-in on, `entries` and `count` are the signed-in user's own; administrators also get `total` and `by_user` (`[{user, count}]`, `user` an anonymous tag, `""` for answers cached before sign-in) — counts only |
| `DELETE` | `/api/cache` | Clear all cached responses |
| `DELETE` | `/api/cache/entry?entry_id={id}` | Delete a single cache entry by ID |

**`DELETE /api/cache/entry`** — removes a specific cached response without touching any other entries. With sign-in on, a user who is not an administrator removes only an entry cached for them; any other id answers `{"ok": false}`. The `entry_id` is included in every `cache_hit` WebSocket message and stored as `data-entry-id` on cached message elements.

```jsonc
// Request
DELETE /api/cache/entry?entry_id=abc123

// Response
{ "ok": true }
// or on failure:
{ "ok": false, "error": "entry_id required" }
```

### RAG Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rag/advice` | Per-setting verdicts for the RAG controls, derived from this deployment's own retrieval history — the distribution of pre-threshold scores and which chunk ranks answers cited. Returns `{advice, presets}`. A setting with no measurement behind it is `unknown` or `static` rather than scored. `?project=<id>`: measured on that project's knowledge bases only (all of them for a project without its own), adding `project: {id, name, kbs}`; `404` for an unknown project |
| `GET` | `/api/rag/stats` | Per-instance query statistics |
| `DELETE` | `/api/rag/stats` | Reset all counters |

### Feedback

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/feedback?limit=200&value=up` | List thumbs ratings, newest first — `value` filters to `up`/`down` (or raw `1`/`-1`) |
| `POST` | `/api/feedback` | Record a rating — free-form body (text fields cut to 8,000 characters; at most 64 KB as JSON and 5 levels deep, else `413`); used by 👍/👎 on an answer |

### Sessions

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/sessions` | List conversations (with sign-in on: the caller's own), newest first: `id`, `title`, `messages`, `preview`, `created`, `updated` (epoch seconds), `model` and `provider` of the latest answer, `kbs` (knowledge bases asked), `pinned`, `expires` (epoch seconds; `null` while it is kept), and when set `project`, `kb_select` (the conversation's selection) and `template` |
| `PATCH` | `/api/sessions/{sid}` | Rename, pin, file under a project and/or change the knowledge-base selection: `{"title": "…"}`, `{"pinned": true}`, `{"project": "<project id>"}` (`null`: no project), `{"kb_select": "all" \| "none" \| ["kb", …]}` → the updated row. A pinned conversation never expires |
| `GET` | `/api/sessions/{sid}` | Fetch message history |
| `DELETE` | `/api/sessions/{sid}` | Delete session |
| `POST` | `/api/sessions/{sid}/fork` | Fork at a message (`{"role","content_prefix","occurrence"}`) → new session id |
| `GET` | `/api/usage` | All-time provider-reported token usage, as `{"provider:model": {in, out, cached, cache_write}}`. The four counts are disjoint: `in` is fresh input, `cached` prompt-cache reads, `cache_write` cache creation, `out` generated tokens. `cached` and `cache_write` appear only for providers that report them |
| `DELETE` | `/api/usage` | Reset the all-time tally to zero. Conversations and their per-turn counts are untouched |

### Projects

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/projects` | List projects by name: `id`, `name`, `icon`, `color`, `kbs`, `template` (a template name), `provider`, `model`, `documents_only` (`true`/`false`, `null` = the Settings default), `created`, `updated` |
| `POST` | `/api/projects` | Create one: `{"name": "…"}` plus any of the other fields → the project. `400` for a field that cannot be stored (name 1–60 characters; `color` `#rrggbb`; `icon` up to 8 characters; up to 50 knowledge-base names), `409` for a name already in use (ignoring case) |
| `PATCH` | `/api/projects/{id}` | Change some of its fields → the project |
| `DELETE` | `/api/projects/{id}` | Delete it → `{"ok": true, "unfiled": n}`. Its conversations are kept, without a project, each with the time it had left |

With sign-in on, `GET` is open to every signed-in user and the others to administrators. A chat turn files a new conversation under a project with `project` (see the WebSocket below); `PATCH /api/sessions/{sid}` moves one.

### Templates

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/templates` | List templates |
| `POST` | `/api/templates` | Save templates |

### WebSocket

```
WS /ws/chat/{session_id}
```

**Client → Server messages:**

```jsonc
// Start a chat turn
{
  "content": "What is Redis?",
  "provider": "ollama",             // "ollama" | "claude" | "openai" | "qwen" | "mistral" | "groq" | "gemini"
  "model": "llama3.2",
  "system_prompt": "You are a helpful assistant.",
  "rag_instance": "product-docs",   // single instance
  // OR
  "rag_instances": ["docs", "kb"],  // parallel multi-instance
  "images": [],                      // base64 data URIs for vision
  "bypass_cache": false,             // true = skip cache lookup for this query
  "documents_only": false,           // true = answer only from the documents (JSON true only)
  // stored with the conversation (all optional):
  "kb_select": ["docs", "kb"],       // its knowledge-base selection: "all" | "none" | [names]
  "template": "Concise",             // its template, by name ("" = Default)
  "project": "p_0123456789ab"        // the project it starts in — first message only
}

// Abort current stream
{ "type": "abort" }
```

**Server → Client messages:**

```jsonc
{ "type": "status", "phase": "cache" }   // progress phase: "cache" | "rag" | "hyde"
{ "type": "stream_start" }
{ "type": "rag_context", "chunks": [...], "rag_used": true, "docs_only": false, "latency": { "rag": 0.12 } }
// a Documents only turn with nothing to answer from adds "not_found": true here and on stream_end;
// its fixed answer arrives as one token with "done": true. A knowledge-base search that failed
// adds "search_failed": true instead (Documents only: on stream_end too, with the fixed answer);
// a Documents only follow-up answered from the previous answer's sources adds "reused_sources": true
{ "type": "token", "content": "Hello", "done": false }
{ "type": "stream_end", "latency": { "cache": 0, "rag": 0.12, "llm": 1.4, "total": 1.52 }, "title": null }
{ "type": "session_title", "title": "Redis Overview" }   // deferred auto-title (first turn only)
{
  "type": "cache_hit",
  "content": "...",
  "score": 0.97,
  "entry_id": "abc123",             // BARE id; pass as-is to DELETE /api/cache/entry
  "latency": { "cache": 0.03, "total": 0.03 }
}
{ "type": "error", "content": "Connection refused" }
{ "type": "stream_end", "aborted": true, "latency": {} }
```

`rag_context` is sent after `cache_hit` when the cache entry contains stored RAG chunks, allowing the client to attach them to the cached message's inspector panel.

`stream_end` always carries `"title": null`; the auto-generated session title is delivered afterwards as a separate `session_title` message (only on a session's first turn). This lets the UI unlock the composer immediately instead of waiting for the extra title-generation call.

`bypass_cache: true` causes the server to skip the cache lookup for that single query. The resulting response is still written to the cache at the end.

---

## Architecture

```
Browser (index.html)
  │
  ├── WebSocket /ws/chat/{sid}  ←→  handle_chat()
  │     ├── Semantic cache lookup   (Redis FLAT index, KNN)
  │     │     └── cache_hit → re-emit stored RAG chunks
  │     ├── RAG retrieval           (Redis HNSW index, KNN + BM25 → RRF merge)
  │     │     └── search_rag_parallel() — asyncio.gather across endpoints
  │     └── LLM streaming           (Ollama / Claude / OpenAI / Qwen / Mistral / Groq / Gemini)
  │
  └── REST /api/*  ←→  FastAPI routes
        ├── Config CRUD
        ├── RAG instance management (create / toggle / reset / optimize)
        ├── File & URL ingestion    (batch + SSE streaming)
        ├── Export / Import
        ├── Cache management        (stats / clear all / delete single entry)
        └── Analytics (cache stats, RAG per-instance stats)

Redis (one or more servers)
  ├── rag:{instance}:idx           — HNSW vector index + TEXT field for BM25
  ├── rag:{instance}:chunk:{id}    — HASH: text, source, embedding
  ├── rag:{instance}:chunk_counter — INCRBY atomic ID counter
  ├── rag_meta:{instance}          — JSON metadata (color, endpoint, enabled)
  ├── visualweaver:rag_instances   — SET: the names of the instances on this database
  ├── semcache:idx                 — FLAT vector index for response cache
  └── semcache:{id}                — HASH: query, response, embedding, chunks_json, TTL
```

### Vector indexes

| Index | Type | Use case |
|---|---|---|
| `rag:{instance}:idx` | HNSW (M=16, EF_CONSTRUCTION=200) | RAG chunk retrieval — scales to ~1M chunks |
| `semcache:idx` | FLAT (brute-force) | Cache lookup — perfect recall on small sets |

### Cache metadata storage

Each cache entry (`semcache:{id}` HASH) stores:

| Field | Content |
|---|---|
| `query` | Original query text |
| `response` | Full LLM response |
| `embedding` | Query vector (for similarity search) |
| `chunks_json` | JSON-encoded RAG chunks retrieved during the original query |

When a cache hit occurs, `chunks_json` is decoded and re-emitted as a `rag_context` WebSocket message, so the client renders the same "See also" inspector panel as the original response.

### Hybrid search — RRF detail

```
query
  ├── embed(query) → KNN search  → vec_rows  [rank 0..N]
  └── keywords(query) → @text:() → bm25_rows [rank 0..N]

for each result key:
    rrf_score += 1 / (60 + rank)   ← applied separately per list

ranked by rrf_score desc → top_k returned
threshold filter applied to cosine score (not RRF score)
```

### Streaming abort

The WebSocket handler uses `asyncio.create_task` so the server can concurrently receive an `abort` message while streaming tokens. Cancelling the task raises `CancelledError` at the next `await`, cleanly stopping the LLM generator.

### Embedding model

Default: `intfloat/multilingual-e5-small` (384 dimensions, fast). Change in **Settings → General**.

> **Important:** Changing the embedding model requires re-indexing all existing RAG data — the vector dimensions will not match.

Available models:
- `intfloat/multilingual-e5-small` — fast, 384d
- `all-mpnet-base-v2` — more accurate, 768d
- `paraphrase-multilingual-MiniLM-L12-v2` — multilingual
- `BAAI/bge-base-en-v1.5` — high quality, English

Embeddings are computed locally by `sentence-transformers` (PyTorch). On the local install the device is whatever your PyTorch build supports — Apple Silicon uses `mps`, a CUDA machine uses the GPU.

**In Docker, embeddings run on CPU.** The image installs PyTorch from the CPU-only index on purpose: the default PyPI `torch` pulls NVIDIA CUDA runtime wheels that are proprietary (`LicenseRef-NVIDIA-SOFTWARE-LICENSE`), and redistributing those inside an AGPL-3.0 image would combine proprietary binaries with copyleft code. It also removes about 2 GB from the image. See [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

To use an NVIDIA GPU, install the CUDA build **on your own machine** rather than expecting it in the published image — nothing proprietary is then distributed by this project:

```bash
# pick the channel matching your driver: cu126 | cu128 | cu129 | cu130
docker compose exec visualweaver pip install --force-reinstall torch \
  --index-url https://download.pytorch.org/whl/cu129
```

The container also needs GPU access (`--gpus all`, or a `deploy.resources` reservation in compose), and the step must be repeated after every `docker compose pull` because a fresh image is CPU-only again. For a permanent GPU image, change the `pip install` line in the `Dockerfile` and build it yourself.

---

## Configuration File

`config.json` (in the platform data directory — see the README) is created on first run and updated via the Settings UI or the `/api/config` endpoint. The complete, always-current annotated template is [`config.example.json`](config.example.json); the abbreviated example below shows the common keys, and the keys with **no UI control** are listed under [Advanced options](#advanced-options-config-only) beneath it.

```jsonc
{
  "storage": { "default_type": "redis", "app": "default" },
  "redis": {
    "host": "localhost",
    "port": 6379,
    "db": 0,
    "password": "",
    "ssl": false
  },
  "redis_endpoints": [],
  "provider": "ollama",
  "ollama": { "host": "http://localhost", "port": 11434, "model": "" },
  "claude": {
    "api_key": "",
    "model": "claude-sonnet-4-6",
    "base_url": "https://api.anthropic.com"
  },
  "openai": {
    "api_key": "",
    "model": "gpt-4o",
    "base_url": "https://api.openai.com"
  },
  "qwen": {
    "api_key": "",
    "model": "qwen-plus",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
  },
  "mistral": {
    "api_key": "",
    "model": "mistral-small-latest",
    "base_url": "https://api.mistral.ai/v1"
  },
  "groq": {
    "api_key": "",
    "model": "llama-3.3-70b-versatile",
    "base_url": "https://api.groq.com/openai"
  },
  "gemini": {
    "api_key": "",
    "model": "gemini-3-flash-preview"
  },
  "embedding": { "model": "intfloat/multilingual-e5-small", "max_image_dim": 1024 },
  "rag": {
    "chunk_size": 180,
    "chunk_overlap": 32,
    "top_k": 5,
    "similarity_threshold": 0.35,
    "hybrid_search": true,
    "rerank_candidates": 40,
    "documents_only": false
  },
  "cache": { "enabled": true, "similarity_threshold": 0.92, "ttl": 3600 },
  "ui": {
    "theme": "auto",
    "show_rag_matches": false
  },
  "active_rag": "default",
  "base_instruction": "…prepended to every chat turn; blank = use the shipped default…",
  "prompt_templates": [
    { "name": "Default",      "system": "" },
    { "name": "Redis Expert", "system": "You are an expert in Redis..." }
  ],
  "web_sources": [],
  "security": { "enabled": false, "password": "" }
}
```

### Advanced options (config-only)

These keys have **no Settings-UI control** — edit `config.json` directly and restart. All are present in [`config.example.json`](config.example.json).

| Key | Default | What it does |
|---|---|---|
| `visual_instructions` | `true` | When `false`, drops the ~2,000-token chart/diagram authoring section from the base instruction — text-only, token-lean answers. (Also exposed as the Settings → Templates toggle.) |
| `history_max_tokens` | `3000` | Approximate token budget for the conversation history resent to the model each turn. A 20-message hard cap always applies; `0` disables the token cap. |
| `reranker` | `{ "enabled": false, "model": "cross-encoder/ms-marco-MiniLM-L-6-v2", "top_n": 5 }` | Optional cross-encoder reranking: when enabled, retrieval widens to `rag.rerank_candidates`, the cross-encoder re-scores, then results are trimmed to `top_k`. `"half_precision": true` (not in Settings) runs it in half precision on a GPU: faster, slightly different scores (see [Reranking](#reranking)). |
| `hyde` | `{ "enabled": false }` | HyDE (Hypothetical Document Embeddings): the model drafts a hypothetical answer and search uses *its* embedding, improving recall for sparse or keyword-poor queries. Costs one extra LLM call per query. |
| `auth` | `{ "mode": "off", … }` | Sign-in through a reverse proxy — see [Sign-in through a reverse proxy](#sign-in-through-a-reverse-proxy-multiple-users). Only read from `config.json` and the environment; Settings and config imports never change it. |
| `sessions` | `{ "persist": true, "ttl": 0 }` | Whether conversations persist to the app-data store, and how long each is kept after its last message, in seconds (`0` = forever; Settings → General → Conversations offers forever, 30 days and 1 day, and records `"retention_chosen": true`). A `ttl` of `86400` without `retention_chosen` — the earlier default, which every saved `config.json` holds — is read as `0`. Pinned conversations never expire. |
| `storage` | `{ "default_type": "redis", "app": "default" }` | `default_type`: whether the built-in `default` connection is the built-in Redis (`redis` section) or the built-in MongoDB (`mongodb` section: `uri`, `username`, `password`, `database`). `app`: the connection holding the app data and semantic cache. Written by the installer and by `POST /api/storage/app`; a settings save or config import leaves this section, `mongodb` and the built-in connections as they are. |
| `recrawl` | `{ "enabled": false, "interval_minutes": 60 }` | Scheduled automatic re-crawl of registered web sources, every `interval_minutes`. |
| `scheduled_sources` | `[]` | The web sources registered for scheduled re-crawl. |
| `crawl` | `{ "concurrency": 10, "js_render": false, "js_concurrency": 3, "smart_mode": true, "min_words": 100 }` | Web-crawler tuning — see [SETTINGS.md → Web Crawler Settings](SETTINGS.md#web-crawler-settings). |
| `rag.rerank_candidates` | `40` | How many candidates retrieval fetches for the reranker to choose from (only relevant when reranking is on). |
| `rag.documents_only` | `false` | Documents only for new conversations (Settings → RAG). The 📚 button switches it per conversation. |

### Storage from the environment

For a container deployment the storage can be set without editing `config.json`:

| Variable | Effect |
|---|---|
| `VISUALWEAVER_STORAGE` | `mongodb` — the built-in connection is the MongoDB below; `both` — the MongoDB below is the built-in connection `local-mongodb` (or, when the built-in store already is MongoDB, that built-in MongoDB); `redis` — the built-in Redis (`VISUALWEAVER_REDIS_HOST` / `VISUALWEAVER_REDIS_PORT`). Without `VISUALWEAVER_MONGODB_URI` it changes nothing: `config.json` describes the stores |
| `VISUALWEAVER_MONGODB_URI` | The MongoDB server or connection string. A username and password inside it are moved to their own fields |
| `VISUALWEAVER_MONGODB_USERNAME`, `VISUALWEAVER_MONGODB_PASSWORD` | The MongoDB user (override the ones in the URI). The password is never written to `config.json` |
| `VISUALWEAVER_MONGODB_DATABASE` | The database (default `visualweaver`) |

These apply to the run only: a settings save keeps `config.json`'s own storage values.

---

## Backup and Restore

What holds state depends on the storage you run:

| What | Docker | Local install | Contains |
|---|---|---|---|
| App data | the app container's `/data` volume | `$VISUALWEAVER_DATA_DIR` | `config.json`, uploads, ingestion logs, feedback |
| Redis | the `redis` container's `/data` volume | the dedicated Redis's data dir | chunks, vectors and indexes; conversations; the answer cache |
| MongoDB | the `mongodb` container's `/data/db` volume (+ its generated passwords) | the built-in MongoDB's `…_db` volume | the same, for what is stored on MongoDB |

A backup needs all of them. Restoring only a store leaves the app without API keys or
connections; restoring only the app data leaves it with no documents. MongoDB's
search indexes are not part of a backup: they are rebuilt from the database.

### Docker

```bash
./docker.sh backup                      # → ./visualweaver-backup-<date>/ (app, redis, mongodb, …)
./docker.sh restore ./visualweaver-backup-20261106-1500
```

`backup` stops the containers for a few seconds so the copy is consistent, saves each
volume through its container (so the compose project's volume names do not matter),
and starts them again. `restore` replaces the data, clears MongoDB's search indexes so
they are rebuilt, and starts everything; on a new machine it also restores the `.env`
with the storage choice.

### Local install

Stop VisualWeaver first so every store is written out (`./stop.sh` shuts Redis down
with a save and stops the built-in MongoDB cleanly), then copy:

```bash
./stop.sh
DATA="$HOME/Library/Application Support/VisualWeaver"     # Linux: ~/.local/share/visualweaver
tar czf appdata-backup.tgz -C "$DATA" .                    # config, uploads, logs — and redis/data,
                                                           # and mongodb/state.json (MongoDB's passwords)
docker run --rm -v visualweaver-mongodb_db:/src:ro alpine \
  tar czf - -C /src . > mongodb-backup.tgz                  # only if MongoDB is installed
./start.sh
```

The built-in Redis keeps its data in `redis/data` inside the data folder, so the first
archive already holds it. The built-in MongoDB keeps its database in the Docker volume
`visualweaver-mongodb_db` (`docker volume ls | grep visualweaver-mongodb` lists it; a data
folder other than the default one adds a short suffix to the name). Its search indexes
(`…_search`) are not needed: they are rebuilt from the database.

To restore, stop VisualWeaver, unpack `appdata-backup.tgz` into the data folder, unpack
`mongodb-backup.tgz` into the volume (`docker run --rm -i -v visualweaver-mongodb_db:/dst
alpine sh -c "find /dst -mindepth 1 -delete && tar xzf - -C /dst" < mongodb-backup.tgz`),
remove the search volume so it is rebuilt (`docker volume rm visualweaver-mongodb_search`
while MongoDB is stopped), and run `./start.sh`.

### Verifying a restore

`GET /api/health` returns `status: ok`, and `GET /api/rag/instances` lists each
instance with a non-zero chunk count. If chunk counts are zero while the keys
exist, the index did not rebuild — restart once and check the log for the schema
version line.

### What a backup does not protect

Chunks are stored with the vector produced by the embedding model configured at
ingest time. Restoring a backup onto an install with a different embedding model
leaves the vectors intact but unsearchable by the new model; VisualWeaver logs a
warning when it detects this. Re-ingest after changing models.

## Running and Maintenance

### Everyday commands

| | Local install | Docker |
|---|---|---|
| Start | `./start.sh` (or `./start.sh 9000` for another port) | `./docker.sh up` |
| Stop (data kept) | `./stop.sh` | `./docker.sh stop` |
| Restart | `./restart.sh` | `./docker.sh up` |
| What runs, versions | the start banner; **Settings → About** | `./docker.sh status`; **Settings → About** |
| Who can open it | `./changeAccess.sh local` / `lan`, then restart | `VISUALWEAVER_WEB_BIND=127.0.0.1` in `.env` (this machine only), then `./docker.sh up` |
| Back up | see [Backup and Restore](#backup-and-restore) | `./docker.sh backup` / `restore` |

`./start.sh` starts whatever `./install.sh` set up — the dedicated Redis, the built-in
MongoDB, or both — then the app, and prints the address and the versions in use.
`./stop.sh` stops the app first, then shuts Redis down with a save and stops MongoDB's
containers cleanly. Nothing is deleted by either.

For the built-in MongoDB, `./start.sh` (and `./install.sh`) also starts the container
runtime when it is installed but stopped — Colima, Docker Desktop, OrbStack, Rancher
Desktop or Podman, whichever the `docker` command uses — and waits until it answers.
`./stop.sh` reports each part as it goes down: Redis saved and stopped, MongoDB stopped
cleanly (or not, with its exit code), and the container runtime stopped again if
VisualWeaver started it and no other containers run in it
(`VISUALWEAVER_KEEP_RUNTIME=1` always leaves it running). It ends by checking that
nothing of VisualWeaver still answers.

After a reboot nothing starts on its own: run `./start.sh`. A Docker Engine daemon on
Linux is not started by the scripts (it needs `sudo systemctl start docker`).

### Ports

| Port (default) | What | Reachable from |
|---|---|---|
| 8420 | the web UI | this machine, or the network after `./changeAccess.sh lan` |
| 6389 | the dedicated Redis | this machine only |
| 27019 | the built-in MongoDB | this machine only (its search process is not published) |

`./install.sh` checks each port before using it: a port held by another program is
reported with the program's name and the next free one is used and recorded
(`VISUALWEAVER_PORT` in `.visualweaver.env`, `redis.conf`, MongoDB's `state.json`). On
every start, `./start.sh` checks the MongoDB port again: if another program has taken it,
MongoDB moves to the next free port and the app's connection follows. In Docker only the
web UI is published, and `./docker.sh up` moves it the same way.

### Logs

| Log | Where |
|---|---|
| The app | `<data folder>/log/visualweaver.log` (path printed by `./start.sh`) |
| The dedicated Redis | `<data folder>/redis/redis.log` |
| The built-in MongoDB | `venv/bin/python -m visualweaver.local_mongo logs` |
| Python dependencies (install) | `<data folder>/log/pip-install.log` |
| Docker | `./docker.sh logs [visualweaver|redis|mongodb|mongodb-search]` |

The data folder is `~/Library/Application Support/VisualWeaver` on macOS and
`~/.local/share/visualweaver` on Linux (`VISUALWEAVER_DATA_DIR` overrides it).

### The built-in MongoDB

`./install.sh` generates its passwords on first install and keeps them in
`<data folder>/mongodb/state.json` (readable by your user only). Three users sign in
against the `admin` database: `visualweaver` (the app — read/write on the `visualweaver`
database), `vwadmin` (administrator) and `mongotUser` (MongoDB's search process). To
connect with MongoDB Compass or `mongosh`, print an administrator connection string:

```bash
python3 -c "import json,os; s=json.load(open(os.path.expanduser('~/Library/Application Support/VisualWeaver/mongodb/state.json'))); print(f\"mongodb://vwadmin:{s['admin_password']}@127.0.0.1:{s['port']}/?authSource=admin&directConnection=true\")"
```

Do not change these passwords in MongoDB: the app and the search process read them from
`state.json`. `venv/bin/python -m visualweaver.local_mongo status` prints its state as
JSON (port, versions, whether search is ready, disk use).

**Disk space.** MongoDB's search process stops building indexes when the disk holding
them is over 85 % full, resumes below 80 %, and stops updating existing ones above 90 % —
searches then miss new documents without an error. That disk is the container runtime's
(on macOS, the Colima / Docker Desktop VM disk, which images fill up). `./start.sh`,
`./install.sh` and `./docker.sh` warn from 80 %; free space with `docker image prune` /
`docker builder prune`, or give the VM a larger disk.

### Where app data lives

Conversations, the usage tally, retrieval stats and the answer cache are kept on one
storage connection — the built-in Redis or MongoDB, or one you added. Move them under
**Settings → Storage → App data & answer cache** (see [Storage Connections](#storage-connections)).
RAG instances stay where they were created; move one by exporting it and importing it on
another connection.

### Upgrading

```bash
git pull
./install.sh      # updates the Python packages; keeps your storage, ports and data
./restart.sh
```

With Docker: `docker compose pull && ./docker.sh up` (or `./docker.sh up --build` to build
from the checkout). The MongoDB images are pinned by VisualWeaver and change only with it.

### Adding or removing a store

Run `./install.sh` again and answer **y** to add the store you do not have yet (or set
`VISUALWEAVER_STORAGE=both`); the existing one and its data stay. VisualWeaver never
removes a store. To remove the built-in MongoDB from an install that also has Redis:
move the app data to the built-in Redis and export or delete the instances stored on
MongoDB, stop VisualWeaver, run `venv/bin/python -m visualweaver.local_mongo down`, delete
the volumes (`docker volume rm visualweaver-mongodb_db visualweaver-mongodb_search`) and the
`mongodb` folder in the data folder, remove the `local-mongodb` entry from `config.json`,
set `VISUALWEAVER_STORAGE=redis` in `.visualweaver.env`, and start again.

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `./start.sh`: *its container runtime is not available* | The runtime could not be started here: none is installed, it did not start in time (its output is in `log/container-runtime.log` in the data folder), or it is a Linux daemon (`sudo systemctl start docker`). Start it, then `./start.sh`. |
| "… did not answer within 80 s — it is busy or stuck" | The container runtime is overloaded: wait and run `./start.sh` again; if it keeps happening, restart the runtime. |
| A new knowledge base on MongoDB finds nothing for a while | Its search index is still being built (seconds); if it stays that way, check the disk-space warning above. |
| Redis shows **search ?** or the Status tab says search is unavailable | Redis is still loading its data after a start (wait), or the search module did not load: run `./install.sh` again. |
| A browser shows *VisualWeaver does not answer browsers that reach it as '…'* | You opened it under a host name it does not know (for example a reverse proxy's domain): add that name to `VISUALWEAVER_ALLOWED_HOSTS` and restart (see [Settings Reference](#settings-reference) → Security). |
| Every request but `/api/health` answers `503` (which reports `misconfigured`) and the log says *config.json could not be loaded* | `config.json` cannot be read (permissions), or a damaged copy that configured sign-in is kept as `config.json.corrupt-…`: restore it and restart. To run without sign-in meanwhile, start with `VISUALWEAVER_AUTH=off`. |
| The log says *Refused a cross-site request* | A page on another site tried to use the API — expected. If it was VisualWeaver's own page behind a proxy that rewrites the Host header, forward the Host header or list the page's origin in `VISUALWEAVER_CORS_ORIGINS`. |
| In Docker, Ollama is not found | Ollama must listen on this machine (`ollama serve`); the container reaches it as `host.docker.internal`. For another machine set `VISUALWEAVER_OLLAMA_URL` in `.env`. |

## Optional Dependencies

Most of the packages below are **installed automatically** by `install.sh` and the Docker image (they're declared in `pyproject.toml`) — PDF/DOCX/XLSX extraction, web-content extraction, and the provider SDKs are all included by default. The app guards their imports with try/except so a hand-trimmed install still starts, but a standard install has them all. The only genuinely optional add-on is **`crawl4ai`** (JavaScript rendering for crawling SPAs), installed separately via `pip install '.[crawl]'`.

| Package | Install | Adds |
|---|---|---|
| `PyMuPDF` | `pip install PyMuPDF` | PDF text extraction |
| `trafilatura` | `pip install trafilatura` | High-quality HTML-to-text (best web crawl results) |
| `beautifulsoup4` | `pip install beautifulsoup4` | HTML parsing fallback |
| `anthropic` | `pip install anthropic` | Claude native SDK |
| `openai` | `pip install openai` | OpenAI / Qwen / Groq native SDK |
| `google-genai` | `pip install google-genai` | Gemini native SDK |

Without `trafilatura` and `bs4`, only plain-text and Markdown URLs can be crawled. Without `PyMuPDF`, PDF files are rejected at upload. Without provider SDKs, the corresponding provider is disabled with a clear error message.

The browser rendering libraries — marked (Markdown), DOMPurify (SVG sanitising), KaTeX (math), math.js (function graphs), and abcjs (music) — are loaded from CDN in `index.html`; no installation needed.

---

## Acknowledgments & License

VisualWeaver is built on open-source projects, each under its own license:

- **[Redis](https://redis.io)** *(AGPLv3)* — datastore + vector/query engine
- **Rendering** (all CDN-loaded on first use, never bundled) — [marked](https://marked.js.org) *(MIT)*, [DOMPurify](https://github.com/cure53/DOMPurify) *(Apache-2.0 / MPL-2.0)*, [KaTeX](https://katex.org) *(MIT)*, [math.js](https://mathjs.org) *(Apache-2.0)*, [Chart.js](https://www.chartjs.org) *(MIT)*, [Mermaid](https://mermaid.js.org) *(MIT)*, [Viz.js](https://github.com/mdaines/viz.js) *(MIT; embeds [Graphviz](https://graphviz.org) 15.1.1, EPL-2.0)*, [JSXGraph](https://jsxgraph.org) *(MIT or LGPL-3.0-or-later)*, [Leaflet](https://leafletjs.com) *(BSD-2-Clause)*, [Plotly.js](https://plotly.com/javascript/) *(MIT)*, [SmilesDrawer](https://github.com/reymond-group/smilesDrawer) *(MIT)*, [abcjs](https://www.abcjs.net) *(MIT)*, [highlight.js](https://highlightjs.org) *(BSD-3-Clause)*
- **Map data** — © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, [ODbL](https://opendatacommons.org/licenses/odbl/) (attribution rendered on every map)
- **Backend** — [FastAPI](https://fastapi.tiangolo.com)/[Uvicorn](https://www.uvicorn.org) *(MIT/BSD)*, [redis-py](https://github.com/redis/redis-py) & [RedisVL](https://github.com/redis/redis-vl-python) *(MIT)*, [NumPy](https://numpy.org) *(BSD)*, [sentence-transformers](https://www.sbert.net) *(Apache-2.0)*, [PyMuPDF](https://pymupdf.readthedocs.io) *(AGPLv3)*, [Trafilatura](https://trafilatura.readthedocs.io) *(Apache-2.0)*, [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) *(MIT)*, [Pillow](https://python-pillow.org) *(HPND)*, [httpx](https://www.python-httpx.org) *(BSD)*, [Crawl4AI](https://github.com/unclecode/crawl4ai) + [Playwright](https://playwright.dev) *(Apache-2.0)*, [python-docx](https://python-docx.readthedocs.io) & [openpyxl](https://openpyxl.readthedocs.io) *(MIT)*
- **LLM SDKs** — [Ollama](https://ollama.com), plus the [Anthropic](https://github.com/anthropics/anthropic-sdk-python) *(MIT)*, [OpenAI](https://github.com/openai/openai-python), [Groq](https://github.com/groq/groq-python), and [Google GenAI](https://github.com/googleapis/python-genai) *(Apache-2.0)* SDKs

VisualWeaver itself is licensed under **AGPL-3.0-or-later**. PyMuPDF and Redis 8 are AGPLv3; all other dependencies are permissive (MIT/BSD/HPND) or Apache-2.0, which are one-way compatible into AGPLv3 — so the combined work is cleanly licensable under the AGPL.
