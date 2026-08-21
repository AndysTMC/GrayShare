---
type: decision
---

# 0005. Keep UPnP off unless enable and passcode-required flags are both set

Status: accepted
Date: 2026-08-21
Deciders: existing project
Supersedes: —
Superseded-by: —

## Context

`miniupnpc` can map the listen port through a WAN gateway. An unauthenticated file server must not become reachable from the public internet by default.

## Options

- A. Forward the port whenever UPnP is available.
- B. Never implement WAN mapping.
- C. Opt-in via `GRAYSHARE_ENABLE_UPNP=1`, and refuse the mapping unless `GRAYSHARE_REQUIRE_PASSCODE=1` is also set.

## Decision

We will keep UPnP off by default. `PortForwarder.open` returns false unless both environment flags are set, even if the library is present.

## Assumptions

- [A1] A required passcode policy is enough extra friction for an intentional WAN experiment (revisit if passcodes become unused or guessable PINs).
- [A2] LAN mDNS advertisement does not imply WAN reachability.
- [A3] Operators who need ingress will set both flags on purpose.

## Consequences

Default installs stay LAN-local. Never restore unconditional forwarding.

## Revisit if

A product requirement is “share past NAT without flags,” or passcodes are no longer enforced when UPnP is on.
