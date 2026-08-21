---
type: decision
---

# 0001. Run the FastAPI backend as a child --server-only process

Status: accepted
Date: 2026-03-23
Deciders: existing project
Supersedes: —
Superseded-by: —

## Context

The desktop window and the HTTP API share a repo but not a process. Importing `main.py` in the UI process to set port, scheme, or shutdown callbacks loads a second copy of share state, logs, and the access key. Frozen builds already re-enter `desktop_app.py` via `sys.executable`.

## Options

- A. Import `main.py` in the desktop process and run uvicorn in a thread.
- B. Spawn `sys.executable --server-only` as a child and wait for `/api/health`.
- C. Ship two binaries (GUI and server) that do not share an entrypoint.

## Decision

We will launch the API as a child with `--server-only` and never import `main.py` from the UI process just to mutate runtime state.

## Assumptions

- [A1] The frozen executable (and `python desktop_app.py` in some environments) re-parses `--server-only` on `sys.executable` (revisit if source-mode spawn cannot pass the script path).
- [A2] `/api/health` is a sufficient ready signal before the splash hands off.
- [A3] Parent and child share `APP_DATA_DIR` / env for port, scheme, and logs.

## Consequences

Splash, pywebview, mDNS, and UPnP stay in the parent. `configure_runtime_control` in the child is not wired from the parent today, so `/api/app/save-and-close` returns 501. Graceful shutdown terminates the child.

## Revisit if

Source-mode `Popen([sys.executable, "--server-only", ...])` cannot start the child, or we need in-process shutdown of the API from the UI without a child.
