# Featherbase → public catalogue: integration brief

You are building the **public bird-photo catalogue**: a separate application in its own Portainer stack
on the same TrueNAS server as **Featherbase**. This document describes what Featherbase provides, what
your app owns, and the rules for reading Featherbase's data. Treat it as the interface contract. If you
need something that isn't here, ask the owner to have it added on the Featherbase side. Do not work
around it.

## 1. What Featherbase is

Featherbase is the photographer's private workflow app. It is already deployed and owns everything up to
and including filing photos in the library:

```
camera dump ──► Dumpsite/ ──► Featherbase: ingest, EXIF, day sessions, AI species suggestions
                               (Aves Caesar models), human review and correction
                                   │ publish = move the file and record it
                                   ▼
                    Final_Published/{country}/{state}/{city}/{YYYY-MM-DD}/{FILETYPE}/{filename}
                    PostgreSQL "aves" ── views ──► YOUR APP (read-only)
```

Featherbase is the **only writer** of the photo library and the database. Your app only reads.

### Who owns what

| Concern | Owner |
| --- | --- |
| Ingest, species identification, review, corrections, filing into `Final_Published` | Featherbase |
| The database schema and the views you read | Featherbase (changes go through its migrations) |
| **Public visibility**: which photos and species are shown, and the publishing workflow for that | **Your app** (Featherbase has no public flag, on purpose) |
| **Web images**: thumbnails and display sizes, EXIF stripping, watermarking | **Your app** (Featherbase provides none) |
| **Location privacy**: precision shown, sensitive species, map display | **Your app** |
| Public UI, search, species pages, photographer attribution | Your app |

## 2. Environment

| Item | Value |
| --- | --- |
| Host | TrueNAS SCALE 25.10, x86_64, Docker 28, managed with Portainer CE 2.45 (endpoint "local", id 3) |
| NAS LAN address | `192.168.0.200` |
| PostgreSQL | 16 (Alpine), host port **5433**, database **`aves`** |
| Featherbase stack | Portainer stack `aves-caesar` (containers `aves-caesar-web-1`, `-worker-1`, `-db-1`). Do not modify it. |
| Ports already taken on the host | 3000, 3681, 5000, 5433, 6010, 8080, 8095, 8102, 8181, 8686, 9200, 9201, 9443, 20489, 30041, 30052, 30070, 30103, 31015, 31054, 31055. Pick a free one, and check again before deploying. |
| Photo dataset | `/mnt/MasterPool/Wildlife` (POSIX ACLs). The library is `/mnt/MasterPool/Wildlife/Final_Published`. |
| File ownership | Files and folders are owned by `3000:3000` (user `jiwateshwar`). Run your container as `3000:3000`, or as a user that can read them. |
| Timezone | `Asia/Kolkata` |

## 3. How to access the data

### 3.1 Database: read-only login

| Setting | Value |
| --- | --- |
| Host / port | `192.168.0.200` / `5433` (reach it through the host address; there is no shared Docker network) |
| Database | `aves` |
| User | `featherbase_public` |
| Password | Supplied separately by the owner. Keep it in your stack's environment variables, never in code or git. |

- **Group role:** the login belongs to `featherbase_reader`.
- **Readable objects:** it can read only `v_published_photos`, `v_species_sightings` and `species`.
- **Everything else is refused:** sessions are read-only by default, and internal tables, full EXIF, writes and DDL are all denied.
- **Connection limit:** 20. Use a small pool (2–5 connections) and reconnect with backoff. The database restarts whenever the Featherbase stack is redeployed.
- **Never** use or ask for the `aves` owner login.
- **Never** create tables, views or functions in the `aves` database. Keep your own state (visibility choices, derivative image index, caches) in **your own database** inside your stack.
- **Don't use** the REST API at `:8095/docs`. It's Featherbase's private admin API, it isn't a public contract, and it can change or require auth at any time.

### 3.2 Files: read-only mount

Mount the library read-only, and read the image files only:

```yaml
volumes:
  - /mnt/MasterPool/Wildlife/Final_Published:/photos:ro
```

- A file's path is `/photos/<published_relpath>`, using `published_relpath` from the view.
- **Use the database to discover and describe photos. Don't scan or parse folder paths.**
  - The folder pattern is configurable in Featherbase, so paths aren't a stable format.
  - Species, confidence and camera data exist only in the database.
- XMP sidecars (`<name>.xmp` or `<stem>.xmp`) may sit next to photos. Ignore them, or read them if useful.
- Filenames are unique across the whole library, case-insensitively. A clash on import gets a user-chosen
  suffix, e.g. `DSC_0001_trip2.NEF`, so `filename` can differ from `original_filename`.
- Published files are never moved or renamed by Featherbase. A file can disappear only if someone deletes
  it by hand; then `in_library` turns false (see §5).

## 4. The data contract

The objects below are a **versioned contract**: columns are only ever appended, and existing columns keep
their names, types and meaning. Read them by column name, not position. Featherbase migrations are at
`server/aves_server/migrations/versions/` in the Featherbase repository (0001 baseline, 0002 added
`session_title`, `species_names`, `in_library` and `changed_at`, 0003 created the reader role).

