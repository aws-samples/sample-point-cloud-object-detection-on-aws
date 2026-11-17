# Streaming Ingest I/O Redesign — Option C (design record)

**Status: IMPLEMENTED AND MERGED** (MR 27). This document was written as the design
proposal and is kept as the design record — the rationale, the options considered, and
the reasoning behind the one chosen. It is no longer a plan to be executed. Text below
that reads as future work ("we will", "this plan") describes what was subsequently
built; the measured outcome is in the results section at the end.

## Where we are now (what already landed)

The earlier version of this doc proposed three options.

**Option A landed first** (commits `68f562c` route-via-cell→leaf-map, `6140ec8`
file-order fix), removing the "root cause" the old doc named — the per-chunk × per-leaf
`O(points × leaves)` inner scan. That left the *other* cost the old doc flagged and
deferred: the file was still re-read once per batch.

**Option C then landed** (MR 27), removing those re-reads: single-pass routing to
on-disk per-tile extracts, store-once with neighbour reconstruction. `_batch_leaves`
and `MAX_LEAVES_PER_BATCH` are gone; peak is now bounded by the flush buffer plus a
byte-capped read cache plus one tile rather than by a batch. That closed the 50 GB
throughput wall — see "Measured outcome" at the end of this document.

## The measured problem (updated with the 50 GB run)

All runs: synthetic scene, A10G box, stub backend (no real inference), `/usr/bin/time -v`.

| Scene | Planner / routing | Tiles | Wall time | Peak RSS |
|-------|-------------------|-------|-----------|----------|
| 300 M | midpoint, per-leaf scan | 49 | 5 h 05 m | 1.67 GB |
| 300 M | occupancy-median, per-leaf scan | 71 | 8 h 16 m | 1.61 GB |
| 300 M | occupancy-median, **Option A routing** | 71 | **39 m 41 s** | 1.86 GB |
| 50 GB | occupancy-median, Option A routing | ~10,000 | **did NOT finish in ~24 h** | ~2.9 GB (top) |

Two things this table proves:

1. **Option A worked at 300 M** — ~12x, same 71-tile plan, same detections. The per-point
   routing cost is no longer the bottleneck at that scale.
2. **It is NOT enough at 50 GB.** A 50 GB scene plans into ~10,000 tiles. Memory stayed
   bounded (~2.9 GB, zero swap — NFR-1.1 holds at 50 GB), but the run was CPU-bound and did
   not complete in a day. The remaining cost is now the **re-read multiplier**: total Pass-2
   I/O is `(1 + n_batches) × file_size`, and batches grow with tile count, so a 10,000-tile
   scene walks the 50 GB file many times over.

So the last lever is the re-reads. Option C removes them.

## Root cause that remains (in the code at HEAD)

`stream_tiles` (`streaming.py`) Pass 2, current shape:

```
for batch in batches:                       # (1 + n_batches) full file reads  <-- the wall
    cell_map = build_cell_leaf_map(batch)   # cell -> candidate leaves (Option A, landed)
    for xyz,rgb,i in iter_chunks(file):      # a full walk of the file, per batch
        route xyz -> candidate leaves via cell_map, exact box test, np.sort (file order)
        acc[leaf].append(selected points)
    for leaf in batch: preprocess + yield tile
```

`_batch_leaves` bounds a batch by the memory budget (`MAX_LEAVES_PER_BATCH = 256`, or the
point budget). At ~10,000 tiles that is ~40+ batches, i.e. the 50 GB file is read ~40+
times in Pass 2 alone, on top of the one Pass-1 occupancy walk. Each `iter_chunks` walk is a
fresh `laspy.open(...).chunk_iterator(...)` (`iter_chunks_las`), decompressing LAZ every time.

## Option C — single-pass routing to on-disk per-tile extracts

