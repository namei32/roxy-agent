/** 区分原生 resize 与 WebView 可视区域平移，避免重复扣除键盘高度。 */
export function mobileViewportBounds(innerHeight: number, visual?: { height: number; offsetTop: number; scale: number } | null): { height: number; top: number } {
  const height = Math.max(1, Math.round(innerHeight));
  if (visual && Number.isFinite(visual.height) && Number.isFinite(visual.offsetTop)
    && Math.abs(visual.scale - 1) < .01 && visual.height > 0
    && visual.height < height && visual.offsetTop > 1) {
    const visibleHeight = Math.round(visual.height);
    return { height: visibleHeight, top: Math.min(height - visibleHeight, Math.round(visual.offsetTop)) };
  }
  return { height, top: 0 };
}
