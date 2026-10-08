import assert from "node:assert/strict";
import test from "node:test";
import { groupWebcams, webcamPreviewUrl } from "../app/webcam-data.ts";

test("cropped regional views preserve every visible webcam in screen-space groups", () => {
  const catalog = {
    cameras: ["a", "b", "c"].map((id) => ({ id, name: id, provider: "DriveBC", links: [{ url: "https://example.org" }] })),
    domains: { bc: [{ id: "a", x: 0.51, y: 0.51 }, { id: "b", x: 0.511, y: 0.511 }, { id: "c", x: 0.1, y: 0.1 }] },
  };
  const crop = { left: 0.5, top: 0.5, width: 0.25, height: 0.25 };
  const groups = groupWebcams(catalog, "bc", crop, 800, 600);
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].cameras.map((c) => c.id), ["a", "b"]);
  assert.ok(Math.abs(groups[0].x - 0.042) < 0.00001);
  const broad = groupWebcams(catalog, "bc", { left: 0, top: 0, width: 1, height: 1 }, 1280, 960);
  assert.equal(broad.flatMap((group) => group.cameras).length, 3);
});

test("preview freshness is independent of animation time; signed provider URLs remain unchanged", () => {
  const drive = { provider: "DriveBC", previews: [{ url: "https://www.drivebc.ca/images/64.jpg" }] };
  assert.equal(webcamPreviewUrl(drive, 0, 120_000), "https://www.drivebc.ca/images/64.jpg?t=2");
  const mountain = { provider: "ARFI", previews: [{ url: "https://example.org/latest.php?signature=abc" }] };
  assert.equal(webcamPreviewUrl(mountain, 0, 120_000), mountain.previews[0].url);
  assert.equal(webcamPreviewUrl(mountain, 1, 120_000), undefined);
});
