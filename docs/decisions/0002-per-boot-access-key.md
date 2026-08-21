---
type: decision
---

# 0002. Gate share listing and SSE with a per-boot access key

Status: accepted
Date: 2026-08-21
Deciders: existing project
Supersedes: —
Superseded-by: —

## Context

The server binds `0.0.0.0` so phones on the LAN can connect. Share metadata (filenames, display names, sizes) must not be listable by every host on a shared or office LAN.

## Options

- A. Open `/api/shares` to the LAN; rely only on per-share passcodes for downloads.
- B. Issue a per-boot capability token in the QR / network URL (`?k=`); require it for listing and SSE.
- C. Require a user account or pairing ceremony.

## Decision

We will generate `ACCESS_KEY` at backend boot and require it (constant-time compare) on `/api/shares` and `/api/events`. The UI captures it into `grayshare.accessKey` and strips it from the address bar. `/api/network/info` redacts the key unless the caller is loopback or already presents a valid key.

## Assumptions

- [A1] Possession of the QR/LAN URL is an acceptable capability for this product (revisit if the URL is expected to be posted in public channels).
- [A2] Rotating the key on every backend start is acceptable; receivers rescan after a host restart.
- [A3] Passcodes remain a separate, optional download gate, not a substitute for hiding the share list.

## Consequences

Anyone with the link can list shares; anyone without sees 403. Stale keys in `localStorage` are dropped on 403 so the next scan can install a fresh key. Do not remove this gate.

## Revisit if

We need shares visible without a QR, or we need the key to survive restarts.
