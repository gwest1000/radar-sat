// BC's final spring-forward was 8 March 2026. Do not depend on the browser's
// installed tzdata having the permanent UTC-7 rule or the new PCT abbreviation.
const PERMANENT_PACIFIC_START = Date.parse("2026-03-08T10:00:00Z");

export function pacificClock(value: string, compact = false): string {
  const date = new Date(value);
  const permanent = date.getTime() >= PERMANENT_PACIFIC_START;
  const formatted = new Intl.DateTimeFormat("en-CA", {
    timeZone: permanent ? "Etc/GMT+7" : "America/Vancouver",
    ...(compact ? { hourCycle: "h23" as const } : { weekday: "short" as const }),
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(!permanent ? { timeZoneName: "short" as const } : {}),
  }).format(date);
  return permanent ? `${formatted} PCT` : formatted;
}

export function dailyTrackPoints<T extends { time: string }>(points: T[]): T[] {
  let next = -Infinity;
  const labels = points.filter((point) => {
    const time = Date.parse(point.time);
    if (!Number.isFinite(time) || time < next) return false;
    next = time + 24 * 3600_000;
    return true;
  });
  const last = points.at(-1);
  if (last && Number.isFinite(Date.parse(last.time)) && labels.at(-1)?.time !== last.time) labels.push(last);
  return labels;
}

export function pacificTrackClock(value: string): string {
  const date = new Date(value);
  const permanent = date.getTime() >= PERMANENT_PACIFIC_START;
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: permanent ? "Etc/GMT+7" : "America/Vancouver",
    weekday: "short", hour: "2-digit", hourCycle: "h23",
    ...(!permanent ? { timeZoneName: "short" as const } : {}),
  }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value ?? "";
  return `${part("weekday")} ${part("hour")} ${permanent ? "PCT" : part("timeZoneName")}`;
}
