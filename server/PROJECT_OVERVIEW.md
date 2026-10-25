# Featherbase: complete project overview

This document explains the whole Featherbase project, for an engineer (or Claude) who builds the public
catalogue app and needs to plan merging the two. It covers what exists, how it works, how it is
deployed, the rules it relies on, what is unfinished, and what any merge must preserve. The narrower
read-only database contract is in [`PUBLIC_APP_INTEGRATION.md`](PUBLIC_APP_INTEGRATION.md); read that
too.

State as of 25 October 2026, branch `truenas-workflow`. Production runs commit `6d73c41`; the commits after it so far change only documentation and the Windows bundle.

---

## 1. What it is

Featherbase is a **self-hosted, bird-first photo workflow** for a bird photographer. Photos are copied
off the camera into a *Dumpsite* folder on a TrueNAS server. Featherbase then:

- ingests them;
- groups them into **day sessions**;
- suggests the species of every bird with AI, using the open-source **Aves Caesar** models (10,859 species);
- lets the photographer review and correct each suggestion in a web UI;
- **moves** confirmed photos into a *Published* library arranged as `country/state/city/date/filetype/`,
  never renaming them except to resolve a clash;
- records everything in PostgreSQL.

- **Name:** "Featherbase" is a working name from a brand brief. It isn't trademark-cleared, because an
  ornithological feather database already uses it at featherbase.info.
- **Origin:** a fork of `avescaesar/aves-caesar-desktop`, a Windows desktop app (pywebview, AGPL-3.0).
  The desktop app is still in the repository and still works. Featherbase is a **new server application**
  that reuses the desktop app's inference engine.
- **Licence:** AGPL-3.0-or-later. Serving it to other people over a network obliges you to offer them the
  source; the fork is public.

### The two apps

| | Featherbase (this project) | Public catalogue (your project) |
| --- | --- | --- |
| Users | The photographer only | Anyone (friends, the public) |
| Job | Ingest → identify → review → file into the library | Choose what is public, show it nicely, protect locations |
| Data | Writes the library and the database | Read-only access to both |
| Deployed as | Portainer stack `aves-caesar` | Its own Portainer stack (planned) |

---

## 2. Repository layout

GitHub: `github.com/jiwateshwar/aves-caesar-desktop` (public), branch **`truenas-workflow`**. Remotes in the
owner's clone: `fork` is that repository; `origin` is the upstream Aves Caesar repository (never push there).

```
backend/bird_desktop/        Aves Caesar inference engine and the original desktop app (upstream code)
  inference/pipeline.py        BirdOnnxPipeline: detector + classifier (ONNX Runtime)
  inference/providers.py       execution-provider selection, per model (changed by this project, §5)
  media/exiftool*.py           ExifTool wrapper and persistent session
  media/image_io.py            image loading: RAW embedded preview, HEIC, EXIF orientation
  runtime/config.py, paths.py  engine config (AVES_MODELS_DIR override added by this project)
frontend/                    the desktop app's UI (untouched; not used by Featherbase)
models/                      model files for local development (git-ignored, ~1.8 GB)
server/                      FEATHERBASE
  aves_server/                 Python package: API, worker, workflow logic (§4)
    migrations/                Alembic migrations (§6)
    static/                    built web UI (git-ignored; produced by web/)
  web/                         review UI source: Preact + TypeScript + Vite (§7)
  windows/                     Windows GPU node: setup script, tray launcher, bundle builder (§8)
  docker/entrypoint.sh         container entrypoint: downloads models, starts web or worker
  Dockerfile                   one image for the web and worker containers
  compose.yaml                 the Portainer stack (§9)
  compose.nvidia.yaml          add-on for an NVIDIA card inside the NAS (not used in production)
  stack.env.example            stack variables, documented
  tests/                       pytest suite (§11)
  README.md                    owner-facing install and usage guide
  PUBLIC_APP_INTEGRATION.md    the read-only contract for your app
  PROJECT_OVERVIEW.md          this file
.github/workflows/server.yml CI: tests on SQLite and PostgreSQL, then an image to GHCR (see §12)
```