Walk the source file **once** in Pass 2, and as each chunk streams past, append every
point to the on-disk extract file(s) of the tile(s) it belongs to. Then inference reads each
compact per-tile extract in turn. Total source reads drop from `(1 + n_batches)` to exactly
**2** (Pass-1 occupancy walk + one Pass-2 routing walk), independent of tile count. This is
how production tilers (PDAL, Entwine) scale.

```
Pass 1 (unchanged): scan_occupancy -> ScenePass1 (bounds, count, occupancy grid)
Plan (unchanged):   plan_tile_extents -> leaf boxes; build_cell_leaf_map -> cell->leaves

Pass 2a — route + spill (ONE walk of the source):
    open a writer per leaf (lazily, on first point) into a temp dir
    for xyz,rgb,intensity in iter_chunks(file):          # single walk
        cidx  = floor((xyz - grid_origin)/cell_size); clip     # same as Pass-1 histogram
        flat  = flatten(cidx)
        group rows by cell (argsort/unique — as today)
        for each cell -> candidate leaves (cell_map):
            for each candidate leaf i:
                exact float box test against [lo-margin, hi+margin]
                sel = np.sort(rows[mask])                 # PRESERVE FILE ORDER (file-order fix)
                writer[i].append(xyz[sel], rgb[sel], intensity[sel])
    close all writers

Pass 2b — read + detect (per tile, bounded memory):
    for leaf i in plan order:
        tile_xyz, tile_rgb, tile_i = writer[i].read_all()   # compact, tile-sized
        reconcile core_points_emitted (unchanged)
        region = preprocess(PointCloud(...), config.preprocessing)
        yield Tile(...)                                      # -> detection in _run_streaming
        delete extract i                                     # reclaim disk as we go
```

The routing logic in 2a is **the same code path Option A already uses** (cell→leaf map,
exact box test, `np.sort` for file order). The only change is the *destination*: instead of
an in-RAM `acc[i]` list scoped to a batch, points go to a per-leaf on-disk file, so all
leaves can be filled in a single walk without holding them all in RAM.

### Temp-file (extract) format

Requirements: append cheap, read-back cheap, preserve dtype and channel presence, no laspy
re-encode cost per point. **Raw per-channel `.npy`-style binary is the right call, not
LAS/LAZ:**

- Per leaf, a small directory or a set of streams: `xyz` (float64, N×3), and optionally
  `rgb` (uint8, N×3) and `intensity` (float32/native, N) — only when the source has them.
- Append raw bytes as chunks arrive (`file.write(arr.tobytes())`); no header rewrite per
  append, no compression round-trip.
- Read back with `np.fromfile(...).reshape(-1, 3)` etc. Order on disk = append order =
  file order (guaranteed by writing `np.sort(rows[mask])` selections in chunk-walk order).
- Store a tiny sidecar (JSON or a fixed header) per leaf recording point count and which
  channels are present, so 2b knows how to reshape without guessing.

Rationale for NOT reusing LAS/LAZ for extracts: LAZ compression on write would re-incur the
exact per-point encode cost we are trying to remove, and laspy append is not zero-cost. Raw
binary append is O(bytes) and the extracts are transient.

### Disk headroom

The extracts together hold every routed point once, plus guard-band duplicates (points in
overlap zones are written to each overlapping tile). So extract volume ≈ source *point*
volume × (1 + overlap-duplication factor), uncompressed. For a 50 GB LAZ (which is
compressed ~6.5x), the uncompressed extract footprint is **larger than the source file** —
on the order of the raw point bytes (7.6 B points × ~28 B ≈ ~210 GB for xyz+rgb+intensity,
less if channels absent). This is the main cost of Option C and MUST be checked:

- Compute the estimated extract footprint up front (from `points_read` × per-point stride ×
  overlap factor) and compare against free disk on the temp volume; **fail fast with a clear
  message** if it will not fit, rather than filling the disk mid-walk.
- Delete each extract immediately after its tile is read in 2b (shown above), so peak disk
  is not the sum of all extracts but the max resident set of not-yet-consumed extracts.
  Since 2b consumes in plan order and 2a wrote all of them, worst-case peak is the full set;
  a refinement is to interleave (consume a tile as soon as its writer is known complete),
  but the simple version writes-all-then-reads and needs the full footprint.
