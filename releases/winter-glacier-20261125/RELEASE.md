# Winter glacier update — 2026-11-25

Business source: Linger, 2026-11-25 19:55. Current winter 8-day product uses Renlongba glacier instead of Laigu. This is a product correction, not an independently verified public claim that Laigu is permanently closed.

## Scope

Candidate payload was built from the actual live package, preserving current prices, departure periods, other content groups and private-tour behavior. Keep the internal `laigu` content-group identifier for historical deduplication compatibility, but change its customer-facing text. Replace the winter skill's destination references. Disable material 118 (old itinerary map) and 122 (Laigu photo): the reviewed map explicitly names Laigu on the map and day 3; no approved Renlongba replacement photo was supplied. Use a text itinerary until new assets are approved. Retain original files and historical sent messages.

## Delivery and rollback

Run `publish.py preflight` and `publish.py apply` from the pinned repository checkout on the verified host. This data-only release uses existing route-file hot loading and per-call skill loading; no services, proxy, static roots, or unrelated data are changed. The script locks deployment, verifies live hashes and asset identities, stores a restricted snapshot, and validates all enabled route compilation plus protected-file and process inventories. Rollback restores only the two files and two availability flags, with drift checks; never restore the whole database.

Actual entry map: `ai.luoxuecong.asia` -> Cloudflare tunnel -> 127.0.0.1:8080 -> Nginx -> `/opt/china2go-ai/frontend/dist`; `api.luoxuecong.asia` -> tunnel -> 127.0.0.1:8000 -> china2go-api. Worker and playground share backend runtime data. No Docker containers were listed. Public page/deep link, referenced JS/CSS MIME types and hashes, API health are checked before and after.

## Verification boundaries

Use isolated real-model replay with production writes and outbound delivery disabled. Do not send customer test messages, replay historical queues, or change conversation ownership. This release does not update the marketing website, already delivered images, or provide replacement glacier photography. Public asset/health checks do not constitute a complete authenticated UI regression.
