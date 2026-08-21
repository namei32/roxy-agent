// Public dashboard SDK surface shared with plugin panels. Plugins import this
// via the "@roxy/dashboard-ui" specifier (the old "@akashic/dashboard-ui"
// remains an alias). Both shims use window.__roxyRuntime and the legacy global,
// component implementations and its single React instance.
export * from "./ui";
export { cn } from "./cn";
export { Pie, MetricTile, Sparkline, TrendChart } from "./charts";
export type { ChartTone } from "./charts";
export { api, asPageResult, pageCount } from "../api";
