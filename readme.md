<p align="center"><strong>VisualWeaver</strong></p>

<p align="center">Self-hosted retrieval-augmented chat over your own documents and websites, backed by Redis vector search.</p>

<p align="center">
  Self-hosted &bull; Browser-based &bull; Bring your own LLM &bull; AGPL-3.0
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg" alt="License: AGPL-3.0-or-later"></a>
  <a href="https://github.com/SFCyris/VisualWeaver/releases/latest"><img src="https://img.shields.io/github/v/release/SFCyris/VisualWeaver?include_prereleases&sort=semver" alt="Latest release"></a>
  <a href="https://github.com/SFCyris/VisualWeaver/pkgs/container/visualweaver"><img src="https://img.shields.io/badge/ghcr.io-visualweaver-2496ED?logo=docker&logoColor=white" alt="Docker image on GHCR"></a>
  <img src="https://img.shields.io/badge/python-3.11%20%E2%80%93%203.12-blue.svg" alt="Python 3.11 – 3.12">
</p>

<p align="center">
  <strong><a href="https://github.com/SFCyris/VisualWeaver/releases/latest/download/visualweaver-latest.zip">⬇&nbsp; Download the latest release</a></strong>
  &nbsp;·&nbsp; <a href="https://github.com/SFCyris/VisualWeaver/releases/latest">release notes</a>
</p>

---

## What is VisualWeaver?

VisualWeaver is a self-hosted chat application that answers from **your** knowledge base. Point it at documents (PDF, DOCX, XLSX, TXT, CSV, Markdown) and websites, and it ingests them into a Redis vector index; when you chat, it retrieves the most relevant passages and grounds the model's answer in them (retrieval-augmented generation). 
Redis is also used for semantic caching, so similar queries are answered from the cache and don't waste tokens.

It runs entirely on your own machine — you bring your own LLM (a local **Ollama** model, or an API key for **Claude, OpenAI, Qwen, Mistral, Groq, or Gemini**), and your data never leaves your control.

**Runs on macOS and Linux.** Redis 8 (with the search/query engine) is set up for you automatically — no separate Redis install needed.

---

## Screenshots

Answers render richly **inline** — Markdown and tables, LaTeX math, SVG diagrams, exact function graphs, and even sheet music — right in the chat.

<p align="center">
  <img src="screenshots/graph-and-formula.jpg" alt="LaTeX formulas, a comparison table, and a function graph rendered inline in VisualWeaver" width="49%">
  <img src="screenshots/notation-and-svg.jpg" alt="Sheet-music notation and an SVG geometry diagram rendered inline in VisualWeaver" width="49%">
</p>

<p align="center"><sub>Left: LaTeX math, a table, and an exact function graph. &nbsp;&bull;&nbsp; Right: sheet-music notation and an SVG geometry diagram.</sub></p>

---

## What answers can render

The model writes a short, declarative block — the browser does the drawing. That keeps figures **accurate**, because the maths and layout never depend on the model getting pixels right.

