type TimedFrame = {
  validTime: string;
};

export function appendLiveEdgeFrame<T extends TimedFrame>(
  frames: readonly T[],
  candidateTimes: readonly string[],
): T[] {
  if (!frames.length) return [];
  const newest = candidateTimes.reduce((current, value) => {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? Math.max(current, parsed) : current;
  }, -Infinity);
  const last = frames[frames.length - 1];
  const lastTime = Date.parse(last.validTime);
  if (!Number.isFinite(newest) || !Number.isFinite(lastTime) || newest <= lastTime + 1_000) {
    return [...frames];
  }
  return [
    ...frames,
    {
      ...last,
      validTime: new Date(newest).toISOString(),
    },
  ];
}