The Python package is still called `aves_server`, and environment variables are still `AVES_*`. These
internal names were deliberately not rebranded, so the live deployment keeps its data.

---

## 3. The workflow end to end

### 3.1 Ingest (worker, `scanner.py`)

- **Polling:** the worker polls the Dumpsite every `poll_interval_seconds` (30) and ingests a file only
  once it has stopped changing for `settle_seconds` (30), so cards still being copied over SMB are never
  read half-way. *Scan now* in the UI triggers a quick scan that waits only 3 seconds.
- **What it takes:** still images only: JPG, JPEG, HEIC, HEIF, PNG, TIF, TIFF, and about 30 RAW formats
  (NEF, CR2, CR3, ARW, DNG, RAF, ORF, RW2, …).
- **What it skips:** videos, hidden files, `._*`, `@eaDir`, `.recycle`, `#recycle` and similar NAS clutter.
- **Per file:** it records the SHA-256 and the full EXIF/XMP/IPTC (read with ExifTool, `-G -n`, maker notes
  excluded, NUL bytes stripped so PostgreSQL JSONB accepts it), plus camera, lens, exposure, size and GPS.
- **Duplicates:** a file whose SHA-256 matches a file already in the workflow or the library becomes
  `status = duplicate` and is kept out of the workflow. The UI lists duplicates and can delete the extra
  copy from the Dumpsite.
- **Shots:** files in the same folder with the same stem and the same capture time become one **shot**,
  so a RAW+JPEG pair is reviewed once. The key is `group_key = "<folder>|<stem lowercased>|<capture time ISO>"`.
- **Sessions:** a shot goes into the **session** for its capture date (EXIF `DateTimeOriginal`, in camera
  local time). It falls back to the file's modification time, recorded as `captured_at_source = 'file_mtime'`.
- **Missing files:** files that disappear from the Dumpsite become `missing`, and become `in_dump` again if
  they come back. If the whole Dumpsite suddenly looks empty, nothing is marked missing, on the assumption
  that the dataset isn't mounted.
- **The scanner never moves or deletes anything.**

### 3.2 Location (`locations.py`, `geocoding.py`)

- A shot with GPS is reverse-geocoded **offline**, with GeoNames data bundled in the `reverse_geocode`
  package, to country, state and city. `city_min_population` can skip villages.
- A shot without GPS inherits the **session location** that the photographer sets in the UI, by place
  search or by pasting coordinates (`location_source = 'session'`). Until then it is `awaiting_location`,
  unless the session is set to "identify without location".
- Setting a session location queues for re-identification the shots that haven't been touched yet,
  because the model is more accurate with a location.
- The photographer can override country, state and city on selected shots before publishing, since GPS
  sometimes resolves to a neighbourhood.

### 3.3 Identification (`analysis.py`, `engine.py`, backend engine)

- **What it runs:** a shot's primary file is the JPG if the shot has one, otherwise HEIC, TIFF, PNG, and
  then RAW, from its embedded preview. That file goes through the Aves Caesar **detector**, which finds
  the birds and their boxes. Each bird crop then goes through the **classifier**, which returns the top-5
  species. The classifier also takes latitude, longitude and day of year.
- **Batching and filtering:** shots are sent in batches of 8, and predictions below the engine's
  `min_classification_confidence` (0.01) are dropped.
