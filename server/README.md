# Aves Caesar Workflow (server)

A self-hosted photo workflow built on the Aves Caesar bird identification engine, designed to run on
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
4. **Identify.** The Aves Caesar detector and 10,859-species classifier run on the NVIDIA GPU.
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
Action runs the tests against PostgreSQL and publishes `ghcr.io/<your-user>/aves-caesar-workflow:latest`.
If the package is private, make it public or add a registry login in Portainer. Then paste
`server/compose.yaml` into *Stacks → Add stack → Web editor*, delete its `build:` block, and set
`AVES_IMAGE=ghcr.io/<your-user>/aves-caesar-workflow:latest`.

**From Git.** Go to *Stacks → Add stack → Repository*, enter your fork's URL, branch `truenas-workflow`, and
compose path `server/compose.yaml`. Portainer builds the image on the NAS from the Dockerfile.

**Or build on the NAS.** In the TrueNAS shell:
```sh
git clone <your fork> aves && cd aves && git checkout truenas-workflow
docker build -f server/Dockerfile -t aves-caesar-workflow:latest .
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
IDs could differ. To start the node with Windows, create a Task Scheduler task that runs the command in
step 3 "At log on".

### Updating

Pull or redeploy the stack. The database schema is created automatically. Models are only downloaded again
when `model-version.json` changes.

## Using the database

PostgreSQL is reachable on the host at `POSTGRES_PORT` (default `5433`), with database `aves`, user `aves`,
and your `POSTGRES_PASSWORD`. Remove the `ports:` entry of the `db` service to keep it private.

| View / table | What it holds |
| --- | --- |
| `v_published_photos` | One row per published file per confirmed species: filename, path in the library, species (common + scientific name, family), capture time, latitude/longitude, country/state/city, camera, lens, exposure, model confidence. |
| `v_species_sightings` | Each species in the library with its number of shots and first/last sighting. |
| `media_files` | Every file ever ingested, with its full EXIF as JSON (`exif`), SHA-256, size, and dump and published paths. |
| `shots`, `detections`, `sessions`, `species` | The workflow itself: bounding boxes, the model's top-5 per bird, review state. |

Example:
```sql
SELECT common_name, count(*) AS photos, min(captured_at) AS first
FROM v_published_photos WHERE country = 'India' GROUP BY common_name ORDER BY photos DESC;
```

The REST API used by the UI is documented interactively at `http://<truenas-ip>:8095/docs`.

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

## Licence

AGPL-3.0-or-later, like Aves Caesar. If you let other people use your instance over a network, the licence
requires you to offer them the source code of your modified version.
