---
type: decision
---

# 0004. Restrict host-disk writes to loopback

Status: accepted
Date: 2026-08-21
Deciders: existing project
Supersedes: —
Superseded-by: —

## Context

LAN browsers share the same HTTP API as the desktop window. Endpoints that write the host filesystem (save-as, clear-data, host settings, preferred port) would let any holder of the access key wipe the inbox or drop a file on an arbitrary path.

## Options

- A. Allow those endpoints to any client that has the access key.
- B. Require loopback (`127.0.0.1`, `::1`, `localhost`) for host-disk and host-settings mutations; LAN clients keep prefs in `localStorage`.
- C. Add a separate admin secret for mutations.

## Decision

We will reject non-loopback callers on `save-local`, `POST /api/data/clear`, host `settings.json` writes, and desktop port config. Desktop save still uses the pywebview dialog in the parent, then posts an absolute path from loopback.

## Assumptions

- [A1] The desktop UI always loads via loopback, not the LAN IP (revisit if the webview is pointed at the advertised LAN URL).
- [A2] Reverse proxies that make LAN look like loopback are out of scope for a local-first host.
- [A3] A native save path chosen in the host OS dialog is the only absolute path we will write.

## Consequences

LAN clients cannot clear host data or write `settings.json`. `save-local` is unavailable off-box; those clients use FS Access or the browser download. Do not expose arbitrary file-write behavior to LAN clients.

## Revisit if

A remote admin UI is required, or the desktop webview stops using loopback.
