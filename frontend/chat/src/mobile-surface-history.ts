export type MobileSurface =
  | { kind: "chat" }
  | { kind: "home" }
  | { kind: "conversations" }
  | { kind: "tools" }
  | { kind: "plugins" }
  | { kind: "runtime" }
  | { kind: "runtime-detail"; detailKind: "document" | "mcp" | "schedule"; key: string }
  | { kind: "dashboard"; pluginId: string };

interface MobileSurfaceHistoryState {
  roxyMobileSurface?: true;
  akashicMobileSurface?: true;
  surface: MobileSurface;
  depth?: number;
  dialog?: true;
}

interface HistoryWriter {
  readonly state?: unknown;
  pushState(data: unknown, unused: string): void;
  replaceState(data: unknown, unused: string): void;
}

export function mobileSurfaceHistoryState(surface: MobileSurface): MobileSurfaceHistoryState {
  return { roxyMobileSurface: true, surface };
}

export function readMobileSurfaceHistoryState(value: unknown): MobileSurface {
  if (!value || typeof value !== "object") return { kind: "chat" };
  const state = value as Partial<MobileSurfaceHistoryState>;
  if (
    state.roxyMobileSurface !== true
    && state.akashicMobileSurface !== true
  ) return { kind: "chat" };
  if (!state.surface) return { kind: "chat" };
  if (
    state.surface.kind === "home" ||
    state.surface.kind === "conversations" ||
    state.surface.kind === "tools" ||
    state.surface.kind === "chat" ||
    state.surface.kind === "plugins" ||
    state.surface.kind === "runtime"
  ) return state.surface;
  if (
    state.surface.kind === "runtime-detail" &&
    ["document", "mcp", "schedule"].includes(state.surface.detailKind) &&
    state.surface.key.trim()
  ) return state.surface;
  if (state.surface.kind === "dashboard" && state.surface.pluginId.trim()) return state.surface;
  return { kind: "chat" };
}

export function mobileSurfaceHistoryDepth(value: unknown): number {
  if (!value || typeof value !== "object") return 0;
  const state = value as Partial<MobileSurfaceHistoryState>;
  return state.roxyMobileSurface === true && typeof state.depth === "number" && Number.isSafeInteger(state.depth) && state.depth >= 0 ? state.depth : 0;
}

export function replaceMobileSurface(history: HistoryWriter, surface: MobileSurface): void {
  history.replaceState({ ...mobileSurfaceHistoryState(surface), depth: mobileSurfaceHistoryDepth(history.state) }, "");
}

export function pushMobileSurface(history: HistoryWriter, surface: MobileSurface): void {
  const state = { ...mobileSurfaceHistoryState(surface), depth: mobileSurfaceHistoryDepth(history.state) + 1 };
  if (isMobileDialogHistoryState(history.state)) {
    history.replaceState({ ...state, depth: mobileSurfaceHistoryDepth(history.state) }, "");
  } else history.pushState(state, "");
}

export function isMobileDialogHistoryState(value: unknown): boolean {
  return !!value && typeof value === "object" && (value as MobileSurfaceHistoryState).roxyMobileSurface === true && (value as MobileSurfaceHistoryState).dialog === true;
}

export function pushMobileDialog(history: HistoryWriter, surface: MobileSurface): void {
  history.pushState({ ...mobileSurfaceHistoryState(surface), depth: mobileSurfaceHistoryDepth(history.state) + 1, dialog: true }, "");
}