- **What is stored:**
  - one `detections` row per bird, holding the top-5 `predictions`;
  - the current `species_id`, initially the top-1;
  - `confidence`, `box` and `box_confidence`;
  - `source = model`, `status = pending`.
  The shot's `bird_count`, `min_confidence` (the weakest top-1 across its birds), `image_width` and
  `image_height` (the analysed image's size) are set as well.
- **Where it runs:** on the NAS's CPU, on an NVIDIA GPU in the NAS, or on a **Windows PC's GPU** over the
  LAN (§8). The worker falls back to local processing when the PC is off.
- **Previews:** the worker writes a 480 px thumbnail and a 2048 px preview JPEG per shot to
  `AppData/cache/previews/{shot_id // 1000}/{shot_id}_{thumb|preview}.jpg`, for the review UI only.
- **Robustness:**
  - a failed batch is retried one photo at a time;
  - a photo's attempt counter is saved before inference, so even a hard crash counts towards the limit;
  - after 3 failures the shot goes to `error`;
  - when a whole multi-photo batch fails, the worker reloads the models, which re-runs the GPU check and
    falls back to the CPU if the GPU is gone.

### 3.4 Review (web UI and `review.py`)

Tabs per session and folder: To review, Low confidence (any bird's top-1 below `confidence_threshold`,
0.6), No bird, Needs location, Queued, Name conflicts, Confirmed, Published, Left in dump, Errors.

The photographer can:
- confirm a shot;
- pick one of the model's alternatives;
- search all species;
- reject a detection as "not a bird";
- add a species by hand (no box);
- leave a shot in the dump (`ignored`);
- reopen a shot;
- bulk-confirm the confident shots.

*Bulk confirm* only touches shots where every bird's top-1 meets the threshold and nothing was edited.

A "no bird" shot can be published without a species, tagged by hand, or left in the dump.

### 3.5 Publishing (`publisher.py`)

- **What gets moved:** a publish job moves every `in_dump` file of each `confirmed` shot to
  `Published/<pattern>/<final_filename>`. The default pattern is `{country}/{state}/{city}/{date}/{filetype}`.
  Fields available: `country`, `state`, `city`, `date`, `year`, `month`, `filetype`. Unsafe characters
  become `-`, and empty values become `unknown_placeholder` ("Unknown").
- **Never overwrites:** on one filesystem a move is a hard link and then unlink, which fails if the name
  exists. Across filesystems it's copy, fsync, SHA-256 verify, rename, then delete the source.
- **Atomic per shot:** every file of a shot is checked before any moves, so a RAW+JPEG pair is never split.
- **Database updated per file:** the database is updated after each file moves, so it never claims more
  than is on disk.
- **XMP sidecars** (`name.ext.xmp`, `stem.xmp`) move with their photo.
- **Filename uniqueness across the whole library, case-insensitive:**
  - `library_files` indexes every file in `Published`, including files the app didn't put there. The
    index is rebuilt at worker startup and every 6 hours.
  - A clashing shot is held back until the photographer appends a suffix of their choice, e.g.
    `DSC_0001.NEF` → `DSC_0001_trip2.NEF`. The same suffix goes on every file of the shot.

---

## 4. Server code (`server/aves_server/`)

| Module | Responsibility |
| --- | --- |
| `config.py` | `ServerConfig` from environment variables: paths, database URL, password. |
| `settings.py` | `WorkflowSettings` (user-tunable, stored in the `settings` table) and validation. |
| `db.py` | SQLAlchemy engine and sessions; `Database.migrate()` runs Alembic under a PostgreSQL advisory lock. |
| `models.py` | ORM models and status enums (§6). |
| `media.py` | File classification, SHA-256, EXIF parsing, `RestartingExifTool` (a persistent ExifTool process, restarted when it dies). |
| `scanner.py` | Dumpsite polling, settle detection, ingest, duplicates, missing files. |
| `shots.py` | Primary-file choice and shot summary (`bird_count`, `min_confidence`). |
| `locations.py`, `geocoding.py` | Session and shot locations; offline geocoding and place search. |
| `engine.py` | Inference backends: `LocalBackend`, `RemoteBackend` (Windows node), `FallbackBackend`. No database imports. |
| `analysis.py` | `Analyzer`: loads images, runs the backend, stores detections, isolates failures. |
| `review.py` | Every review action, and filename-suffix conflict resolution. |
| `publisher.py` | `ConflictChecker`, `target_folder`, `move_file`, `Publisher`, `LibraryIndexer`. |
| `previews.py` | Thumbnails and previews for the review UI. |
| `jobs.py` | Database-backed job queue (`scan`, `publish`, `reindex_library`, `reanalyse`). |
| `worker.py` | The worker process loop (§4.2). |
| `api.py`, `serializers.py` | FastAPI app: the REST API and the static web UI (§4.1). |
| `catalog.py` | Species list from the model's `species_mapping_v2.csv`, search, and seeding of the `species` table. |
| `inference_node.py`, `tray.py` | The Windows GPU node and its tray app (§8). |
| `brand.py` | The product name (also in `web/src/brand.ts`), kept in one place so it can be renamed. |
| `views.py` | Names of the SQL views; their definitions are in the migrations. |

### 4.1 REST API (private admin API, `api.py`)

This API is for the Featherbase UI only. **It is not a public contract**, and your app should not depend
on it. Authentication: if `AVES_PASSWORD` is set, every route except `/api/health` needs HTTP Basic
(any username). Production currently runs **without** a password. Interactive docs are at `/docs`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness check. |
| GET | `/api/status` | Worker heartbeat and activity, providers in use, model errors, last scan, totals per category, running jobs, paths. |
| GET | `/api/sessions`, `/api/sessions/{id}` | Day sessions with counts per category (and per folder). |
| PATCH | `/api/sessions/{id}` | Title; location (`latitude`/`longitude`/`label`); `clear_location`; `analyse_without_location`. |
| POST | `/api/sessions/{id}/confirm-confident` | Bulk-confirm confident, untouched shots. |
| GET | `/api/shots?session_id&folder&category&offset&limit`, `/api/shots/{id}` | Shot lists by category; full shot detail (detections, files, EXIF, publish target). |
| GET | `/api/shots/{id}/thumb.jpg`, `/preview.jpg` | Review previews. |
| GET | `/api/files/{id}/download` | The original file. |
| POST | `/api/shots/{id}/confirm`, `/ignore`, `/reopen`, `/species`, `/suffix` | Review actions on one shot. |
| POST | `/api/shots/{id}/detections/{det}/species`, `/reject`, `/restore` | Per-bird actions. |
| POST | `/api/shots/confirm`, `/api/shots/place`, `/api/shots/reanalyse` | Bulk confirm; override country/state/city; re-identify. |
| POST | `/api/publish`, `/api/scan`, `/api/library/reindex` | Queue jobs. |
| GET | `/api/jobs`, `/api/jobs/{id}` | Job history and progress. |
| GET, DELETE | `/api/duplicates`, `/api/duplicates/{file_id}` | List duplicates; delete an extra copy (checks the SHA-256 again first). |
| GET | `/api/species?q`, `/api/locations/search?q`, `/api/locations/reverse?lat&lon` | Lookups. |
| GET, PUT | `/api/settings` | Workflow settings. |

### 4.2 Worker loop (`worker.py`)

The worker is a single process with a single-threaded loop. Each tick it does, in order:
1. claims and runs one queued job;
2. re-indexes the library, at startup and then every 6 hours;
3. runs an automatic Dumpsite scan when one is due;
4. builds previews for shots that aren't waiting for analysis;
5. analyses up to 8 pending shots.

It writes a heartbeat to `worker_state`; the UI shows the worker as offline after 120 seconds without one.
Any failing step is logged, and the loop carries on after 10 seconds. SIGTERM stops it after the current
step. Jobs interrupted by a restart are requeued.

---

## 5. The inference engine (Aves Caesar)

- **Models:** pinned by `model-version.json` to revision `f9b2159306029fbaa8f6c9a7799776b422278ea2` of
  `huggingface.co/avescaesar/bird-detect-classify`. The files are `bird_detector.onnx` (307 MB),
  `bird_classifier.onnx` with `.onnx.data` (1.55 GB), `species_mapping_v2.csv` (10,859 species, names in
  70+ languages) and `model_performance.json`. The container downloads them on first start, into
  `AppData/models`.
- **Change made to the engine:** execution providers are chosen and smoke-tested **per model**. On Apple
  CoreML, for example, the detector doesn't work but the classifier does. CUDA was also added as a choice
  (`AVES_PROVIDER` = `auto`, `cuda`, `coreml`, `directml` or `cpu`). Anything that fails falls back to the CPU.
- **Measured on a Mac, CPU only:** about 3.7 GB peak memory and about 0.8 s per photo.
- **Production setup:** the NAS image uses CPU-only `onnxruntime` (the build argument `ONNXRUNTIME`).
  Identification is sent to the Windows PC.

---

## 6. Database

PostgreSQL 16 in production; SQLite for development and tests. All workflow tables live in the `public`
schema of database `aves`, owned by user `aves`.

### 6.1 Tables

| Table | Key columns | Notes |
| --- | --- | --- |
| `sessions` | `id`, `shoot_date` (unique), `title`, `location_lat`/`lon`/`label`, `country`/`state`/`city`, `analyse_without_location` | One per shooting day. |
| `shots` | `id`, `session_id`, `dump_folder`, `group_key` (unique), `captured_at` (naive camera time), `captured_at_source`, `exif_datetime`, `gps_lat`/`gps_lon` (EXIF only), `location_source`, `country`/`state`/`city`, `status`, `analysed_at`, `analysed_with_location`, `analysis_provider` (e.g. `Dml,CPU`), `analysis_error`, `analysis_attempts`, `image_width`/`image_height` (analysed image), `min_confidence`, `bird_count`, `preview_ready`, `reviewed_at`, timestamps | One captured frame. |
| `media_files` | `id`, `shot_id`, `original_filename`, `final_filename`, `filename_key` (casefolded), `extension`, `file_type`, `is_raw`, `size_bytes`, `mtime`, `sha256`, `status`, `duplicate_of_id`, `dump_relpath` (kept after publishing), `published_relpath`, camera/lens/exposure columns, `width`/`height` (EXIF), `exif` (JSON/JSONB, full), `ingested_at`, `published_at` | Every file ever ingested. |
| `detections` | `id`, `shot_id`, `box`, `box_confidence`, `predictions` (top-5), `species_id`, `confidence`, `source`, `status`, timestamps | One per bird, or per species tagged by hand. |
| `species` | `species_id`, `scientific_name`, `common_name`, `family`, `order_name`, `names` (JSON) | Seeded from the model's mapping file. |
| `library_files` | `id`, `relpath` (unique), `filename_key`, `media_file_id` (NULL for files the app didn't publish), `seen_at` | Index of `Published` for conflict checks. |
| `jobs` | `id`, `kind`, `payload`, `status`, `progress`, `total`, `message`, `result`, timestamps | Work queue. |
| `settings` | `key`, `value` (JSON) | Workflow settings that differ from the defaults. |
| `worker_state` | `name`, `heartbeat_at`, `activity`, `providers`, `model_error`, `last_scan`, `last_scan_at` | Status for the UI. |
| `alembic_version` | `version_num` | Migration state (currently `0003`). |

Enums are stored as plain strings (non-native), with names equal to values:
- **Shot status:** `awaiting_location`, `pending`, `review`, `confirmed`, `published`, `ignored`, `error`.
- **File status:** `in_dump`, `published`, `duplicate`, `missing`.
- **Detection status:** `pending`, `confirmed`, `rejected`.
- **Detection source:** `model`, `manual`.
- **Job status:** `queued`, `running`, `done`, `failed`.

### 6.2 Views (the external contract)

- **`v_published_photos`:** one row per published file per confirmed species, with 39 columns. The
  columns and their meaning are in `PUBLIC_APP_INTEGRATION.md` §4.
- **`v_species_sightings`:** one row per species in the library.

Both are **append-only**: columns are only ever added at the end, which PostgreSQL requires for
`CREATE OR REPLACE VIEW`.

### 6.3 Migrations

- Alembic migrations live in `aves_server/migrations/versions/`. `Database.migrate()` runs them whenever
  either container starts, under advisory lock `724514`.
- A database created before migrations existed is detected (it has tables but no `alembic_version`) and
  stamped `0001`.
- The migrations so far:
  - **`0001`:** baseline tables and the original views.
  - **`0002`:** adds `session_title`, `species_names`, `in_library` and `changed_at` to `v_published_photos`.
  - **`0003`:** the `featherbase_reader` group role (PostgreSQL), with SELECT on the two views and `species`.
- To add a migration: change `models.py`, autogenerate a migration (the commands are in `README.md`),
  review it for PostgreSQL, then run `tests/test_migrations.py`.
- The production migration was rehearsed on a copy of the production data before it ran.

### 6.4 Roles

| Role | Rights | Used by |
| --- | --- | --- |
| `aves` | Owner of everything (the Postgres superuser in the container) | Featherbase only. Never hand it out. |
| `featherbase_reader` | NOLOGIN group: CONNECT, USAGE on `public`, SELECT on the two views and `species` | Grant it to read-only logins. |
| `featherbase_public` | LOGIN, member of `featherbase_reader`, `default_transaction_read_only = on`, CONNECTION LIMIT 20 | The public catalogue app. The password is held by the owner. |

---

## 7. Review UI (`server/web/`)

Preact 10, TypeScript and Vite 6, with no router library: navigation uses the URL hash (`#/sessions/12?category=review&shot=345`).
`npm run build` writes to `server/aves_server/static/`, which the web container serves.

- **Pages:** Sessions (home), Session (tabs, folder filter, bulk actions, location panel), the photo viewer
  with box overlays, Duplicates, Activity (jobs and worker status) and Settings.
- **Keyboard in the viewer:** ← → to move between photos, Enter to confirm, Esc to close.
- **Brand:**
  - a "featherbase" wordmark and a working feather-on-catalogue symbol (`public/featherbase-mark.svg`);
  - the Forest/Moss/Sage/Warm Ivory palette from the brief, with text colours adjusted to pass WCAG 2.2 AA;
  - the Inter font, served locally (no Google Fonts);
  - UK English;
  - the tagline "Your birds. Beautifully organised."
- **Honest AI labels:** each bird is shown as "AI suggestion", "Corrected by you", "Confirmed by you" or
  "Added by you". Low confidence is stated in words, and confidence never displays 100%.
- **Limitations:**
  - the interface is English only;
  - the viewer's low-confidence marker uses a fixed 0.6 instead of the setting;
  - there's no upload page.

---

## 8. Windows GPU node

Identification can run on a Windows PC's graphics card through **DirectML** (`onnxruntime-directml`),
which works with NVIDIA, AMD and Intel cards through the normal driver. It's in production on
`192.168.0.133:8096`, with DirectML for both models.

- **Protocol:**
  - `GET /health` returns the providers and the model revision.
  - `POST /predict` takes multipart data: up to 16 full-resolution JPEGs, plus `meta`, a JSON list of
    `{latitude, longitude, datetime}`. It returns detections in the same shape as the local pipeline.
  - Both require `Authorization: Bearer <token>`; the same token is `AVES_REMOTE_TOKEN` on the NAS.
  - A paused or loading node returns `503`.
  - The NAS refuses a node whose model revision differs from its own.
- **Fallback** (`AVES_REMOTE_FALLBACK`): `local` runs on the NAS while the PC is unavailable, retrying the
  PC every 60 seconds; `wait` holds photos until the PC is back. Production uses `local`.
- **Tray app:** setup registers a per-user autostart entry (`HKCU\…\Run\FeatherbaseNode`). The tray menu
  offers Pause (unloads the models and frees GPU memory), Resume, Start with Windows, Open folder, Open log
  and Quit. The log is `node.log` in the node's folder.
- **Install:**
  1. Build `dist/featherbase-node.zip` with `python server/windows/build_node_bundle.py`. The build checks
     that the zip imports and launches correctly.
  2. Extract the zip on the PC (production: `C:\aves-inference-node`).
  3. Run `start-inference-node.ps1` as Administrator. It creates a virtual environment, installs
     packages, downloads the models, creates `node-token.txt`, opens firewall port 8096 for private
     networks only, and starts the tray app.
- **The node only does identification.** It can't see the photo library and makes no previews. See the
  earlier discussion: previews aren't GPU work.

---

## 9. Deployment (production)

| Item | Value |
| --- | --- |
| Host | TrueNAS SCALE 25.10.7, 6 cores, 31 GB RAM, x86_64, Docker 28.3, no NVIDIA runtime. LAN address `192.168.0.200`. |
| Portainer | CE 2.45.3 (TrueNAS app on `:31015`). Endpoint `local`, id 3. |
| Stack | `aves-caesar`, id 37. **Git-backed:** repository above, branch `truenas-workflow`, compose file `server/compose.yaml`. |
| Containers | `aves-caesar-db-1` (postgres:16-alpine), `aves-caesar-web-1`, `aves-caesar-worker-1` (both image `featherbase:latest`, built on the NAS; `pull_policy: build` rebuilds on every deploy). |
| Ports | Web UI `8095`; PostgreSQL `5433`. |
| Volumes | `/mnt/MasterPool/Wildlife` → `/data/photos` (Dumpsite `Dumpsite`, library `Final_Published`, in one mount so moves are instant renames). `/mnt/MasterPool/Wildlife/AppData` → `/data/appdata` (models, previews). Docker volume `aves-caesar_aves-db` (name pinned) → Postgres data. |
| Runs as | `3000:3000` (user `jiwateshwar`, who owns the dataset). |
| Key stack variables | `PHOTOS_DATASET`, `DUMP_FOLDER`, `PUBLISHED_FOLDER`, `APPDATA_PATH`, `PUID`/`PGID`, `POSTGRES_PASSWORD`, `POSTGRES_PORT`, `AVES_PORT`, `AVES_PASSWORD` (empty), `ONNXRUNTIME=onnxruntime>=1.20`, `AVES_PROVIDER=auto`, `AVES_REMOTE_URL`, `AVES_REMOTE_TOKEN`, `AVES_REMOTE_FALLBACK=local`, `TZ=Asia/Kolkata`. All are documented in `stack.env.example`. |
| Data today | 4 sessions, 136 shots, 136 files, 110 detections, 14 published shots (34 view rows, 12 species; 33 of 34 files are Nikon NEF). |

- **Deploying:** push to `truenas-workflow`, then redeploy the stack. **Portainer's 5-minute GitOps
  polling has not been firing.** The stack was created through the API, pushes are not picked up, and every
  deploy since the first has been triggered by hand with `PUT /api/stacks/37/git/redeploy?endpointId=3`.
  The owner has been advised to re-save *GitOps updates* in the Portainer UI.
- **Where credentials live:** the owner's clone has a git-ignored `.secrets/deploy.env`, holding the
  TrueNAS API key, Portainer token, database passwords and node token. Database backups taken before
  migrations are in `.secrets/backups/`.

---

## 10. Invariants that must survive any change or merge

1. **The Dumpsite is read-only during ingest.** Files leave it only on publish, or when the photographer
   deletes a duplicate.
2. **Publishing never overwrites**, moves a shot's files all together or not at all, and updates the
   database after each file.
3. **Filenames never change**, except for the user-chosen conflict suffix. They are unique
   (case-insensitively) across the whole library.
4. **Published files never move.** `published_relpath` is stable.
5. **Schema changes only through migrations**, rehearsed on PostgreSQL. **The views only ever gain columns.**
6. **Only Featherbase writes** the library and its tables. Other apps read through `featherbase_reader`
   logins.
7. **Identification is AI-assisted and human-confirmed.** It's presented honestly, and photos are never
   sent to cloud AI services.
8. **Exact locations are private by default.** The original files contain GPS in EXIF.

---

## 11. Tests and development

- `cd server && pytest`: 40 tests (1 skipped), with real ExifTool-written test photos.
  - Optional real-model end-to-end: set `AVES_TEST_PHOTOS` (a folder with `kingfisher.jpg` and `heron.jpg`).
  - PostgreSQL: set `AVES_TEST_DATABASE_URL=postgresql+psycopg://…`.
- **Areas covered:** the scanner (settling, pairs, duplicates, missing files, an unmounted dataset),
  locations, review, publishing (patterns, sidecars, conflicts, pairs), the API, migrations (a fresh
  database, a database from before migrations, data preserved), the engine (remote node, fallback, pause,
  a photo that breaks the model), and media parsing.
- **Running locally:** see `README.md` → Development. Set the `AVES_*` paths, run
  `uvicorn aves_server.api:app` and `python -m aves_server.worker`, and `npm run dev` in `web/` (proxied
  to `:8080`).
- The original desktop app's tests (`/tests`) still pass, apart from 2 failures that already existed upstream.

---

## 12. Known gaps and open issues

| Area | Status |
| --- | --- |
| Portainer auto-deploy | Not firing; deploys are triggered by hand (§9). |
| CI (`server.yml`) | Written, but not confirmed to run on the fork (Actions may be disabled). Publishing to GHCR is unused. |
| PostgreSQL testing | Rehearsed against production data on the NAS; the regular suite runs on SQLite unless `AVES_TEST_DATABASE_URL` is set. |
| Windows tray app | Running in production; autostart after a reboot or sign-in not yet confirmed. |
| `box` coordinates | Relative to the analysed image; its size (`shots.image_width/height`) isn't in the views yet. |
| Uploads | None. Photos arrive only through the Dumpsite folder (SMB). A cloud demo would need an upload page. |
| Authentication | A single optional shared password (HTTP Basic). No user accounts, no multi-user support. |
| UI translation | English only (species names are multilingual). |
| Throughput | Ingest is single-threaded and hashes every file. No timing breakdown yet (CPU vs network vs GPU). |
| Geocoding | The nearest GeoNames place may be a neighbourhood; the photographer can correct it. |
| Name | "Featherbase" isn't trademark-cleared. |
| Videos | Ignored by design. |

---

## 13. Planning the merge

"Merging" can mean three very different things. Choose deliberately.

### Option A: two apps, one contract (recommended starting point)

Keep two codebases and two stacks, joined only through the read-only views and the read-only library mount.
- **Upsides:** no risk to Featherbase's data or uptime, independent release cycles, and the public app can
  be any technology.
- **To keep in mind:** the public app keeps its own state (visibility, derivatives) in its own database,
  keyed by `file_id` and `shot_id`. This is what `PUBLIC_APP_INTEGRATION.md` describes.

### Option B: one repository, separate services

Move the public app into this repository (for example `catalogue/` beside `server/`), and optionally into the
same stack as a fourth service.
- **Upsides:** shared CI, a shared brand (`brand.ts`, the palette, the logo) and shared docs.
- **Rules:**
  - the public service still connects as a `featherbase_reader` login and mounts the library read-only;
  - its own tables go in **its own schema or database**, never in `public`;
  - joining the `aves-caesar` stack means it redeploys whenever Featherbase does;
  - keep using the host IP, or a stack-internal network if it joins the stack.

### Option C: one application

The public catalogue becomes part of Featherbase, sharing the Python package and the database.
- **What it would take:**
  - user accounts and roles (owner versus visitor), since today there is one shared password;
  - a public-visibility model in Featherbase's schema, added through migrations, with privacy rules
    enforced in SQL (for example a `v_public_photos` view);
  - a public API separate from the admin API;
  - public serving hardened for the internet: rate limits, a cache, no admin routes reachable.
- **Risk:** the largest of the three. A public-facing bug could expose the private workflow, or the exact
  GPS data in the originals.

### Whatever you choose

- **Never break the invariants in §10.** Never write to Featherbase's tables except through Featherbase's
  code and migrations.
- **Ask for new data as view columns** through a Featherbase migration (for example `analysed_width` and
  `analysed_height` for `box`). Don't read the internal tables.
- **Share the brand on purpose.** It lives in `server/web/src/brand.ts`, `server/aves_server/brand.py`,
  `server/web/src/styles.css` (the colour tokens) and `server/web/public/featherbase-mark.svg`.
- **Expect redeploys.** Featherbase restarts its database on every redeploy, so handle reconnects.
- **AGPL:** a merged, network-served application must offer its source to users.