- Make the temp dir configurable (default alongside the output, or `$TMPDIR`), because the
  operator may need to point it at a larger volume than the source lives on.

Honest note: disk is the tradeoff we accept to kill the re-read multiplier. On the 300M box
this is trivial; at 50 GB it needs a headroom check; at 500 GB it needs a deliberately sized
scratch volume. That is inherent to the on-disk-extract approach and should be documented for
operators, not hidden.

### What Option C does NOT change (invariants preserved)

Every invariant the current routed path guarantees must hold byte-for-byte. Mapping each to
how Option C preserves it:

- **File order within a tile** — 2a writes `np.sort(rows[mask])` selections in chunk-walk
  order (i.e. file order), and raw-binary append preserves that order on read-back. This is
  the file-order invariant (`voxel_downsample` keeps the first point per
  voxel by input order); Option C keeps it because it reuses the same `np.sort` and appends
  in walk order.
- **Byte-identical output** vs today — same routing predicate, same order in → same
  `preprocess` input → same detections. This is the primary correctness gate (see below).
- **Guard-band overlap** — a boundary point is written to *each* overlapping leaf's extract,
  exactly as `build_cell_leaf_map` + the per-candidate box test route it today. Duplication
  across extracts is expected and correct.
- **100% core coverage** — the `core_points_emitted` reconcile still runs in 2b against the
  un-expanded `[lo,hi]` box; the warning-on-shortfall check is unchanged.
- **Bounded memory (NFR-1.1)** — 2a holds only the current chunk + open file handles (a
  write buffer per leaf, which can be capped/flushed); 2b holds one tile at a time. Peak RAM
  is not the whole cloud. (Contrast Option B, rejected precisely because it holds all leaves
  in RAM.)
- **coord_frame handling** — planning/selection stay in FILE frame; `_transform_box` maps
  extents out per tile in 2b; `coord_frame="auto"` still rejected. Unchanged.
- **E57 parallelism** — 2a routes through the format-agnostic `iter_chunks` dispatcher, so
  LAS/LAZ and E57 share the writer. The E57 buffer-copy contract (`_iter_e57_blocks` reuses
  buffers; callers must copy) must be respected when writing extract bytes — copy on
  selection, which `xyz[sel]` already does.

### A memory subtlety in 2a: too many open file handles

At ~10,000 tiles, opening 10,000 file handles at once may hit the OS `ulimit`. Mitigations,
in order of preference:
1. **Buffer per leaf in RAM, flush in bounded groups.** Keep a small append buffer per leaf;
   when total buffered bytes cross a cap, flush the fullest buffers to their files (open,
   append, close). Bounds both RAM and concurrent handles. This is the recommended shape.
2. Or process leaves in handle-sized groups with a routing walk per group — but that
   reintroduces re-reads, defeating the point. Avoid.

Option 1 keeps a single source walk while bounding open handles; it is the crux of the
implementation and where most of the care goes.

## Concrete code shape (design-level, not final)

New/changed in `streaming.py`. Signatures are indicative:

