import { api } from "../api";
import { attempt, formatBytes, useLoader } from "../util";

export function DuplicatesPage({ onChange }: { onChange: () => void }) {
  const duplicates = useLoader(() => api.duplicates(), []);

  if (duplicates.error) return <div class="empty error">{duplicates.error}</div>;
  if (!duplicates.data) return <div class="empty">Loading…</div>;

  return (
    <div class="page narrow">
      <h1>Duplicates</h1>
      <p class="hint">
        These files are byte-for-byte identical to a photo that is already in the workflow or the published library, so they were not added
        again. Deleting removes only the extra copy from the dumpsite.
      </p>
      {duplicates.data.length === 0 ? (
        <div class="empty">No duplicates.</div>
      ) : (
        <table class="list">
          <thead>
            <tr><th>Extra copy in the dumpsite</th><th>Original</th><th /></tr>
          </thead>
          <tbody>
            {duplicates.data.map((d) => (
              <tr key={d.id}>
                <td><code>{d.dump_relpath}</code> <small class="muted">{formatBytes(d.size_bytes)}</small></td>
                <td>{d.original ? <><code>{d.original.path}</code> <small class="muted">({d.original.status.replace("_", " ")})</small></> : "–"}</td>
                <td>
                  <button
                    class="danger"
                    onClick={async () => {
                      if (!window.confirm(`Delete ${d.dump_relpath} from the dumpsite?`)) return;
                      await attempt(() => api.deleteDuplicate(d.id), "Deleted the extra copy");
                      duplicates.reload();
                      onChange();
                    }}
                  >
                    Delete copy
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
