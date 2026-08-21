---
type: decision
---

# 0003. Write chunked uploads in-place at offset with no merge lock

Status: accepted
Date: 2026-08-21
Deciders: existing project
Supersedes: —
Superseded-by: —

## Context

Parallel browser uploads of large files cannot wait on a single asyncio lock or a merge of part files. Disjoint byte ranges can be written concurrently if the destination length is known up front.

## Options

- A. Upload part files, then merge under a session lock.
- B. Preallocate the final inbox file (`truncate`) and write each chunk at `offset = index * chunk_size` with its own handle.
- C. Buffer the whole file in RAM, then write once.

## Decision

We will preallocate `inbox/{file_token}_{name}` on `POST /api/share/init`, write each chunk in place, and have `finalize` check chunk count and total size. On failure the target file is deleted. No part files, no merge phase, no session-wide lock.

## Assumptions

- [A1] The filesystem allows concurrent writes to disjoint regions of one file without tearing (revisit if a backend shows corruption; `tests/test_backend.py` covers the lock-free invariant).
- [A2] Clients send the correct `total_size` and `chunk_size` at init.
- [A3] SMB mode does not need this path yet (`share_init` returns 501; the UI uses single-request `POST /api/share`).

## Consequences

Workers never stall on a slow sibling chunk. Short chunks are rejected at upload time. Do not reintroduce part-file merging or a session-wide asyncio lock.

## Revisit if

SMB must support chunked upload, or a filesystem is found that corrupts overlapping parallel writes even at disjoint offsets.