### 4.1 `v_published_photos`

One row per **published file per confirmed species**.

- **Several birds:** a shot with three species gives three rows.
- **RAW+JPEG pair:** one row per file, with the same `shot_id`.
- **No species:** a published photo without a confirmed species (the user may publish "no bird" photos)
  gives one row with NULL species columns.

| Column | Type | Meaning |
| --- | --- | --- |
| `file_id` | integer | Stable id of the file. Primary key for files. |
| `filename` | varchar | Name in the library (may include a conflict suffix). |
| `original_filename` | varchar | Name as it came off the camera. |
| `file_type` | varchar | Normalised extension, upper case: `JPG`, `HEIC`, `TIFF`, `PNG`, or a RAW type such as `NEF`, `CR3`, `ARW`, `DNG`, `RAF`, `ORF`, `RW2`. |
| `published_relpath` | varchar | Path relative to the published folder (see §3.2). |
| `sha256` | varchar | Content hash. Use it to detect content changes and to name derivative caches. |
| `size_bytes` | bigint | File size. |
| `camera_make`, `camera_model`, `lens_model` | varchar | From EXIF; may be NULL. |
| `focal_length`, `f_number`, `exposure_time` | double | mm, f-number, seconds (e.g. `0.0008` = 1/1250 s). |
| `iso` | integer | |
| `width`, `height` | integer | Image size as recorded in EXIF. For RAW this is the sensor size, not the size of any preview. |
| `published_at` | timestamptz | When Featherbase moved the file into the library. |
| `shot_id` | integer | Stable id of the captured frame. **Group by this for photo cards.** |
| `captured_at` | timestamp (**no time zone**) | Camera local time from EXIF, as shown on the camera. Display it as is; don't convert it. |
| `shoot_date` | date | The day the session is filed under. |
| `session_title` | varchar | The photographer's name for that day/trip; often NULL. |
| `latitude`, `longitude` | double | **Exact** coordinates, from the photo's GPS or the location the user set for the day. **Never publish them as is.** |
| `location_source` | varchar | `exif` = the photo's own GPS; `session` = one point the user chose for the whole day (approximate, shared by many photos); NULL = unknown. |
| `country`, `state`, `city` | varchar | Offline reverse-geocoded names (GeoNames). The user may have corrected them. `city` can be a neighbourhood or village. |
| `detection_id` | integer | Id of this bird in the photo; NULL when there's no species. |
| `species_id` | varchar | Stable species id from the model taxonomy (e.g. `'264'` = Common Kingfisher). Join to `species`. |
| `common_name` | varchar | English common name. |
| `scientific_name`, `family`, `order_name` | varchar | e.g. `Alcedo atthis`, `Alcedinidae (Kingfishers)`, `Coraciiformes`. |
| `species_names` | jsonb | Common names keyed by language code (70+ languages), e.g. `species_names->>'hi'`. |
| `model_confidence` | double | The AI model's confidence for this species, from 0 to 1. NULL for hand-tagged species. **A human has confirmed every row anyway**, so don't present this as accuracy. |
| `identification_source` | varchar | `model` = an AI suggestion the photographer confirmed; `manual` = tagged by the photographer. |
| `box` | jsonb | `[x0, y0, x1, y1]`: the bird's position in pixels of the image Featherbase analysed. For RAW files that is the embedded preview, so it isn't necessarily `width`×`height`. The analysed image's size isn't exposed yet, so **don't rely on `box` for cropping**; ask for that column to be added if you need it. NULL for hand-tagged species. |
| `in_library` | boolean | False once the file is no longer in the published folder (deleted by hand). Hide those rows. |
| `changed_at` | timestamptz | The latest change to the file, its shot, its day or its identification. Use it for incremental sync, but see §5. |

### 4.2 `v_species_sightings`

One row per species present in the published library: `species_id`, `scientific_name`, `common_name`,
`family`, `shots` (bigint, the number of published shots), `first_seen`, `last_seen` (timestamp without time zone,
camera local time). This view knows nothing about your visibility rules, so recompute these figures over
visible photos if you hide any.

### 4.3 `species`

The full taxonomy the model knows: **10,859 species**, with `species_id`, `scientific_name`,
`common_name`, `family`, `order_name` and `names` (jsonb, 70+ languages). Use it for search and species
pages, including species with no photos yet.

### 4.4 Data facts today (they will change)

- **Small so far:** 34 rows, 14 shots and 12 species. 3 shots contain more than one species, and no
  published photo is currently species-less.
- **Mostly RAW:** 33 of the 34 published files are **Nikon NEF RAW** and only 1 is a JPEG.
- **Locations:** all current rows have `location_source = 'session'`.

## 5. Keeping your copy in sync

Featherbase changes the library in only a few ways:

