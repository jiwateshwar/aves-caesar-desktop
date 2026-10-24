import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import type { Category } from "./api";

export interface Route {
  path: string[];
  params: URLSearchParams;
}

function parseHash(): Route {
  const [path, search = ""] = window.location.hash.replace(/^#\/?/, "").split("?");
  return { path: path.split("/").filter(Boolean), params: new URLSearchParams(search) };
}

export function useRoute(): Route {
  const [route, setRoute] = useState(parseHash);
  useEffect(() => {
    const update = () => setRoute(parseHash());
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return route;
}

export function href(path: string, params: Record<string, string | number | undefined | null> = {}): string {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  const search = entries.length ? "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString() : "";
  return `#/${path.replace(/^\//, "")}${search}`;
}

export function navigate(path: string, params: Record<string, string | number | undefined | null> = {}) {
  window.location.hash = href(path, params);
}

/** Loads data, reloads on demand, and optionally polls. */
export function useLoader<T>(load: () => Promise<T>, deps: unknown[], pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    setData(null);
    reload();
    if (!pollMs) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) reload();
    }, pollMs);
    return () => window.clearInterval(timer);
  }, deps);
  return { data, error, reload, setData };
}

export function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

/** Model confidence as a percentage. Never shows 100%: a suggestion is never certain. */
export function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "–";
  if (value >= 0.995) return `${Math.min(99.9, Math.floor(value * 1000) / 10).toFixed(1)}%`;
  return `${Math.round(value * 100)}%`;
}

export const plural = (count: number, one: string, many: string = `${one}s`) => `${count} ${count === 1 ? one : many}`;

export function formatDate(iso: string | null, withWeekday = true): string {
  if (!iso) return "Undated";
  const date = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  return date.toLocaleDateString(undefined, { weekday: withWeekday ? "long" : undefined, year: "numeric", month: "long", day: "numeric" });
}

export const formatTime = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");

export function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

export function formatExposure(seconds: number | null | undefined): string | null {
  if (!seconds) return null;
  return seconds >= 1 ? `${seconds}s` : `1/${Math.round(1 / seconds)}s`;
}

export const CATEGORY_LABELS: Record<Category, string> = {
  review: "To review",
  low_confidence: "Low confidence",
  no_bird: "No bird",
  awaiting_location: "Needs location",
  pending: "Queued",
  conflicts: "Name conflicts",
  confirmed: "Confirmed",
  published: "Published",
  ignored: "Left in dump",
  error: "Errors",
};

export const CATEGORY_ORDER: Category[] = ["review", "low_confidence", "no_bird", "awaiting_location", "pending", "conflicts", "confirmed", "published", "ignored", "error"];

/** Tabs that always show, even when empty. */
export const PRIMARY_CATEGORIES = new Set<Category>(["review", "low_confidence", "no_bird", "confirmed"]);

type ToastListener = (message: Toast) => void;
export interface Toast {
  id: number;
  text: string;
  kind: "info" | "error";
}
const listeners = new Set<ToastListener>();
let toastId = 0;

export function toast(text: string, kind: Toast["kind"] = "info") {
  const message = { id: ++toastId, text, kind };
  listeners.forEach((listener) => listener(message));
}

export function onToast(listener: ToastListener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Runs an action, reporting failures as an error toast. */
export async function attempt<T>(action: () => Promise<T>, success?: string | ((result: T) => string)): Promise<T | undefined> {
  try {
    const result = await action();
    if (success) toast(typeof success === "string" ? success : success(result));
    return result;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    return undefined;
  }
}
