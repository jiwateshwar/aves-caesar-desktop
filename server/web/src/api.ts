export type Category =
  | "review" | "low_confidence" | "no_bird" | "awaiting_location" | "pending"
  | "conflicts" | "confirmed" | "published" | "ignored" | "error";

export type Counts = Record<Category | "total", number>;

export interface Species {
  id: string;
  name: string;
  scientific_name: string;
  family: string | null;
}

export interface Place {
  city: string;
  state: string | null;
  country: string;
  country_code: string;
  latitude: number;
  longitude: number;
  population: number;
  distance_km: number | null;
  label: string;
}

export interface SessionSummary {
  id: number;
  date: string | null;
  title: string | null;
  location: { latitude: number; longitude: number; label: string; country: string | null; state: string | null; city: string | null } | null;
  analyse_without_location: boolean;
  counts: Counts;
  folders?: { folder: string; counts: Counts }[];
}

export interface FileInfo {
  id: number;
  original_filename: string;
  final_filename: string;
  file_type: string;
  is_raw: boolean;
  size_bytes: number;
  status: string;
  dump_relpath: string | null;
  published_relpath: string | null;
  conflict: "library" | "dump" | null;
  publish_to?: string | null;
  camera?: string | null;
  lens?: string | null;
  focal_length?: number | null;
  f_number?: number | null;
  exposure_time?: number | null;
  iso?: number | null;
  width?: number | null;
  height?: number | null;
}

export interface Detection {
  id: number;
  box: [number, number, number, number] | null;
  box_confidence: number | null;
  species: Species | null;
  confidence: number | null;
  source: "model" | "manual";
  status: "pending" | "confirmed" | "rejected";
  alternatives: (Species & { confidence: number })[];
}

export interface ShotSummary {
  id: number;
  session_id: number;
  folder: string;
  captured_at: string | null;
  captured_at_source: string;
  status: string;
  category: Category;
  bird_count: number;
  min_confidence: number | null;
  species: Species[];
  location: { latitude: number | null; longitude: number | null; source: string | null; country: string | null; state: string | null; city: string | null };
  preview_ready: boolean;
  has_conflict: boolean;
  files: FileInfo[];
}

export interface ShotDetail extends ShotSummary {
  detections: Detection[];
  image_width: number | null;
  image_height: number | null;
  analysed_at: string | null;
  analysed_with_location: boolean | null;
  analysis_provider: string | null;
  analysis_error: string | null;
}

export interface Job {
  id: number;
  kind: string;
  status: "queued" | "running" | "done" | "failed";
  progress: number;
  total: number;
  message: string | null;
  result: Record<string, any>;
  payload: Record<string, any>;
  created_at: string;
  finished_at: string | null;
}

export interface Status {
  worker: {
    online: boolean;
    activity: string | null;
    heartbeat_at: string | null;
    providers: { via?: "local" | "remote"; node?: string; remote_error?: string | null; detector?: string[]; classifier?: string[] };
    model_error: string | null;
    last_scan: Record<string, any>;
    last_scan_at: string | null;
  };
  totals: Counts;
  duplicates: number;
  missing: number;
  jobs: Job[];
  paths: { dumpsite: string; published: string };
}

export interface Settings {
  confidence_threshold: number;
  publish_pattern: string;
  unknown_placeholder: string;
  auto_scan: boolean;
  poll_interval_seconds: number;
  settle_seconds: number;
  species_language: string;
  city_min_population: number;
}

export interface Duplicate {
  id: number;
  dump_relpath: string;
  size_bytes: number;
  original: { id: number; status: string; path: string } | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}

const query = (params: Record<string, string | number | undefined | null>) => {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "") as [string, string][];
  return entries.length ? "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString() : "";
};

export const api = {
  status: () => request<Status>("GET", "/api/status"),
  sessions: () => request<SessionSummary[]>("GET", "/api/sessions"),
  session: (id: number) => request<SessionSummary>("GET", `/api/sessions/${id}`),
  updateSession: (id: number, body: Record<string, unknown>) => request<SessionSummary & { queued: number }>("PATCH", `/api/sessions/${id}`, body),
  confirmConfident: (id: number, folder?: string) => request<{ confirmed: number }>("POST", `/api/sessions/${id}/confirm-confident${query({ folder })}`),
  shots: (params: { session_id?: number; folder?: string; category?: Category; offset?: number; limit?: number }) =>
    request<{ total: number; items: ShotSummary[] }>("GET", `/api/shots${query(params)}`),
  shot: (id: number) => request<ShotDetail>("GET", `/api/shots/${id}`),
  confirm: (id: number) => request<ShotDetail>("POST", `/api/shots/${id}/confirm`),
  ignore: (id: number) => request<ShotDetail>("POST", `/api/shots/${id}/ignore`),
  reopen: (id: number) => request<ShotDetail>("POST", `/api/shots/${id}/reopen`),
  setSpecies: (id: number, detectionId: number, speciesId: string) =>
    request<ShotDetail>("POST", `/api/shots/${id}/detections/${detectionId}/species`, { species_id: speciesId }),
  reject: (id: number, detectionId: number) => request<ShotDetail>("POST", `/api/shots/${id}/detections/${detectionId}/reject`),
  restore: (id: number, detectionId: number) => request<ShotDetail>("POST", `/api/shots/${id}/detections/${detectionId}/restore`),
  addSpecies: (id: number, speciesId: string) => request<ShotDetail>("POST", `/api/shots/${id}/species`, { species_id: speciesId }),
  suffix: (id: number, suffix: string) => request<ShotDetail>("POST", `/api/shots/${id}/suffix`, { suffix }),
  confirmMany: (shotIds: number[]) => request<{ confirmed: number; failed: { shot_id: number; reason: string }[] }>("POST", "/api/shots/confirm", { shot_ids: shotIds }),
  setPlace: (shotIds: number[], place: { country?: string; state?: string; city?: string }) =>
    request<{ updated: number }>("POST", "/api/shots/place", { shot_ids: shotIds, ...place }),
  reanalyse: (shotIds: number[]) => request<Job>("POST", "/api/shots/reanalyse", { shot_ids: shotIds }),
  publish: (body: { session_id?: number; shot_ids?: number[] }) => request<Job>("POST", "/api/publish", body),
  scan: () => request<Job>("POST", "/api/scan"),
  reindexLibrary: () => request<Job>("POST", "/api/library/reindex"),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
  job: (id: number) => request<Job>("GET", `/api/jobs/${id}`),
  duplicates: () => request<Duplicate[]>("GET", "/api/duplicates"),
  deleteDuplicate: (id: number) => request<{ deleted: string }>("DELETE", `/api/duplicates/${id}`),
  searchSpecies: (q: string) => request<Species[]>("GET", `/api/species${query({ q })}`),
  searchPlaces: (q: string) => request<Place[]>("GET", `/api/locations/search${query({ q })}`),
  reversePlace: (lat: number, lon: number) => request<Place>("GET", `/api/locations/reverse${query({ lat, lon })}`),
  settings: () => request<{ values: Settings; defaults: Settings; languages: string[] }>("GET", "/api/settings"),
  updateSettings: (changes: Partial<Settings>) => request<{ values: Settings; defaults: Settings; languages: string[] }>("PUT", "/api/settings", changes),
};

export const thumbUrl = (shotId: number) => `/api/shots/${shotId}/thumb.jpg`;
export const previewUrl = (shotId: number) => `/api/shots/${shotId}/preview.jpg`;
export const downloadUrl = (fileId: number) => `/api/files/${fileId}/download`;