| Fence | Renders | The model writes |
|---|---|---|
| ` ```plot ` | Function graph (exact) — also parametric, polar, vector-field, contour and implicit — with optional live parameter **sliders** | `y = a*sin(b*x)` + `param: a = 0.5 .. 3 (1)` |
| ` ```chart ` | Bar / line / pie / scatter / radar chart | Chart.js JSON |
| ` ```mermaid ` | Flowchart, sequence, class, state, ER, Gantt, mindmap, quadrant, XY chart, sankey, block, kanban | mermaid syntax |
| ` ```dot ` | Auto-laid-out graph (dependencies, call graphs) | Graphviz DOT |
| ` ```geometry ` | Geometric construction | points/lines/circles as JSON |
| ` ```map ` | Map with markers / GeoJSON | `center`, `zoom`, `markers` |
| ` ```plot3d ` | 3-D surface / scatter | Plotly JSON |
| ` ```fractal ` | Mandelbrot/Julia (drag/click to zoom), IFS, L-system curves, strange attractors — interactive iteration/depth | JSON: `type` + parameters |
| ` ```molecule ` | Chemical structure | a SMILES string |
| ` ```molecule3d ` | 3D structure, rotatable/zoomable | XYZ format (atoms + coordinates) |
| ` ```plotly ` | Histogram, box, violin, heatmap, contour, sankey, treemap, sunburst, candlestick, waterfall, funnel | Plotly figure JSON |
| ` ```ode ` | Phase portrait — direction field + trajectories, with optional **sliders** | `dx = y`, `dy = -sin(x)`, `start: 1, 0` |
| ` ```scene ` | 3-D scene of primitives — rotate, zoom, pan | JSON `objects` with `position`, `rotation`, `color` |
| ` ```sequence ` | DNA / RNA / protein sequence, or an alignment | FASTA or a bare sequence |
| ` ```phylo ` | Phylogenetic tree (phylogram or cladogram) | Newick |
| ` ```reaction ` | Reaction scheme drawn from structures | reaction SMILES |
| ` ```circuit ` | Electrical schematic on a grid | JSON `components` |
| ` ```gantt ` | Project schedule — dates, durations, dependencies | mermaid gantt syntax |
| ` ```timeline ` | Dated event sequence | mermaid timeline syntax |
| ` ```network ` | Force-directed graph, draggable nodes | JSON nodes + edges |
| ` ```geojson ` | Geographic features with popups | a GeoJSON FeatureCollection |
| ` ```abc ` | Sheet music, with a Play button | ABC notation |
| ` ```calc ` | Arithmetic, unit conversion, dates, matrices — **computed, not guessed** | `5 km/h to m/s` |
| ` ```solve ` | Symbolic algebra — derivative, simplify, roots | `derivative: x^3 + 2x` |
| ` ```stats ` | Descriptive statistics & linear regression | `data: 4, 8, 15, 16, 23` |
| ` ```table ` | Sortable / filterable table with **computed** totals + CSV export | JSON: `columns`, `rows`, `total` |
| ` ```diff ` | Real line diff of two texts | `--- before` … `--- after` … |
| ` ```regex ` | Runs a pattern against samples, highlights matches | `pattern:` + `test:` lines |
| ` ```truth ` | Truth table for a boolean expression | `(A and B) or not C` |
| ` ```latex ` / `$…$` | Math formulas | LaTeX |
| ` ```svg ` | Custom vector graphic | SVG (sanitised) |
| ` ```<language> ` | Syntax-highlighted code | code |

The bottom group (`calc`, `solve`, `stats`, `table`, `diff`, `regex`, `truth`) is computed in your browser rather than asserted by the model — so unit conversions, column totals and truth tables are exact instead of "usually right". Slider-equipped `plot` blocks re-graph instantly as you drag, with no new request to the model.

<p align="center">
  <img src="screenshots/rendering/mermaid.png" alt="Mermaid flowchart rendered in a chat answer" width="32%">
  <img src="screenshots/rendering/chart.png" alt="Bar chart rendered in a chat answer" width="32%">
  <img src="screenshots/rendering/geometry.png" alt="Geometric construction rendered in a chat answer" width="32%">
</p>
<p align="center">
  <img src="screenshots/rendering/map.png" alt="Map with markers rendered in a chat answer" width="32%">
  <img src="screenshots/rendering/plot3d.png" alt="3D surface plot rendered in a chat answer" width="32%">
  <img src="screenshots/rendering/molecule.png" alt="Chemical structure rendered in a chat answer" width="32%">
</p>
<p align="center">
  <img src="screenshots/rendering/molecule3d.png" alt="Rotatable 3D molecule structure rendered in a chat answer" width="32%">
  <img src="screenshots/rendering/scene.png" alt="A rotatable 3-D scene of primitives rendered in a chat answer" width="32%">
  <img src="screenshots/rendering/ode.png" alt="A phase portrait with a damping slider rendered in a chat answer" width="32%">
</p>

Every rendered figure gets a **Source** toggle, **Copy** button, and an **⛶ Maximize** button that opens it full-viewport; most also have a **⬇ PNG** export. The source is editable in place: **▶ Apply** re-renders the figure from your changes and **↺ Reset** restores the original.

<p align="center">
  <img src="screenshots/rendering/fractal-zoom.png" alt="Mandelbrot card with a ratio-locked box-zoom selection and an Iterations slider" width="32%">
  <img src="screenshots/rendering/fractal-sierpinski.png" alt="Sierpinski triangle drawn as nested outlines with a Depth slider" width="32%">
  <img src="screenshots/rendering/editable-source.png" alt="A plot card whose Source pane has been edited and re-rendered with Apply / Reset" width="32%">
</p>

Fractals are interactive: drag a box on a Mandelbrot or Julia card to zoom in, and every fractal has a slider — **Iterations**, **Depth** or **Steps** — that re-renders it in place.

