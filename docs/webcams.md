# Webcam overlay

Enable **Webcams** under Add/Remove Layers. Hover a camera to fetch one latest
provider-hosted image; click the icon/image or webpage link to open its website.
Nearby cameras share an icon with a count and a camera selector. Additional
views at the same mountain/airport are selectable in the preview. On touch
devices the first tap opens the preview. Keyboard focus opens it too.

These are **current observations, not historical loop frames**. Check the
timestamp burned into the provider image. Some cameras are offline or have no
direct image URL; their webpage links remain available. No image proxy, image
archive, or webcam R2 storage is added.

Camera metadata comes from the public ARFI map's `ARFI_Master_Table` feature
service and the official DriveBC `/api/webcams/` feed. DriveBC records replace
matching ARFI records by numeric camera ID, correcting stale coordinates,
retired webpage URLs, and incorrect thumbnail IDs. All ARFI webcam records with
a webpage are retained, including cameras outside BC when visible in a domain.

ARFI: https://arfi.avalanche.ca/

DriveBC open data: https://catalogue.data.gov.bc.ca/dataset/bc-highwaycams

Contains information licensed under the Open Government Licence – British
Columbia. Camera images belong to their respective operators. Operator credits
are displayed in the preview. ARFI locations are credited to Avalanche Canada.

Refresh metadata before a site deployment:

```sh
.venv/bin/python -m scripts.update_webcams
npm run build:pages
```

The refresher downloads only metadata, atomically replaces `public/webcams.json`,
and preserves the previous file on network/feed failure. Adding/removing the
webcam layer does not add a meteorological recipe, change prebuilt eligibility,
reset the frame, or alter playback settings. The memoized overlay has no loop
timer: projection is done offline and icon grouping only runs on stage resize or
domain change. Preview requests are delayed until a hover settles; at most one
image is mounted, and it is removed when the preview closes or the page is hidden.