```python
class _TileExtractWriter:
    """Append-only on-disk buffer for one leaf's points, in file order.

    Raw per-channel binary in a temp dir. Bounded RAM append buffer, flushed to
    the leaf's file when the shared byte budget is hit. Records channel presence
    and count in a sidecar so the reader reshapes without guessing.
    """
    def append(self, xyz, rgb, intensity) -> None: ...
    def close(self) -> None: ...

class _TileExtractStore:
    """Owns the temp dir and all per-leaf writers; bounds total RAM + open handles."""
    def __init__(self, tmp_dir: Path, leaf_indices: list[int],
                 flush_bytes_budget: int): ...
    def route_append(self, i: int, xyz, rgb, intensity) -> None: ...
    def flush_all(self) -> None: ...
    def read_tile(self, i: int) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]: ...
    def drop(self, i: int) -> None: ...       # delete extract after its tile is consumed
    def estimated_footprint_bytes(self) -> int: ...
    def cleanup(self) -> None: ...            # remove temp dir on exit / on error

def stream_tiles(...):
    # Pass 1 + plan: UNCHANGED (scan_occupancy, plan_tile_extents, build_cell_leaf_map).
    # Disk headroom check: fail fast if estimated_footprint_bytes() > free(tmp_dir).
    # Pass 2a: ONE iter_chunks walk over the whole plan (all leaves, not batched),
    #          routing rows to store.route_append(i, ...) using the SAME cell_map +
    #          exact box test + np.sort as today.
    # Pass 2b: for i in plan order: read_tile(i) -> reconcile -> preprocess -> yield;
    #          then drop(i). cleanup() in a finally.
```

`_batch_leaves` is no longer needed for the routing walk (there is one walk, not
per-batch); it can stay for any fallback path but Option C's Pass 2 does not call it.
`build_cell_leaf_map` is now built once for the whole plan, not per batch — check its size
at 10,000 tiles (it is keyed by occupancy cell, so it scales with grid cells not tiles;
should be fine, but measure).

Failure handling: wrap Pass 2 in `try/finally` that calls `store.cleanup()`, so a crash or
`KeyboardInterrupt` (which is exactly what happened on the 50 GB run) does not leave ~200 GB
of orphaned extracts on disk. Also handle partial-write / disk-full mid-walk: catch the
write error, clean up, and raise a clear "ran out of scratch disk at tile N" message.

## Validation plan (the acceptance gate — measured, not argued)

Correctness first, then the benchmark. Both are required before this merges.

1. **Byte-identical equivalence test (correctness gate).** On a small synthetic scene (with
   AND without overlap margin, and with rgb+intensity present and absent), assert the tiles
   produced by Option C are **identical** to the current routed path — not just same
   membership, but same per-tile point *order* (the `voxel_downsample` order-sensitivity is
   why order matters). Concretely: run `stream_tiles` old vs new, compare each tile's `xyz`
   (and rgb/intensity) arrays with `np.array_equal` in yield order. This is stronger than the
   Option A test (which only checked membership) precisely because the file-order fix showed order
   matters.
2. **Coverage + overlap tests.** `core_points_emitted == points_read` (no lost points);
   boundary points appear in every overlapping tile (guard-band duplication intact).
3. **Handle/RAM bound test.** Force a low `flush_bytes_budget` and a many-tile plan; assert
   peak open handles and peak RSS stay bounded (no "all leaves resident").
4. **300 M re-benchmark.** `~/benchdata/synth_300M.laz`, stub backend, `/usr/bin/time -v`.
   Compare against the Option A row: **71 tiles, 71 detections, wall time ≤ Option A's
   39 m 41 s (should be similar or better — Option A already removed the per-point cost;
   Option C removes re-reads, which at 71 tiles / few batches is a smaller share), peak RSS
   unchanged (~1.9 GB).** Membership/detections identical to Option A. If Option C is not
   faster at 300 M that is expected — 300 M has few batches so re-reads are cheap there; the
   win shows at 50 GB.
5. **50 GB run (the real target).** The 50 GB scene that did NOT finish in ~24 h under Option
   A. Acceptance: it **completes**, in practical time, peak RSS bounded (~few GB), 100%
   coverage. This is the number that turns NFR-1.2 from "measured failing at 50 GB" into
   "measured passing at 50 GB". Also record peak scratch-disk used, to size operator guidance.
6. Only after 4 and 5 land: update the blog + `PHASE1_COMPLETION.md` throughput wording from
   "measured failing at 50 GB" to the measured Option C result.