The heavier renderers (Mermaid, Chart.js, Plotly, Leaflet, JSXGraph, Viz.js, SmilesDrawer, 3Dmol.js, three.js, highlight.js) are **lazy-loaded on first use**, so they cost nothing at page load. All renderer libraries come from a public CDN; ` ```map ` additionally requests OpenStreetMap tiles for the coordinates shown. See [DOCS.md](DOCS.md#rich-content-rendering) for the full reference.

### Prompts to try

Type any of these into the chat with **No RAG** selected (no documents needed). Each one is a single sentence; the answer comes back as a live figure you can zoom, rotate, drag or edit.

| Ask | What you get |
|---|---|
| *Show me the Mandelbrot set.* | The full set on a canvas — drag a box anywhere on the edge to zoom in, again and again, and push the **Iterations** slider up as you go deeper |
| *Show the Julia set for c = −0.8 + 0.156i.* | A swirling Julia set with the same box-zoom and slider |
| *Draw the Lorenz attractor.* | The butterfly, traced as an orbit; the **Steps** slider integrates it further |
| *Draw a Barnsley fern.* | The fern as a dense point cloud, filling in as you raise **Iterations** |
| *Draw a Koch snowflake and a Sierpinski triangle.* | Two cards; step each one's **Depth** slider from 1 upward and watch the structure build |
| *Build a 3D scene of a red box on a blue plate with a golden sphere on top and a purple torus beside it.* | A rotatable 3-D scene — drag to orbit, wheel to zoom, shift-drag to pan, **⟲ Reset view** to go back |
| *Plot the 3D surface z = sin(x)·cos(y).* | A shaded surface you can spin |
| *Plot y = sin(a·x)/x with a slider for a from 1 to 10.* | A graph with a live slider; drag it and the curve re-draws instantly |
| *Graph the rose curve r = cos(4θ).* | An eight-petal polar rose |
| *Show the vector field of a vortex, −y, x.* | A whirl of arrows across the plane |
| *Plot the contours of x² − y² and the implicit curve x² + y² = 4.* | Saddle contours in a blue ramp with the circle drawn in red on top |
| *Plot the phase portrait of a damped pendulum with a slider for the damping.* | Direction field, spiralling trajectories, and a damping slider that re-integrates them as you drag |
| *Draw the structure of caffeine.* | The molecule as a chemical structure |
| *Show the esterification of acetic acid and ethanol as a reaction scheme.* | Reactants, arrow and products drawn as structures |
| *Draw the schematic of an LED circuit: a 9 V battery, a 220 Ω resistor and an LED.* | A schematic with the symbols on a grid |
| *Show the phylogenetic tree of the great apes with branch lengths.* | A phylogram with a scale bar |
| *Align the first 60 residues of human and mouse hemoglobin alpha.* | A coloured alignment with every differing column marked |
| *Make a sunburst chart of a typical monthly household budget.* | A drillable ring chart |
| *Draw a mind map of the solar system.* | A radial mind map, planets branching from the Sun |
| *Put the seven wonders of the ancient world on a map.* | A world map with a marker and popup for each |
| *Draw a timeline of the Apollo missions.* | A dated timeline card |
| *Write sheet music for the first line of "Twinkle, Twinkle, Little Star".* | A rendered score with a **▶ Play** button |
| *Show a geometric construction of the golden ratio.* | An interactive construction — press **✋ Interact** and drag its points |

Every card has a **Source** button: change a number in the block, press **▶ Apply**, and the figure re-renders in place. If an answer comes back as plain text, ask the model to *"render that as a ```fractal block"* (or whichever fence fits) — the fence names are listed in the table above.

---

## Grounded answers you can check

Answers cite the passages they came from, so any claim can be traced back to the chunk that supports it. Click **⌖ only this** on a chunk to scope your next question to that document alone.

<p align="center">
  <img src="screenshots/rendering/citations-scope.png" alt="An answer with [1] and [2] citation markers, the matched-chunk inspector, and a source-scope chip above the composer" width="88%">
</p>

When retrieval finds nothing above the threshold, the answer says so and is badged **📭 No KB match** (or **📭 “name” is empty** when the knowledge base has no documents yet) — rather than quietly answering from general knowledge as though it were grounded.

<p align="center">
  <img src="screenshots/rendering/no-kb-match.png" alt="An answer badged No KB match" width="88%">
</p>

Each knowledge base is browsable: see what's indexed, scope a question to one document, or remove a single stale document and re-ingest it without rebuilding everything.

<p align="center">
  <img src="screenshots/rendering/documents.png" alt="The document manager listing each source with chunk counts, scope and delete buttons" width="88%">
</p>

---

## Quick start

### Docker (simplest)