- **New photos are published.** New rows appear.
- **A late-arriving file joins a published shot**, e.g. the RAW of a pair dumped later. New rows appear with an existing `shot_id`.
- **The user edits a session's title.** `session_title` and `changed_at` change.
- **A file is deleted from the library by hand.** `in_library` becomes false. Featherbase re-indexes the library at startup and every 6 hours, so this can lag by up to 6 hours, **and it does not update `changed_at`**.

Published shots are otherwise not edited in Featherbase: species can't be changed after publishing.

Recommended approach:
1. **Incremental:** poll every few minutes for `WHERE changed_at > :last_sync` (keep an overlap of a few
   minutes; it's a timestamp, not a sequence).
2. **Reconcile:** about hourly, read `SELECT file_id, shot_id, detection_id, sha256, in_library FROM v_published_photos`
   (cheap) and compare it with your own index. Pick up `in_library` flips and rows that have gone, and
   rebuild a file's derivatives when its `sha256` changes.
3. Key your own records by `file_id` (files), `shot_id` (photos shown to the public) and `(shot_id,
   species_id)` (species appearances).

## 6. Rules your app must follow

1. **Read-only, always.** Use the database login read-only and mount the folder read-only. Never write to `Final_Published`, Featherbase's database or its app-data folder.
2. **Strip EXIF and XMP from every image you serve.** The originals contain the photographer's **exact GPS
   position**, camera serial numbers and other private data. Never serve or link the original files.
3. **Build web images yourself**, from the shot's JPEG if there is one, otherwise from the RAW. Most of the
   library is RAW, so you need a RAW path. The fast way is to extract the RAW's embedded JPEG preview, e.g.
   with ExifTool (`-JpgFromRaw` / `-PreviewImage`) or rawpy/libraw. Cache derivatives by `sha256`.
4. **Location privacy is yours to enforce.** Never expose `latitude`/`longitude` as is. Coarsen them or use
   `country`/`state`/`city`, and treat sensitive species (e.g. nesting sites, rare species) more strictly.
   With `location_source = 'session'`, the point is the user's choice for a whole day, not where the photo
   was taken.
5. **Visibility is opt-in, in your app.** Every published Featherbase photo is private until your app's
   workflow marks it public.
6. **Describe identifications honestly.** They are AI-assisted and confirmed by the photographer. Don't
   claim certainty or show confidence as accuracy. Credit the identification engine: "Species
   identification by the open-source Aves Caesar models" (aves-caesar.com).
7. **Handle Featherbase being unavailable.** A Featherbase redeploy restarts its database, so serve from
   your own cache or index and reconnect with backoff.
8. **Don't parse folder paths** for country, date or species; use the columns.

## 7. Example queries

```sql
-- Photo cards for one species (one card per shot), newest first
SELECT shot_id,
       max(captured_at)                                AS captured_at,
       max(session_title)                              AS session_title,
       max(city) AS city, max(state) AS state, max(country) AS country,
       max(published_relpath) FILTER (WHERE file_type IN ('JPG', 'HEIC', 'TIFF', 'PNG')) AS still_image,
       max(published_relpath)                          AS any_file
FROM v_published_photos
WHERE species_id = '264' AND in_library
GROUP BY shot_id
ORDER BY captured_at DESC;

-- All species in a shot (for a photo page)
SELECT DISTINCT species_id, common_name, scientific_name, identification_source
FROM v_published_photos WHERE shot_id = $1 AND species_id IS NOT NULL;

-- Species index with localised names
SELECT s.species_id, s.common_name, s.names->>'hi' AS hindi_name, s.scientific_name, v.shots, v.last_seen
FROM v_species_sightings v JOIN species s USING (species_id)
ORDER BY v.shots DESC;

-- Incremental sync
SELECT * FROM v_published_photos WHERE changed_at > $1 ORDER BY changed_at;
```

## 8. Suggested stack skeleton

```yaml
name: featherbase-catalogue            # your own Portainer stack
services:
  app:
    build: .                           # or your image
    user: "3000:3000"                  # can read the library (owner 3000:3000)
    restart: unless-stopped
    environment:
      FEATHERBASE_DB_HOST: 192.168.0.200
      FEATHERBASE_DB_PORT: "5433"
      FEATHERBASE_DB_NAME: aves
      FEATHERBASE_DB_USER: featherbase_public
      FEATHERBASE_DB_PASSWORD: ${FEATHERBASE_DB_PASSWORD}   # set in Portainer, not in git
      PHOTOS_DIR: /photos
    volumes:
      - /mnt/MasterPool/Wildlife/Final_Published:/photos:ro
      - catalogue-data:/data                                 # your own derivatives, cache and index
    ports:
      - "<free host port>:8080"
  # your own database for visibility and publishing state, if you need one
volumes:
  catalogue-data:
```

## 9. Asking for changes on the Featherbase side

Featherbase can add columns or views to this contract through a migration (e.g. the analysed image size
for `box`, the RAW's embedded preview size, or the photographer's name). Ask the owner, describe what's
needed and why, and **don't** query internal tables or ask for wider grants as a shortcut.
