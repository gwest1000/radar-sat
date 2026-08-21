import type {
  CompositeLoopManifest,
  CompositeProfilePointer,
  VideoLoopManifest,
} from "./video-loop";

const FAILED_PROFILE_HISTORY_LIMIT = 16;

export function rememberFailedKey(current: readonly string[], key: string): string[] {
  if (!key) return [...current];
  return [...current.filter((candidate) => candidate !== key), key]
    .slice(-FAILED_PROFILE_HISTORY_LIMIT);
}

export function rememberCompositeMediaFailure(
  current: Readonly<Record<string, string>>,
  key: string,
  reason: string,
): Record<string, string> {
  const entries = Object.entries(current).filter(([candidate]) => candidate !== key);
  return Object.fromEntries(
    [...entries, [key, reason]].slice(-FAILED_PROFILE_HISTORY_LIMIT),
  );
}

export function requiresPendingMediaPreload(
  selection: { manifest: VideoLoopManifest },
): boolean {
  return selection.manifest.transport === "progressive-mp4";
}

export type CompositeProfileCandidate = {
  pointer: CompositeProfilePointer;
  failureKey: string;
  fresh: boolean;
};

export function preferredCompositeProfile(
  exact: CompositeProfileCandidate | null,
  hybrid: CompositeProfileCandidate | null,
  failedProfileKeys: readonly string[],
): CompositeProfileCandidate | null {
  for (const candidate of [exact, hybrid]) {
    if (
      candidate
      && candidate.fresh
      && !failedProfileKeys.includes(candidate.failureKey)
    ) return candidate;
  }
  return null;
}

type CompositeCircuit = Pick<
  CompositeLoopManifest,
  "generation" | "layerId" | "presetId" | "productId" | "rangeHours" | "track"
>;

export function canRetainLoadedComposite(
  loaded: Pick<CompositeLoopManifest, "generation" | "presetId">,
  target: Pick<CompositeProfilePointer, "presetId"> | null,
  acceptedGeneration: string,
): boolean {
  return target?.presetId === loaded.presetId
    || (Boolean(acceptedGeneration) && acceptedGeneration === loaded.generation);
}

export function shouldQueueCompositeHandoff(
  active: CompositeCircuit | null,
  incoming: CompositeCircuit,
  acceptedGeneration: string,
  activeMatchesSelection: boolean,
): boolean {
  return Boolean(
    active
    && activeMatchesSelection
    && acceptedGeneration
    && active.generation === acceptedGeneration
    && active.productId === incoming.productId
    && active.layerId === incoming.layerId
    && active.track === incoming.track
    && active.rangeHours === incoming.rangeHours
    && (
      active.generation !== incoming.generation
      || active.presetId !== incoming.presetId
    ),
  );
}

export type PendingMediaFailureTransition = {
  discardPendingComposite: boolean;
  discardPendingVideo: boolean;
  failedMediaKey: string;
  failedProfileKey: string;
};

export function pendingMediaFailureTransition(
  source: "sidecar" | "legacy",
  mediaKey: string,
  profileKey = "",
): PendingMediaFailureTransition {
  return {
    discardPendingComposite: source === "sidecar",
    // A legacy exact rendition is only one asset inside the video manifest.
    // Keep the pending manifest so its base video can take over at the boundary.
    discardPendingVideo: false,
    failedMediaKey: mediaKey,
    failedProfileKey: source === "sidecar" ? profileKey : "",
  };
}