## Guardrails (do not regress these)
- Peak memory stays bounded by one tile / the flush buffer (NFR-1.1 — proven at 50 GB, keep it).
- Point coverage stays 100% (`core_points_emitted` reconcile must still pass).
- Guard-band overlap semantics unchanged (boundary points belong to all overlapping tiles).
- Per-tile file order unchanged (byte-identical output; the `np.sort` file-order invariant).
- `coord_frame` handling unchanged (streaming still rejects `auto`; box transform intact).
- Scratch disk is checked up front and cleaned up on every exit path (no orphaned extracts).

## Rejected alternatives
- **Option B (all leaves resident in RAM, single walk):** rejected — reintroduces
  whole-cloud memory, the exact thing streaming exists to avoid. Only viable if leaves spill
  to disk, which is Option C.
- **LAS/LAZ extract files:** rejected — re-incurs per-point encode/compress cost on write,
  defeating the purpose. Raw binary append is O(bytes).
- **Keep batching, just raise the budget:** rejected — fewer batches means fewer re-reads
  but a larger per-batch RAM footprint, trading the throughput problem straight back for the
  memory problem. Does not scale to 500 GB.

## Not to be done solo / self-verified
This is core ingest architecture. Whoever implements it has the other review it, and the
benchmark numbers (steps 4-5) — not unit tests alone — are the acceptance gate. History
that earns this rule: an inert budget cap that did nothing, a placement fix mis-framed as a
throughput fix, and an Option A routing change whose correctness tests passed while silently
reordering points until review caught it. Correctness at scale is not self-evident here.

## Measured outcome (what actually happened)

Option C shipped in MR 27. The validation plan above was followed; this section records
the result so the design record is not left reading as an open proposal.

### Reported benchmark figures

One operator's bench box, single runs, not CI:

| Scene | Backend | Wall time | Peak RSS | Note |
|-------|---------|-----------|----------|------|
| 300 M synthetic | stub | 12 m 29 s | — | 3.2x over Option A's 39 m 41 s |
| 50 GB synthetic (NVMe) | stub | **6 h 42 m** | 4.25 GB | previously did not finish in ~24 h |

The 50 GB row is the one that matters: the throughput wall this design existed to remove
is gone. **These numbers are not reproduced in CI and their raw `/usr/bin/time -v` output
is not yet committed** — that is tracked as a publication blocker in
`docs/blogs/02-deep-dive.md`.

### Independently verified during review

Separate from the benchmark figures, and reproducible on any machine:

- **Byte-identical output** to the pre-Option-C path across five tiling configurations
  (wide overlap, zero margin, downsample active, downsample off, small-tiles-large-overlap),
  compared with order-sensitive hashes so a permutation would be caught.
- **Extract cleanup on all six exit paths** — normal drain, consumer exception, generator
  abandoned mid-iteration, reference dropped, explicit `close()`, KeyboardInterrupt.
  Zero orphaned extract directories in every case, and none left after the full test suite.
- **Guards fire before damage.** A missing `PCD_EXTRACT_DIR` and an insufficient-disk
  condition both raise with actionable messages, and the disk check raises after only the
  Pass-1 occupancy walk — before Pass 2a writes anything.
- **Concurrency safe.** Two simultaneous runs sharing one `PCD_EXTRACT_DIR` get distinct
  `mkdtemp` directories and both clean up.
- **Memory overhead does not grow with scene size.** Peak RSS versus the previous path was
  +26% at 2 M points and +5% at 8 M — the signature of a bounded fixed cost, which is what
  the flush-buffer + byte-capped-cache design intends. Contrast Option A, whose overhead
  grew with scene size.

### Known limits carried forward

- **The read cache cap is a hardcoded 512 MB**, not derived from `memory_budget_gb`. On a
  small configured budget it is a large fraction of the allowance.
- **Filesystem syscall overhead is the remaining throughput floor** — roughly
  `O(tiles × neighbours)` small-file operations from the per-core-file layout. A packed
  single file plus an offset index would address it. Not attempted; flagged for a design
  call rather than tuned blindly.
- **500 GB remains unmeasured.** 50 GB is the first benchmark tier, not the design target.
