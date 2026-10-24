# Featherbase

**Your birds. Beautifully organised.**

Featherbase is a self-hosted, bird-first photo workflow. It identifies species with AI assistance using the
open-source [Aves Caesar](https://www.aves-caesar.com) models, and you review every suggestion. It's designed to run on
TrueNAS SCALE as a Portainer stack.

```
 Dumpsite/ ──► ingest ──► day sessions ──► identify (GPU) ──► you review ──► Published Pics/
 (camera dumps)  EXIF, hash,   by shot date    detector +         confirm,          country/state/city/
                 duplicates                    classifier         correct, rename   date/filetype/
```

1. **Watch.** The worker polls the dumpsite (every 30 s by default) and ingests a photo once it has stopped
   changing, so cards still being copied are never read half-way. *Scan now* in the UI does it immediately.
   Videos and NAS clutter (`@eaDir`, `.recycle`, `._*` …) are ignored.
2. **Ingest.** Every file is hashed (an identical copy dumped twice is flagged under *Duplicates*), its full
   EXIF/XMP/IPTC metadata is stored, and it is grouped into a **session per shooting day** using the EXIF
   capture date. A RAW and the JPEG shot with it become one *shot*, reviewed once.
3. **Locate.** Photos with GPS are located offline (GeoNames, nothing leaves your network). For photos
   without GPS the session asks you where they were taken: search a place name or paste coordinates. You
   can also choose *Identify without location*. Setting a location later re-identifies the shots you haven't
   touched, since the model is more accurate with one.
4. **Identify.** The Aves Caesar detector and 10,859-species classifier suggest a species for every bird, on
   your own hardware (NAS, NVIDIA card or a Windows PC's GPU). Nothing is sent to a cloud service.
5. **Review** per session and per folder, in tabs: *To review*, *Low confidence* (top guess below 60%,
   configurable), *No bird*, *Needs location*, *Name conflicts*, *Confirmed*, *Published*, *Left in dump*.
   For each bird you can confirm, pick one of the model's alternatives, search any species, or mark it
   "not a bird". You can also tag a species the detector missed. *Confirm all confident* handles the easy
   ones in bulk. Keyboard: `←` `→` to move, `Enter` to confirm, `Esc` to close.
6. **Publish.** Confirmed shots are moved to `Published Pics/{country}/{state}/{city}/{YYYY-MM-DD}/{FILETYPE}/`
   (pattern configurable). XMP sidecars travel with their photo. **Filenames are never changed**, except when
   you resolve a conflict.
7. **Conflicts.** A filename may exist only once in the whole library, case-insensitively, counting files
   already in *Published Pics* that this app didn't put there. A clashing shot is held back (a RAW+JPEG pair
   never gets split) until you append a suffix of your choice, e.g. `DSC_0001.NEF` → `DSC_0001_trip2.NEF`.
   The same suffix is applied to every file of the shot.

Everything, including species, confidence, location, camera, lens, exposure and full EXIF, is stored in
PostgreSQL so other applications can use it (see [Using the database](#using-the-database)).

## Safety

- The dumpsite is only ever read during ingest. Files leave it only when you publish, or when you delete an
  identical duplicate yourself.
- Publishing never overwrites. On one filesystem a move is a hard link plus unlink, which fails if the name
  exists. Across filesystems the file is copied, its SHA-256 is verified against the original, and only then
  is the source removed.
- The database is updated after each file is moved, so it never claims more than what is on disk.
- If the dumpsite or library suddenly looks empty (for example, an unmounted dataset), nothing is marked as
  missing or forgotten.

## Install on TrueNAS SCALE

### 1. Choose where the models run

| Option | Speed | Setup |
| --- | --- | --- |
| **NVIDIA card in the NAS** | Fast | *Apps → Configuration → Settings → Install NVIDIA Drivers* (TrueNAS 24.10 or later); check with `nvidia-smi`. Deploy with `compose.nvidia.yaml` added (step 3). |
| **GPU of a Windows PC on the network** | Fast while the PC is on | Run the inference node on the PC; see [Using a Windows PC's GPU](#using-a-windows-pcs-gpu). |
| **NAS CPU only** | Slow but works | Nothing to do: `compose.yaml` on its own. |

The worker checks the processor on every start. If the GPU can't be used (no driver, driver mismatch, card
removed), each model falls back to the CPU on its own. The status pill in the top bar shows what's in use, e.g.
`CUDA`, `PC · DirectML` or `CPU`. If the GPU fails mid-run, the worker reloads the models and carries on on the
CPU. A photo that keeps breaking the model is retried 3 times, then parked in the *Errors* tab, so it can't
hold up the rest.

### 2. Create the datasets

- A **photos dataset**, e.g. `tank/photos`, with two folders: `Dumpsite` and `Published Pics`. Share it over
  SMB so your computer can copy camera cards into `Dumpsite`. Both folders must be in the same dataset:
  the stack mounts the dataset once, so publishing is an instant rename rather than a copy.
- An **appdata dataset**, e.g. `tank/apps/aves-caesar`, for the model files (~1.8 GB), previews, and the
  database volume.

Grant the user the containers run as (`PUID`/`PGID`, default `568` = TrueNAS `apps`) **modify** permission on
both. If your SMB share is owned by your own user, set `PUID`/`PGID` to that user's IDs instead, so published
files stay owned by you.

### 3. Deploy the stack in Portainer

**Prebuilt image (easiest).** Push this repository to your own GitHub fork. The *Workflow server* GitHub
Action runs the tests against PostgreSQL and publishes `ghcr.io/<your-user>/featherbase:latest`.
If the package is private, make it public or add a registry login in Portainer. Then paste
`server/compose.yaml` into *Stacks → Add stack → Web editor*, delete its `build:` block, and set
`AVES_IMAGE=ghcr.io/<your-user>/featherbase:latest`.

**From Git.** Go to *Stacks → Add stack → Repository*, enter your fork's URL, branch `truenas-workflow`, and
compose path `server/compose.yaml`. Portainer builds the image on the NAS from the Dockerfile.

**Or build on the NAS.** In the TrueNAS shell:
```sh
git clone <your fork> aves && cd aves && git checkout truenas-workflow
docker build -f server/Dockerfile -t featherbase:latest .
```
Then paste `server/compose.yaml` into *Stacks → Add stack → Web editor* and delete its `build:` block.

In every case, fill in the environment variables from [`stack.env.example`](stack.env.example). At minimum
set `PHOTOS_DATASET`, `APPDATA_PATH` and `POSTGRES_PASSWORD`.

On the first start the worker downloads the models from Hugging Face. That's the only time it needs the
internet, and its log shows the progress. Then open `http://<truenas-ip>:8095`. The status pill in the top
bar shows which processor each model runs on, e.g. `CUDA`.

**NVIDIA card in the NAS:** with a Repository stack, add `server/compose.nvidia.yaml` under *Additional paths*.
With the Web editor, add the `environment` and `deploy` lines from `compose.nvidia.yaml` to the `worker`
service. Leave this out on a NAS without the card: Docker refuses to start a container that reserves a GPU it
can't find.

The image works with or without a GPU. If you'll never use a GPU in the NAS, build with
`--build-arg ONNXRUNTIME="onnxruntime>=1.20"` for an image about 2 GB smaller.

## Using a Windows PC's GPU

A PC with a graphics card can do the identification for the NAS. The NAS still does everything else: it
reads the dumpsite, extracts RAW previews, stores results and moves files. It sends each photo to the PC
(full resolution, over your LAN only) and gets the detected birds back.

On the PC:
1. Install the latest graphics driver and [Python 3.11 or newer](https://www.python.org/downloads/windows/).
2. Get this repository (`git clone`, or download the ZIP of the `truenas-workflow` branch).
3. In PowerShell, in the repository folder, run:
   ```powershell
   powershell -ExecutionPolicy Bypass -File server\windows\start-inference-node.ps1
   ```
   The first run installs the packages, downloads the models (~1.8 GB) and creates a token in
   `node-token.txt`. It uses DirectML, which works with NVIDIA, AMD and Intel cards and needs no CUDA install.
   Run it once as Administrator to open port 8096 in Windows Firewall, private networks only.
4. It prints the two values to add to the stack's environment variables on the NAS. Then redeploy:
   ```
   AVES_REMOTE_URL=http://192.168.1.50:8096
   AVES_REMOTE_TOKEN=<from node-token.txt>
   ```

When the PC is off or asleep, the worker falls back to its own processor and tries the PC again every minute.
Set `AVES_REMOTE_FALLBACK=wait` to hold photos in *Queued* until the PC is back instead. That suits a NAS with
a weak CPU or little memory, because the NAS never loads the models itself. The PC and the NAS must use the
same model version (`model-version.json`); the NAS refuses a node with different models, because species
IDs could differ.

After setup the node runs as a **tray app** and starts by itself whenever you sign in to Windows. Right-click
the Featherbase icon to:
- **Pause** it. This unloads the models and frees the graphics card's memory, e.g. for gaming or editing.
  The NAS treats a paused PC like a switched-off one. **Resume** loads the models again in a few seconds.
- Turn **Start with Windows** on or off.
- Open the node's folder (token, settings) or its log.
- **Quit**.

The icon's dot shows the state: green means running, yellow means loading, grey means paused, red means
error. Hovering shows how many photos it has identified.

To update the node, unzip a newer `featherbase-node.zip` over the same folder and run the setup command
again. The models and token are kept, and the running copy is replaced. Use `-Foreground` to run it in the
console instead, and `-NoAutostart` to skip starting with Windows. Build the zip with
`python server/windows/build_node_bundle.py`.

### Updating

Pull or redeploy the stack. The database schema is created automatically. Models are only downloaded again
when `model-version.json` changes.

## Using the database

Other applications, such as a public catalogue in its own Portainer stack, read Featherbase's PostgreSQL
**through views, with a read-only login**. The views are a versioned contract, managed by migrations
(see [Schema changes](#schema-changes)): new columns are only ever added at the end, and existing ones
keep their meaning.

**Connecting:** host `<truenas-ip>`, port `POSTGRES_PORT` (default `5433`), database `aves`. Use a login
that belongs to `featherbase_reader`, never the `aves` owner. That role can read `v_published_photos`,
`v_species_sightings` and `species`, and nothing else: no internal tables, no full EXIF, no writes. To
create a login for another app:
```sql
CREATE ROLE my_app LOGIN PASSWORD '...' IN ROLE featherbase_reader;
ALTER ROLE my_app SET default_transaction_read_only = on;
```

### `v_published_photos`

One row per published file **per confirmed species**. A shot with three birds gives three rows, and a
RAW+JPEG pair gives one row per file. Files without a species appear once, with empty species fields.

| Group | Columns |
| --- | --- |
| File | `file_id`, `filename` (name in the library, including any conflict suffix), `original_filename`, `file_type` (`JPG`, `NEF`, `CR3`, …), `published_relpath` (relative to the published folder), `sha256`, `size_bytes`, `width`, `height`, `published_at`, `in_library` (false once the file is gone from the folder) |
| Shot | `shot_id` (groups a RAW+JPEG pair), `captured_at` (camera local time), `shoot_date`, `session_title` |
| Place | `latitude`, `longitude` (**exact**, from the photo's GPS or the session's location), `location_source` (`exif` or `session`), `country`, `state`, `city` |
| Species | `detection_id`, `species_id`, `common_name` (English), `scientific_name`, `family`, `order_name`, `species_names` (JSON: language code → name), `model_confidence`, `identification_source` (`model` = AI suggestion you confirmed, `manual` = tagged by you), `box` (`[x0, y0, x1, y1]` in pixels of the analysed image) |
| Camera | `camera_make`, `camera_model`, `lens_model`, `focal_length`, `f_number`, `exposure_time`, `iso` |
| Sync | `changed_at`: the latest change to the file, its shot, its day or its identification. Poll `WHERE changed_at > :last_seen` and also check `in_library`. |

### `v_species_sightings`

One row per species in the library: `species_id`, `scientific_name`, `common_name`, `family`, `shots`,
`first_seen`, `last_seen`.

### `species`

All 10,859 species the model knows: `species_id`, `scientific_name`, `common_name`, `family`,
`order_name`, `names` (JSON, 70+ languages).

```sql
-- One card per shot for a species page
SELECT shot_id, captured_at, session_title, city, state, country,
       min(published_relpath) FILTER (WHERE file_type = 'JPG') AS jpeg, min(published_relpath) AS any_file
FROM v_published_photos WHERE species_id = '264' AND in_library
GROUP BY shot_id, captured_at, session_title, city, state, country ORDER BY captured_at DESC;
```

**Things your app has to handle itself:**
- The original files still contain their exact GPS position and full EXIF, so strip them before serving.
- Browsers can't display RAW files (`NEF`, `CR3`, …). Make web images from the shot's JPEG, or from the
  RAW's embedded preview.
- Mount the published folder **read-only** in your app's stack. Featherbase is the only writer.

The REST API at `http://<truenas-ip>:8095/docs` is the Featherbase UI's private admin API. It isn't meant
for public use.

### Schema changes

Migrations live in `aves_server/migrations/versions/` and run automatically when the containers start,
under a lock so the web and worker containers never migrate at the same time. A database created before
migrations existed is recognised and recorded as revision `0001`, without changing anything. To change
the schema, edit `models.py`, then add a migration. For example, from `server/`:
```sh
PYTHONPATH=.:../backend python -c "from alembic import command; from alembic.config import Config; from sqlalchemy import create_engine; \
  from aves_server.db import MIGRATIONS; e = create_engine('sqlite:///scratch.db'); c = Config(); c.set_main_option('script_location', str(MIGRATIONS)); \
  conn = e.connect(); c.attributes['connection'] = conn; command.upgrade(c, 'head'); command.revision(c, 'describe the change', autogenerate=True)"
```
Review the generated file, especially for PostgreSQL, before committing it. `tests/test_migrations.py`
checks that the migrations still match the models.

## Settings

Under *Settings* in the UI:
- the low-confidence threshold
- the folder pattern: `{country}`, `{state}`, `{city}`, `{date}`, `{year}`, `{month}`, `{filetype}`
- the folder name used for unknown places
- the smallest place to use as the "city", which avoids suburbs and neighbourhoods
- the language for species names (70+ languages)
- the dumpsite polling interval and settle time

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements-cpu.txt -e "server[dev]"
python scripts/setup/install_exiftool.py
python scripts/models/download_release_models.py --output-dir models   # for real identification

export AVES_DATA_DIR=/tmp/aves AVES_DUMP_DIR=/tmp/aves/dump AVES_PUBLISHED_DIR=/tmp/aves/published AVES_MODELS_DIR=$PWD/models
export PYTHONPATH=$PWD/server:$PWD/backend
uvicorn aves_server.api:app --port 8080 &   # SQLite by default; set AVES_DATABASE_URL for Postgres
python -m aves_server.worker &
cd server/web && npm ci && npm run dev       # UI with hot reload, proxied to :8080
```

Tests: `cd server && pytest`. Add `AVES_TEST_DATABASE_URL=postgresql+psycopg://…` to run them against
Postgres. Add `AVES_TEST_PHOTOS=<folder with kingfisher.jpg and heron.jpg>` to also run the real-model
end-to-end test.

## Name

"Featherbase" is a working name. An ornithological feather database already uses it
(featherbase.info), so trademark, domain and confusion checks are needed before a public launch. The
name lives in `aves_server/brand.py` and `web/src/brand.ts`, so it can change without touching the
rest. Internal identifiers (the `aves_server` package, `AVES_*` variables, the `aves` database and the
`aves-caesar` stack and volume) are deliberately left unchanged so existing installs keep their data.

## Licence

AGPL-3.0-or-later, like Aves Caesar, whose models and engine Featherbase builds on. If you let other people use your instance over a network, the licence
requires you to offer them the source code of your modified version.
