import { useEffect, useState } from "preact/hooks";
import { api, downloadUrl, previewUrl, type Detection, type FileInfo, type ShotDetail } from "../api";
import { attempt, formatBytes, formatExposure, formatTime, percent } from "../util";
import { SpeciesPicker } from "./SpeciesPicker";

const BOX_COLORS = ["#4ade80", "#60a5fa", "#f472b6", "#facc15", "#a78bfa", "#fb923c"];

export function ShotViewer({ shotId, shotIds, onNavigate, onClose, onChanged }: {
  shotId: number;
  shotIds: number[];
  onNavigate: (shotId: number) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [shot, setShot] = useState<ShotDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [highlight, setHighlight] = useState<number | null>(null);
  const position = shotIds.indexOf(shotId);
  const previous = position > 0 ? shotIds[position - 1] : null;
  const next = position >= 0 && position < shotIds.length - 1 ? shotIds[position + 1] : null;

  useEffect(() => {
    setShot(null);
    setError(null);
    api.shot(shotId).then(setShot, (e) => setError(e.message));
  }, [shotId]);

  const act = async (action: () => Promise<ShotDetail>, advance = false) => {
    const updated = await attempt(action);
    if (!updated) return;
    setShot(updated);
    onChanged();
    if (advance && next !== null) onNavigate(next);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight" && next !== null) onNavigate(next);
      else if (e.key === "ArrowLeft" && previous !== null) onNavigate(previous);
      else if (e.key === "Enter" && shot && canConfirm(shot)) act(() => api.confirm(shot.id), true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [shot, next, previous]);

  return (
    <div class="viewer" role="dialog">
      <div class="viewer-image">
        {shot?.preview_ready ? (
          <div class="image-frame">
            <img src={previewUrl(shot.id)} alt="" />
            {shot.image_width && shot.image_height && (
              <svg viewBox={`0 0 ${shot.image_width} ${shot.image_height}`} preserveAspectRatio="xMidYMid meet">
                {shot.detections.map((d, i) =>
                  d.box && d.status !== "rejected" ? (
                    <rect key={d.id} x={d.box[0]} y={d.box[1]} width={d.box[2] - d.box[0]} height={d.box[3] - d.box[1]}
                          class={highlight === d.id ? "box active" : "box"} style={{ stroke: BOX_COLORS[i % BOX_COLORS.length] }} />
                  ) : null,
                )}
              </svg>
            )}
          </div>
        ) : (
          <div class="placeholder">{error ?? (shot ? "Preview not ready yet" : "Loading…")}</div>
        )}
        <button class="nav prev" disabled={previous === null} onClick={() => previous !== null && onNavigate(previous)} title="Previous (←)">‹</button>
        <button class="nav next" disabled={next === null} onClick={() => next !== null && onNavigate(next)} title="Next (→)">›</button>
      </div>

      <aside class="viewer-panel">
        <div class="viewer-head">
          <div>
            <b>{shot ? formatTime(shot.captured_at) : ""}</b>
            <small>
              {position >= 0 ? `${position + 1} of ${shotIds.length}` : ""} {shot?.folder ? `· ${shot.folder}` : ""}
            </small>
          </div>
          <button class="close" onClick={onClose} title="Close (Esc)">✕</button>
        </div>

        {shot && (
          <>
            <StatusLine shot={shot} />
            {shot.analysis_error && <div class="banner error">{shot.analysis_error}</div>}

            <section>
              {shot.detections.length === 0 && shot.analysed_at && <p class="muted">No bird detected in this photo.</p>}
              {shot.detections.map((d, i) => (
                <DetectionCard key={d.id} detection={d} color={BOX_COLORS[i % BOX_COLORS.length]} onHover={setHighlight}
                               editable={editable(shot)}
                               onSpecies={(speciesId) => act(() => api.setSpecies(shot.id, d.id, speciesId))}
                               onReject={() => act(() => api.reject(shot.id, d.id))}
                               onRestore={() => act(() => api.restore(shot.id, d.id))} />
              ))}
              {editable(shot) && shot.analysed_at && (
                <SpeciesPicker
                  placeholder={shot.bird_count ? "Add a species the detector missed…" : "Tag a species by hand…"}
                  onPick={(species) => act(() => api.addSpecies(shot.id, species.id))}
                />
              )}
            </section>

            <Actions shot={shot} act={act} />

            <section class="files">
              <h4>Files</h4>
              {shot.files.map((file) => <FileRow key={file.id} file={file} dumpsite={shot.dumpsite_name} published={shot.published_name} />)}
              {shot.has_conflict && <SuffixForm shot={shot} onRenamed={(updated) => { setShot(updated); onChanged(); }} />}
            </section>

            <section>
              <h4>Place</h4>
              <p>
                {[shot.location.city, shot.location.state, shot.location.country].filter(Boolean).join(", ") || <span class="muted">Unknown</span>}
                <small class="muted">
                  {" "}
                  {shot.location.source === "exif" ? "· from photo GPS" : shot.location.source === "session" ? "· from session location" : ""}
                </small>
              </p>
            </section>

            <Exif file={shot.files.find((f) => f.camera) ?? shot.files[0]} shot={shot} />
          </>
        )}
      </aside>
    </div>
  );
}

/** Who decided this species: the brief asks for visible AI suggestion and correction states. */
function provenance(d: Detection): string {
  if (d.status === "rejected") return "Rejected: not a bird";
  if (d.source === "manual") return d.status === "confirmed" ? "Added and confirmed by you" : "Added by you";
  const corrected = d.alternatives.length > 0 && d.species?.id !== d.alternatives[0].id;
  if (d.status === "confirmed") return corrected ? "Corrected and confirmed by you" : "AI suggestion, confirmed by you";
  return corrected ? "Corrected by you" : "AI suggestion";
}

const editable = (shot: ShotDetail) => ["review", "confirmed", "ignored"].includes(shot.status);
const canConfirm = (shot: ShotDetail) =>
  shot.status === "review" && shot.detections.every((d) => d.status === "rejected" || d.species);

function StatusLine({ shot }: { shot: ShotDetail }) {
  const labels: Record<string, string> = {
    awaiting_location: "Waiting for a location before identification",
    pending: "Queued for identification",
    review: "Waiting for your review",
    confirmed: "Confirmed · will be published",
    published: "Published",
    ignored: "Left in the dumpsite",
    error: "Could not be identified",
  };
  return (
    <div class={`status-line st-${shot.status}`}>
      {labels[shot.status] ?? shot.status}
      {shot.analysis_provider && <small> · {shot.analysed_with_location ? "with" : "without"} location · {shot.analysis_provider}</small>}
    </div>
  );
}

function DetectionCard({ detection: d, color, editable, onHover, onSpecies, onReject, onRestore }: {
  detection: Detection; color: string; editable: boolean; onHover: (id: number | null) => void;
  onSpecies: (speciesId: string) => void; onReject: () => void; onRestore: () => void;
}) {
  const [changing, setChanging] = useState(false);
  const rejected = d.status === "rejected";
  const low = d.confidence !== null && d.confidence < 0.6;
  return (
    <div class={`detection ${rejected ? "rejected" : ""}`} style={{ borderLeftColor: color }} onMouseEnter={() => onHover(d.id)} onMouseLeave={() => onHover(null)}>
      <span class={`ai-label ${d.status === "confirmed" ? "confirmed" : ""}`}>{provenance(d)}</span>
      <div class="detection-main">
        <div>
          <b>{d.species?.name ?? "Unknown"}</b>
          <i>{d.species?.scientific_name}</i>
        </div>
        {d.confidence !== null && (
          <span class={`conf ${low ? "low" : ""}`} title="How confident the model is in this species">
            {percent(d.confidence)}{low ? " · low" : ""}
          </span>
        )}
      </div>
      {!rejected && d.alternatives.length > 1 && (
        <div class="alternatives">
          {d.alternatives.map((alt) => (
            <button key={alt.id} disabled={!editable} class={alt.id === d.species?.id ? "chip active" : "chip"} onClick={() => onSpecies(alt.id)}
                    title={alt.scientific_name}>
              {alt.name} <small>{percent(alt.confidence)}</small>
            </button>
          ))}
        </div>
      )}
      {editable && (
        <div class="detection-actions">
          {rejected ? (
            <button class="link" onClick={onRestore}>Restore — it is a bird</button>
          ) : (
            <>
              <button class="link" onClick={() => setChanging(!changing)}>{changing ? "Cancel" : "Other species…"}</button>
              <button class="link danger" onClick={onReject}>{d.box ? "Not a bird" : "Remove"}</button>
            </>
          )}
        </div>
      )}
      {changing && (
        <SpeciesPicker autoFocus placeholder="Search by common or scientific name…" onPick={(species) => { setChanging(false); onSpecies(species.id); }} />
      )}
    </div>
  );
}

function Actions({ shot, act }: { shot: ShotDetail; act: (action: () => Promise<ShotDetail>, advance?: boolean) => void }) {
  if (shot.status === "review") {
    const noBird = shot.bird_count === 0;
    return (
      <div class="actions">
        <button class="primary" disabled={!canConfirm(shot)} onClick={() => act(() => api.confirm(shot.id), true)} title="Enter">
          {noBird ? "Publish without species" : "✓ Confirm"}
        </button>
        <button onClick={() => act(() => api.ignore(shot.id), true)}>Leave in dump</button>
      </div>
    );
  }
  if (shot.status === "confirmed" || shot.status === "ignored" || shot.status === "error") {
    return (
      <div class="actions">
        <button onClick={() => act(() => api.reopen(shot.id))}>{shot.status === "error" ? "Try again" : "Back to review"}</button>
      </div>
    );
  }
  return null;
}

function FileRow({ file, dumpsite, published }: { file: FileInfo; dumpsite: string; published: string }) {
  const source = file.dump_relpath ? `${dumpsite}/${file.dump_relpath}` : null;
  return (
    <div class={`file-row ${file.conflict ? "conflict" : ""}`}>
      <div>
        <span class="type">{file.file_type}</span> <b>{file.final_filename}</b>
        {file.final_filename !== file.original_filename && <small class="muted"> (was {file.original_filename})</small>}
        <small class="muted"> · {formatBytes(file.size_bytes)}</small>
        <a class="link" href={downloadUrl(file.id)} title="Download the original file"> ⤓</a>
      </div>
      {source && (
        <small class="path" title={source}>
          <span class="path-label">Picked up from</span> {source}
        </small>
      )}
      {file.publish_to && (
        <small class="path" title={`${published}/${file.publish_to}`}>
          <span class="path-label">{file.status === "published" ? "Published at" : "Will publish to"}</span> {published}/{file.publish_to}
        </small>
      )}
      {file.conflict === "library" && <small class="danger">A file with this name already exists in the library.</small>}
      {file.conflict === "dump" && <small class="warn">Another photo waiting in the dump has the same name.</small>}
    </div>
  );
}

function SuffixForm({ shot, onRenamed }: { shot: ShotDetail; onRenamed: (shot: ShotDetail) => void }) {
  const [suffix, setSuffix] = useState("_");
  const example = shot.files[0]?.original_filename.replace(/(\.[^.]+)$/, `${suffix}$1`);
  return (
    <form
      class="suffix-form"
      onSubmit={async (e) => {
        e.preventDefault();
        const updated = await attempt(() => api.suffix(shot.id, suffix), "Renamed");
        if (updated) onRenamed(updated);
      }}
    >
      <label>
        Append to the filename
        <input value={suffix} maxLength={40} onInput={(e) => setSuffix(e.currentTarget.value)} onKeyDown={(e) => e.stopPropagation()} />
      </label>
      <small class="muted">Becomes {example}{shot.files.length > 1 ? " (applied to every file of this shot)" : ""}</small>
      <button class="primary">Rename</button>
    </form>
  );
}

function Exif({ file, shot }: { file: FileInfo | undefined; shot: ShotDetail }) {
  if (!file) return null;
  const items = [
    file.camera,
    file.lens,
    file.focal_length ? `${Math.round(file.focal_length)} mm` : null,
    file.f_number ? `f/${file.f_number}` : null,
    formatExposure(file.exposure_time),
    file.iso ? `ISO ${file.iso}` : null,
    file.width && file.height ? `${file.width}×${file.height}` : null,
  ].filter(Boolean);
  return (
    <section>
      <h4>Camera</h4>
      <p class="exif">{items.join(" · ") || <span class="muted">No camera data</span>}</p>
      <p class="exif muted">
        Taken {shot.captured_at ? new Date(shot.captured_at).toLocaleString() : "at an unknown time"}
        {shot.captured_at_source === "file_mtime" ? " (from the file date — no EXIF date)" : ""}
        {shot.captured_at_offset_days && shot.captured_at
          ? ` (date corrected: the camera's clock said ${new Date(new Date(shot.captured_at).getTime() - shot.captured_at_offset_days * 86400000).toLocaleDateString()})`
          : ""}
        {shot.location.latitude !== null ? ` · GPS ${shot.location.latitude?.toFixed(5)}, ${shot.location.longitude?.toFixed(5)}` : ""}
      </p>
    </section>
  );
}