**Requires** Docker with the Compose plugin — Docker Desktop, Colima, OrbStack, Rancher Desktop, or Docker Engine on Linux. Check with `docker compose version`; MongoDB needs Compose 2.23.1 or newer.

```bash
./docker.sh up
```

It asks which storage to use (and remembers the answer), downloads what is needed, starts everything, waits until it is ready, and prints the address and the versions in use. Then open **http://localhost:8420**.

| Storage | What runs | Command |
|---|---|---|
| **Redis** (default) | the app + Redis 8 with search | `./docker.sh up redis` |
| **MongoDB** | the app + MongoDB with vector search (database + search process), no Redis | `./docker.sh up mongodb` |
| **Both** | the app + Redis (app data and the answer cache) + MongoDB, available for RAG instances | `./docker.sh up both` |

Done for you, inside the containers and by `docker.sh`:

- **MongoDB**: its passwords and replica-set keyfile are generated on first start and kept in a Docker volume; the replica set, its users and the search process are set up automatically; it is reachable only inside the compose network. Before anything is downloaded, `docker.sh` shows MongoDB's licence (SSPL v1) and asks you to accept it (`VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1` for unattended runs).
- **Start order**: the app starts once its storage is healthy, and `docker.sh up` returns once the app answers.
- **Models**: an Ollama running on the same machine is used automatically. For a cloud model, add its key in **Settings → Providers**.
- **Port**: if 8420 is taken, the next free port is used and remembered (`VISUALWEAVER_WEB_PORT`).
- **Host names**: this machine's DNS name is allowed for browsers (`VISUALWEAVER_ALLOWED_HOSTS`, see [Security](#security)).
- **Disk space**: MongoDB's search stops building indexes when its disk is over 85 % full (it resumes below 80 %), and new documents are then not found. That disk is Docker's own (on macOS and Windows, the Docker VM's disk, which images fill up); `./docker.sh up` and `status` warn from 80 %. Free space with `docker image prune` / `docker builder prune`, or give the VM a larger disk.

Everyday commands:

| Command | Does |
|---|---|
| `./docker.sh status` | what is running, the address and versions |
| `./docker.sh logs [service]` | follow the logs (`visualweaver`, `redis`, `mongodb`, `mongodb-search`) |
| `./docker.sh stop` / `./docker.sh up` | stop and start again; everything is kept |
| `./docker.sh up both` | change the storage choice (data already stored stays where it is) |
| `./docker.sh backup [folder]` | save the app data and every store (they stop for a few seconds) |
| `./docker.sh restore <folder>` | replace the data with a backup, then start |
| `./docker.sh down` | remove the containers; the data volumes are kept |
| `./docker.sh up --build` | build the app image from this checkout instead of pulling it (later `up` runs keep building; `--pull` goes back to the published image) |

To upgrade: `docker compose pull && ./docker.sh up`.

**Without the helper**, the same choices are Compose profiles, read from `COMPOSE_PROFILES` (put the line `COMPOSE_PROFILES=mongodb` in a `.env` file next to `docker-compose.yml` to make every command use it):

```bash
docker compose up -d                              # Redis
COMPOSE_PROFILES=mongodb docker compose up -d     # MongoDB
COMPOSE_PROFILES=both docker compose up -d        # both
docker compose stop                               # stop, everything is kept
```

Use `COMPOSE_PROFILES` rather than the `--profile` flag: the app reads the same value to know which stores it has. Settings `docker.sh` keeps in `.env` are listed in [`.env.example`](.env.example).

MongoDB runs as MongoDB's official Community images (server and search, licensed by MongoDB, Inc. under the [SSPL v1](https://www.mongodb.com/licensing/server-side-public-license)), pulled from Docker Hub only when a MongoDB choice is made; they are not part of VisualWeaver.

Your data lives in Docker volumes that survive restarts, upgrades and `down`: the app's config, uploads and history, and the data of each store you run. `./docker.sh backup` saves all of them.

#### CPU embeddings, and optional GPU

The image ships a **CPU-only build of PyTorch**. That is deliberate: the default PyPI `torch` drags in NVIDIA's CUDA runtime wheels, which are proprietary and must not be redistributed inside an AGPL image (see [THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)). It also keeps the image roughly 2 GB smaller. Embedding a document corpus on CPU is perfectly usable — the LLM itself runs elsewhere (Ollama or a cloud provider).

If you have an NVIDIA GPU and want accelerated embeddings, install the CUDA build **yourself, on your own machine** — pick the channel matching your driver (`cu126`, `cu128`, `cu129`, `cu130`):

```bash
docker compose exec visualweaver pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu129
```

Add `--gpus all` (or a `deploy.resources` reservation in compose) so the container can see the GPU, and re-run it after any `docker compose pull`, since a fresh image is CPU-only again. For a permanent GPU image, change that one `pip install` line in the `Dockerfile` and build it yourself — the resulting image is then yours to distribute or not.

### Local (macOS / Linux)

```bash
./install.sh     # Python venv + the storage you choose (asked once)
./start.sh       # starts the installed storage, then the app
```

Open **http://localhost:8420**. Stop everything (app + its storage) with `./stop.sh`; `./restart.sh` restarts. Your data is kept across stops and restarts. Ports, logs, upgrades, backups and troubleshooting: [DOCS.md → Running and Maintenance](DOCS.md#running-and-maintenance).

`install.sh` asks where VisualWeaver keeps knowledge bases, conversations and the answer cache:

| Choice | What is installed |
|---|---|
| **1 · Redis 8 with search** (default) | A private Redis 8 vendored into `./.redis` (macOS) or reused / installed from packages.redis.io (Linux), on a dedicated loopback port (6389 by default) — it never touches any Redis you already run. |
| **2 · MongoDB with vector search** | MongoDB Community Server 9.0 and MongoDB Search, MongoDB's official container images, run by the container runtime already on your machine, on a loopback port (27019 by default). |
| **3 · Both** | Both of the above. Redis holds app data and the answer cache; MongoDB is available for RAG instances. |

Without a terminal (CI, scripts) set `VISUALWEAVER_STORAGE=redis|mongodb|both`; the default is `redis`. The choice is recorded in `.visualweaver.env`. Re-running `./install.sh` keeps what is installed and offers to add the other store; it never removes one.

**MongoDB needs a container runtime** that is already running — Colima, Podman, Rancher Desktop, OrbStack or Docker Desktop (Docker Engine on Linux). `install.sh` does not install one. Docker Desktop needs a paid subscription for companies with 250 or more employees or US$10 million or more in annual revenue; Colima, Podman and Rancher Desktop are free and open source. If no runtime is running, the installer says how to start one and offers to continue with Redis.

**MongoDB's licence:** MongoDB Community Server and MongoDB Search are licensed by MongoDB, Inc. under the [Server Side Public License v1](https://www.mongodb.com/licensing/server-side-public-license). They are downloaded from MongoDB at install time (about 1.9 GB on disk) and are not distributed with VisualWeaver. The installer shows the licence and asks you to accept it; a non-interactive install needs `VISUALWEAVER_ACCEPT_MONGODB_LICENSE=1`.

`install.sh` checks every port it needs — the web UI (8420), Redis and MongoDB. A port taken by another program is reported with what holds it, and the next free one is used and recorded. It ends with a validation table (✓/✗ with address and version for the Python environment, each store and the web UI port) and leaves the stores running, so `./start.sh` starts the app straight away. `./start.sh` prints the versions in use: VisualWeaver, Python, Redis and its search module, MongoDB and MongoDB Search, and the container runtime.

**Requirements:** Python 3.11 or 3.12 (`install.sh` picks the newest supported one it finds, or the one in `PYTHON=`), and on macOS Homebrew (for `openssl@3`) + Xcode Command Line Tools. For MongoDB: a running container runtime with Compose (`docker compose` or `podman compose`).

---

## Configuration

Settings are stored **outside the repo**, in a per-platform data directory:

| Platform | Location |
|---|---|
| macOS | `~/Library/Application Support/VisualWeaver` |
| Linux | `$XDG_DATA_HOME/visualweaver` or `~/.local/share/visualweaver` |

That directory holds `config.json`, uploads, logs, and ingestion history. A clean template is in [`config.example.json`](config.example.json). Configure everything from the in-app **Settings** UI, or set provider keys via environment variables so they are never written to disk:

```bash
export ANTHROPIC_API_KEY=...   # Claude
export OPENAI_API_KEY=...       # OpenAI
export GEMINI_API_KEY=...       # Gemini
export GROQ_API_KEY=...         # Groq
export DASHSCOPE_API_KEY=...    # Qwen
export MISTRAL_API_KEY=...      # Mistral
```

**Ports:** the web UI is **8420**; the dedicated Redis is **6389** and the built-in MongoDB **27019** (both loopback only). `install.sh` moves any of them to the next free port when another program holds it. Override the app port with `VISUALWEAVER_PORT`, or `./start.sh 9000`.

---

## Features

**💬 Chat** — token-by-token streaming over WebSocket; [rich inline rendering](#what-answers-can-render) (diagrams, charts, graphs, maps, molecules, music, math); image attachments for vision models (drag/drop, paste); voice input (the browser's own speech recognition — some browsers send the audio to their vendor's online service, which the 🎤 tooltip says); regenerate in place with a version switcher to compare attempts; **Copy, Rerun, or Force rerun** (cache-bypassing) any earlier question from its hover action bar; conversations persisted in Redis and restored on reload; **keep an answer** — index one into a knowledge base so it outlives the cache and the conversation, after editing it and its cited sources; **search across conversations** (Shift+⌘/Ctrl+F), including the retrieved source passages, with match counts — plain ⌘/Ctrl+F is left to the browser; pin messages; rate answers (👍/👎); auto-generated session titles; a top-bar token count split into input/output/total, using the provider's own billed figures; export as text.

**🧠 RAG** — multiple named knowledge bases (RAG instances); ingest PDF, DOCX, XLSX, TXT, CSV, and Markdown files; a parallel BFS web crawler with a smart httpx-first mode and optional JS rendering (crawl4ai + Playwright) for SPAs — pausable, cancellable, and reporting real progress against the pages it has discovered; **scheduled auto re-crawl** of web sources, managed from Settings; file ingestion that can be watched, re-attached to and stopped; `llms.txt` auto-detection; **hybrid retrieval** — vector KNN + BM25 keyword search fused by RRF (on by default), with optional cross-encoder reranking and HyDE; a RAG inspector showing retrieved chunks and scores; **inline citations** linking each claim to the passage that supports it — click a `[2]` marker to open and highlight the source it refers to; **per-document management** — browse what's indexed, scope a question to one document, or delete one without rebuilding the instance; an explicit *No KB match* badge when retrieval finds nothing; a top-bar selector to query one instance, **all** of them in parallel, or **No RAG** (answer with no retrieval); export/import a knowledge base as a zip; duplicate detection.

**⚡ Semantic cache** — cosine-similarity response caching in Redis with a configurable threshold and TTL; per-message hit/miss badge; delete or re-run cached answers.

**🤖 LLM providers** — Ollama (local, no key), Claude, OpenAI, Qwen, Mistral, Groq, Gemini. Switch providers and models from the UI; vision auto-detected for capable models.

**⚙️ Settings** — storage: the built-in Redis plus further Redis or MongoDB (Atlas or self-hosted) connections to keep RAG instances on, embedding-model selector, chunk-size/overlap/top-K/threshold controls, a **chat-history token budget**, a **chart-authoring toggle** (drop ~2k tokens/message on text-only deployments), reusable prompt templates, dark/light/auto theme, export/import config; staged edits are marked **unsaved** and confirmed before they are discarded.

**📊 Observability** — per-response latency breakdown (cache / RAG / LLM / total), Redis memory monitor, cache analytics, ingestion logs, a session notification log (errors stay until dismissed), a system-status panel covering every provider, and an all-time **token usage** table broken down by provider and model (CSV export included) — token counts only, so no stale price estimate is passed off as your bill.

<p align="center">
  <img src="screenshots/features/no-rag.png" alt="The top-bar selector set to No RAG — answer with no retrieval" width="72%">
</p>
<p align="center">
  <img src="screenshots/features/query-actions.png" alt="Copy / Rerun / Force rerun action bar under an earlier question" width="72%">
</p>
<p align="center"><sub>Query one knowledge base, all of them in parallel, or <b>No RAG</b> per message &nbsp;&bull;&nbsp; <b>Copy</b>, <b>Rerun</b>, or <b>Force rerun</b> any earlier question from its hover bar.</sub></p>

---

## Security

VisualWeaver has **no built-in authentication**. `install.sh` asks who may open the web UI — only this machine (`127.0.0.1`, what just pressing Enter chooses) or any device on the network (`0.0.0.0`) — and records the answer in `.visualweaver.env`. Without a recorded answer (an install run with no terminal), the **local runtime** (`start.sh`) listens on all interfaces (`0.0.0.0`), and the **Docker Compose** setup always does, so the app *is* reachable from other devices on your network; `start.sh` prints the LAN address and a warning on every start when it is open. Run it open only on a trusted network. To switch later, run `./changeAccess.sh local` (this machine only) or `./changeAccess.sh lan` (any device on the network) and restart; for a single run, `VISUALWEAVER_HOST=127.0.0.1 ./start.sh` restricts the local runtime. For Docker, put `VISUALWEAVER_WEB_BIND=127.0.0.1` in `.env` and run `./docker.sh up` again. For anything beyond a trusted LAN, put a reverse proxy with authentication in front of it (see [`deploy/docker-compose.https.yml`](deploy/docker-compose.https.yml) for a Caddy + automatic-HTTPS setup). **Never expose the port on an untrusted network without an auth layer.** Because there is no login, the app refuses requests that another website makes from your browser (another site's page cannot use the chat or change settings), and it answers browsers only for host names an outsider cannot own — IP addresses, `localhost`, single-word intranet names, names ending in `.local`, `.lan`, `.internal` or `.home.arpa`, and this machine's own name. To reach it under any other name, such as a reverse proxy's domain, list that name in `VISUALWEAVER_ALLOWED_HOSTS` (comma-separated; `*.example.com` matches subdomains); `deploy/docker-compose.https.yml` sets it next to the Caddy domain.

**Untrusted content.** Model and RAG output is treated as untrusted: rendered Markdown and every SVG/diagram is sanitised (DOMPurify) before it reaches the DOM, the `geometry` block accepts data only — never expressions — and a Content-Security-Policy restricts scripts to the app itself plus the two CDNs the renderers come from. Ingested pages can carry prompt injections, so this matters in normal use, not just under attack.

One deliberate trade-off: `img-src` permits any `https:` source, because answers legitimately show images from the pages and documents you ingest, plus map tiles. In principle that is an exfiltration channel if script execution were ever achieved. `connect-src` is far narrower — `'self'` plus one fixed host, `https://paulrosen.github.io`, which abcjs fetches General-MIDI soundfont samples from for the `abc` Play button — so `img-src` remains the broad channel and `connect-src` is limited to that single static-asset host. If your deployment does not need remote images, tighten `img-src` in `_CSP` (`visualweaver/appcore.py`) to `'self' data: blob:`; if you do not need `abc` audio playback, drop `https://paulrosen.github.io` from `connect-src` to bring it back to `'self'`.

---

## Documentation

- **[TUTORIAL.md](TUTORIAL.md)** — step-by-step getting started
- **[DOCS.md](DOCS.md)** — full feature and API reference
- **[SETTINGS.md](SETTINGS.md)** — every setting explained

---

## Acknowledgments

VisualWeaver is built on excellent open-source projects:

- **[Redis](https://redis.io)** — in-memory datastore and vector/query engine *(AGPLv3)*
- **Backend** — [FastAPI](https://fastapi.tiangolo.com) & [Uvicorn](https://www.uvicorn.org) *(MIT / BSD-3-Clause)*, [redis-py](https://github.com/redis/redis-py) and [RedisVL](https://github.com/redis/redis-vl-python) *(MIT)*, [NumPy](https://numpy.org) *(BSD-3-Clause)*, [sentence-transformers](https://www.sbert.net) *(Apache-2.0)*, [PyMuPDF](https://pymupdf.readthedocs.io) *(AGPLv3)*, [Trafilatura](https://trafilatura.readthedocs.io) *(Apache-2.0)*, [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) *(MIT)*, [Pillow](https://python-pillow.org) *(MIT-CMU)*, [httpx](https://www.python-httpx.org) *(BSD-3-Clause)*, and [Crawl4AI](https://github.com/unclecode/crawl4ai) + [Playwright](https://playwright.dev) *(Apache-2.0)*
- **Document parsing** — [python-docx](https://python-docx.readthedocs.io) and [openpyxl](https://openpyxl.readthedocs.io) *(MIT)*
- **LLM providers** — [Ollama](https://ollama.com) for local models, and the [Anthropic](https://github.com/anthropics/anthropic-sdk-python) *(MIT)*, [OpenAI](https://github.com/openai/openai-python), [Groq](https://github.com/groq/groq-python), and [Google GenAI](https://github.com/googleapis/python-genai) *(Apache-2.0)* SDKs
- **Deployment** — [Caddy](https://caddyserver.com) *(Apache-2.0)* for automatic HTTPS

**Browser rendering** — the libraries that turn an answer into a figure. Each is fetched from a public CDN ([cdnjs](https://cdnjs.com), or [jsDelivr](https://www.jsdelivr.com) for smiles-drawer, viz.js and the Noto Music font) the first time its block type appears:

| Library | Used for | License |
|---|---|---|
| [marked](https://marked.js.org) | Markdown | MIT |
| [DOMPurify](https://github.com/cure53/DOMPurify) | sanitising SVG & rendered HTML | Apache-2.0 / MPL-2.0 |
| [KaTeX](https://katex.org) | LaTeX math | MIT |
| [math.js](https://mathjs.org) | evaluating ` ```plot ` functions | Apache-2.0 |
| [Chart.js](https://www.chartjs.org) | ` ```chart ` data charts | MIT |
| [Mermaid](https://mermaid.js.org) | ` ```mermaid ` diagrams | MIT |
| [Viz.js](https://github.com/mdaines/viz.js) | ` ```dot ` graph layout — a build of [Graphviz](https://graphviz.org) 15.1.1 *(EPL-2.0)* | MIT |
| [JSXGraph](https://jsxgraph.org) | ` ```geometry ` constructions | MIT or LGPL-3.0-or-later |
| [Leaflet](https://leafletjs.com) | ` ```map ` maps | BSD-2-Clause |
| [Plotly.js](https://plotly.com/javascript/) | ` ```plot3d ` 3-D plots and ` ```plotly ` statistical charts | MIT |
| [SmilesDrawer](https://github.com/reymond-group/smilesDrawer) | ` ```molecule ` structures and ` ```reaction ` schemes | MIT |
| [3Dmol.js](https://3dmol.csb.pitt.edu) | ` ```molecule3d ` rotatable 3-D structures | BSD-3-Clause |
| [three.js](https://threejs.org) | ` ```scene ` 3-D scenes | MIT |
| [abcjs](https://www.abcjs.net) | ` ```abc ` sheet music | MIT |
| [highlight.js](https://highlightjs.org) | code syntax highlighting | BSD-3-Clause |
| [Noto Music](https://github.com/notofonts/music) font | music notation symbols (𝅗𝅥 𝅘𝅥𝅮 …) in answers and diagrams — fetched only when one appears and no local copy is installed | OFL-1.1 |

Map tiles are served by [OpenStreetMap](https://www.openstreetmap.org/copyright) — map data © OpenStreetMap contributors, available under the [Open Database License](https://opendatacommons.org/licenses/odbl/) (attribution is shown on every rendered map).

Each dependency is distributed under its own license; the full text ships with each package. A complete inventory of every dependency and its license — including the multi-licensed ones and which option VisualWeaver elects — is in **[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)**.

---

## Trademarks

**Redis** is a registered trademark of Redis Ltd. VisualWeaver is an independent project that
integrates with Redis® OSS. It is **not endorsed, supported, sponsored, or certified by
Redis Ltd.**, and no affiliation is implied. The Redis logo and visual identity are not
used anywhere in this project; Redis is referred to by name only, to identify the software
VisualWeaver connects to.

**MongoDB** and **MongoDB Atlas** are trademarks of MongoDB, Inc. VisualWeaver can store
RAG instances in a MongoDB database; it is **not endorsed, supported, sponsored, or
certified by MongoDB, Inc.**, and no affiliation is implied. The MongoDB logo is not used.

Docker, GitHub, Google Gemini, Anthropic Claude, OpenAI, Mistral, Ollama, Groq and Qwen —
and every other product named in this documentation or in the application — are trademarks
of their respective owners. They are used for identification only. None of those owners
endorses, sponsors or is affiliated with VisualWeaver.

A trademark is separate from the licence its software ships under: an open-source licence
grants rights in the code, not in the name or the logo.

## License

VisualWeaver is licensed under the **[AGPL-3.0-or-later](LICENSE)**.

This matches what the dependency stack requires: **PyMuPDF** (PDF extraction) and **Redis 8** are themselves AGPLv3, and the other Python dependencies are permissive (MIT / BSD / MIT-CMU) or Apache-2.0 — all one-way compatible into AGPLv3, so the combined work is cleanly licensable under the AGPL. Using VisualWeaver under different terms would mean replacing the AGPL components (for example, obtaining a commercial PyMuPDF license from Artifex).

The browser rendering libraries are **not bundled or redistributed** — VisualWeaver ships only a URL, and the browser fetches each one from a public CDN at runtime. They are MIT / BSD / Apache-2.0 / MPL-2.0 (JSXGraph is dual MIT-or-LGPL, used here under MIT). One note for completeness: `viz.js` is MIT but embeds **Graphviz** 15.1.1, which is under the Eclipse Public License 2.0 — a license the FSF considers GPL-incompatible. Because it is loaded at runtime by the browser rather than distributed with VisualWeaver, it does not form a combined work with this AGPL codebase; anyone who chooses to *vendor* these libraries into a distributed build should review that themselves.
