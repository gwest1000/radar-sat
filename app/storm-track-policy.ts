type TimedPoint = { time: string; x: number; y: number };
type Viewport = { left: number; top: number; width: number; height: number };

// Interpolation is only for the current-centre visibility test, not new track
// samples or animation frames. Do not bridge missing source positions.
export function stormPresentInViewport(points: TimedPoint[], asOf: number, viewport: Viewport): boolean {
  if (!Number.isFinite(asOf)) return false;
  let position: { x: number; y: number } | undefined;
  for (let index = 0; index < points.length; index++) {
    const point = points[index];
    const time = Date.parse(point.time);
    if (time === asOf) { position = point; break; }
    const previous = points[index - 1];
    if (!previous) continue;
    const prior = Date.parse(previous.time);
    if (prior < asOf && asOf < time && time - prior <= 3 * 3600_000
      && Math.abs(point.x - previous.x) <= 0.5) {
      const fraction = (asOf - prior) / (time - prior);
      position = { x: previous.x + fraction * (point.x - previous.x),
        y: previous.y + fraction * (point.y - previous.y) };
      break;
    }
  }
  return Boolean(position && position.x >= viewport.left && position.x <= viewport.left + viewport.width
    && position.y >= viewport.top && position.y <= viewport.top + viewport.height);
}
