# DJ USB audio deduplication

This Python 3.11+ tool lets Engine DJ reference byte-identical audio in a Rekordbox export's `Contents` directory.
It uses only the Python standard library. No installation is required.
The USB name is not hardcoded. Stored paths are relative to `Engine Library`.

**CAUTION: Test a backed-up disposable export before using this tool on your performance USB.**
Hardware playback, correct Stems associations, and subsequent exports from both DJ apps remain unverified.

## Setup

Requires macOS and Python 3.11 or newer. Optional `ffprobe` enables metadata fallback; without it, exact-hash matching still works.
No Python packages are required.

```sh
git clone git@github.com:justinjohnso/dj-usb-dedup.git
cd dj-usb-dedup
python3 -B dedup_dj_usb.py --dry-run
```

For a command available from any directory, add this function to your shell configuration, using your actual checkout path:

```sh
dj-usb-dedup() {
  python3 -B /absolute/path/to/dj-usb-dedup/dedup_dj_usb.py "$@"
}
```

## Run

**CAUTION: Selecting a volume starts database updates and verified duplicate deletion. Use `--dry-run` for preview only.**

```sh
python3 -B dedup_dj_usb.py
```

Select a mounted volume by number. Type `q` to cancel.
The tool detects Engine DJ and Rekordbox processes and refuses mutation while either app is open.
Lexicon can remain open unless it has files open on the target USB. Do not start a sync during the run.
Backups are created automatically in `DJ USB Backups` at the USB root. There are no typed confirmation or backup-directory prompts.
Use only a dedicated DJ export; references from unrelated applications cannot be inferred.
The tool matches tracks, backs up the database, updates paths, and removes verified exact duplicates.
Metadata matching is included automatically. Metadata-only source copies are always retained.

## Preview without changes

```sh
python3 -B dedup_dj_usb.py --dry-run
```

An explicit path also works:

```sh
python3 -B dedup_dj_usb.py /Volumes/JUSTIN_CDJ1 --dry-run --verbose
```

Dry run writes no logs, SQLite sidecars, locks, or other files to the USB.
It prints row counts, exact matches, metadata matches, conflicts, missing files, and unique removable bytes.
Full SHA-256 hashing and metadata inspection can take several minutes.

The old apply, duplicate-deletion, and metadata opt-in flags are removed. Use `--dry-run` to disable all mutation.

## Why Lexicon can remain open

The official Lexicon manual separates computer-library sync from direct USB sync.
Both [Engine USB sync](https://www.lexicondj.com/manual/sync-engine-dj) and [Pioneer USB sync](https://www.lexicondj.com/manual/sync-to-pioneer-usb) can write databases and audio directly to the selected USB.
These manuals were consulted for this change. They do not guarantee that an open Lexicon instance is idle.
The script therefore removes the blanket closure requirement, but refuses when Lexicon has target-USB files open.
It never quits applications or removes SQLite sidecars to force access.

## Matching and cleanup

Exact full-file hashes remain the first choice.
Metadata fallback uses an existing `ffprobe` executable. It does not install anything or add Python packages.
If ffprobe is missing, exact-hash matching continues and unmatched files are retained.
It requires nonempty embedded artist and title, the same codec/sample rate/channel count, and duration within 0.25 seconds.
Case, Unicode normalization, and whitespace differences are ignored. Remix and edit names are not removed.
Only one matching destination is permitted. Ambiguous or already-occupied destinations are skipped.
Missing tags are not replaced with guessed filenames.

**Metadata-only source files are always retained. Tags do not prove identical audio or compatible cue/grid timing.**
Both complete file hashes are recorded separately and rechecked before commit.
Recovery verifies retained metadata sources against their original hashes. It never recreates them from different destination bytes.

Cleanup is only for dedicated DJ exports. References from unrelated applications cannot be checked.
The tool commits approved path changes before removing audio.
Before each removal, it rechecks full hashes, file identities, database values, and remaining Track references.
Only recorded redundant regular files beneath `Engine Library/Music` and their newly empty parent directories can be removed.
Unsupported paths prevent cleanup. Unknown files and protected assets remain untouched.

## Noninteractive use

```sh
python3 -B dedup_dj_usb.py /Volumes/JUSTIN_CDJ1
```

The automatic backup directory receives a unique recovery subdirectory containing `m.db` and `manifest.jsonl`.
An optional `--backup-dir /existing/directory` overrides the destination.
A USB-root backup does not protect against failure or loss of that USB. Keep an independent full-export backup.

## Retry interrupted cleanup

```sh
python3 -B dedup_dj_usb.py --resume-cleanup /absolute/backup/dedup-ID/manifest.jsonl
```

Use the manifest printed by the original run after a deletion failure.
Only recorded exact duplicates are eligible. Metadata sources and unknown files remain untouched.
The backup hash, library identity, recorded tracks, and complete expected committed database state are verified.
Missing source copies are already complete. A changed database or audio file stops the retry safely.
A normal second run does not guess which unreferenced files are safe to delete.

## Recovery

**CAUTION: A database backup alone cannot reverse audio deletion. Its original paths can refer to missing files.**

The updated database remains valid after partial cleanup.
The safest recovery is to retain that database and restore source audio copies when needed:

```sh
python3 -B dedup_dj_usb.py --restore-audio /absolute/backup/dedup-ID/manifest.jsonl
```

Select the correct current volume. The same automatic application checks apply to recovery.
The recovery operation verifies retained `Contents` files against the recorded full hashes.
It restores missing Engine Music copies without overwriting existing files or changing the database.
Recovery works after a volume rename because recorded audio paths are relative and library UUIDs identify the export.
The entire manifest is validated before audio writes. Wrong-library and malformed manifests are refused.
Audio copies are staged and verified before macOS no-overwrite publication into their original paths.
A complete staging copy can be reused after interruption. An incomplete staging copy is preserved with its exact path and retry instructions.
The final audio path is never populated with a partial copy.
If retained audio changed or disappeared, recovery stops. Restore audio from your independent full-export backup instead.

To return to the original database state:

1. Restore all recorded source audio with `--restore-audio` first.
2. Keep all DJ apps closed.
3. Verify that no `-wal` or `-shm` files exist and that all rollback journals are inactive.
   Use SQLite-aware backup restoration rather than replacing the database while a journal is present.
4. Preserve the current export with an independent backup.
5. Verify the backup database's SHA-256 against the first manifest record.
6. Restore the recovery database with SQLite-aware backup semantics after restoring every audio source. Do not replace a database with sidecars.
7. Open Engine DJ Desktop and check track loading before using the export.

The tests exercise source-audio recovery followed by SQLite-aware database restoration and compare all logical values.
They do not establish power-loss recovery on USB media.

`manifest.jsonl` records preparation, commit, deletion intentions, completed deletions, and recoverable failures.
An interruption can occur between a deletion and its completion record. Audio recovery checks every planned source, not just completed records.
Retain the backup and manifest until playback and the next export cycle are verified.

## Safety boundaries and limits

- Only schema versions 3.0.1 and 3.0.2 are supported, with required identity fields and `UNIQUE(path)`.
- The installed app version does not identify its exported database schema.
- Invalid database UUIDs or empty/invalid origin IDs cause refusal.
- Unknown Track update triggers cause refusal. Known triggers are inspected and preserved.
- All tables, schema objects, and logical fields are compared before commit. Only planned `Track.path` changes are permitted.
- Database integrity is checked. Foreign-key violations must match the pre-change baseline exactly.
- SQLite updates use an in-place exclusive rollback-journal transaction, not replacement of a staged database.
- Existing empty or fully zero-header rollback journals are accepted. SQLite uses connection-local PERSIST mode to retain the main journal.
- Main-journal contents can change during an approved transaction. Auxiliary journals are never modified by this tool.
- A SQLite-aware backup is made from the validated preview snapshot. The exclusive transaction checks that the live baseline still matches.
- Backup files and recovery directory entries are synced before commit. Unsupported directory syncing causes refusal.
- Space checks reserve twice the database size plus 16 MiB on both volumes. A conservative FAT32 size limit applies to every backup.
- Empty rollback journals and journals with a complete zeroed 28-byte header do not require recovery.
- Nonzero or incomplete journal headers and all WAL/SHM state cause refusal. Never delete sidecars to bypass recovery.
- Preview uses a normal read-only SQLite connection with locking, not immutable mode.
- Inactive journal size and stable inode changes cause refusal. Timestamp-only changes do not cause false mismatches.
- macOS FAT high-bit synthetic inode numbers can change without file changes; these are excluded from identity comparisons. Complete hashes, size, timestamps, and database state checks remain active.
- macOS `._` AppleDouble metadata files are not SQLite journals and are preserved.
- Process checks run before preview and again before mutation. Inspection failure causes refusal, not a guessed closed-app result.
- Lexicon is allowed open only when its detected processes have no open paths on this USB. This is not proof it cannot start a sync later.
- The tool's exclusive lock serializes cooperating runs. It cannot prevent another app from opening the export.
- A hard interruption can leave `.dedup-dj-usb.lock`. Verify that no run remains before removing only that empty lock directory.
- Symlinks, hardlinked files, special files, traversal, and ambiguous case/Unicode spellings are rejected.
- File access uses no-follow directory-relative operations. The SQLite API still opens by pathname, with identity/baseline rechecks.
- Do not replace directories, swap mounts, or run untrusted concurrent filesystem writers. Adversarial pathname replacement is not fully prevented.
- File rechecks cannot eliminate every race with an unrelated writer. Closed apps and a quiescent export are required.
- USB filesystem/controller behavior can defeat power-loss durability. No lock or `fsync` call makes FAT media power-loss safe.
- Logical database preservation does not prove Stems or hardware playback compatibility.
- Interactive discovery and automatic process inspection target macOS. Read-only explicit-path preview supports POSIX systems.

Exit codes: `0` means all eligible work completed; `2` means unmatched/conflicting/missing/unsupported rows or incomplete cleanup; `1` means fatal refusal/failure.
A failure after commit retains the updated database. Inspect the recovery directory before retrying.

## Hardening verification

Three independent initial reviews covered architecture, safety, and usability/test boundaries.
An independent plan review preceded implementation. Two independent final reviews challenged recovery and verification; their confirmed findings were fixed.
The implementation keeps one distributable script with explicit planning, manifest validation, cleanup retry, and recovery-publication boundaries.
The current synthetic suite contains 68 tests. Run the command below to verify your checkout.
Initial hardening used synthetic fixtures only. A subsequent authorized JUSTIN_CDJ1 run updated 705 paths and removed 704 exact copies (7.01 GB).
Private run logs and music inventories are not published. Hardware validation remains pending.
Physical power-loss recovery and unsupported filesystem publication remain unverified; errors retain evidence rather than bypassing safeguards.

## Tests

```sh
python3 -B -m unittest -v
```

The files are synthetic byte payloads, not real audio or hardware evidence.
The suite covers preview writes, Unicode/case resolution, URI characters, complete-hash mismatch, collisions, source sharing, unsafe paths, sidecars, schema refusal,
non-path preservation, backups, low space, competing runs, changed inputs, interruptions, partial cleanup, recovery, and a second deduplication run.
Fault-injection tests deliberately print cleanup/log failures.

### Test strategy

```yaml
test_strategy:
  artifact: dedup_dj_usb.py
  rationale: Path updates and optional file deletion require data-integrity checks.
  criticality: HIGH
  selected_types:
    - rationale: Matching, path validation, and volume selection contain branching logic.
      type: unit
      size: small
      framework: unittest
      dependencies: []
      gate: Gate 1
    - rationale: SQLite transactions, backups, filesystem mutations, and recovery cross real boundaries.
      type: integration
      size: medium
      framework: unittest
      dependencies: [disposable SQLite fixtures, temporary directories, subprocess CLI]
      gate: Gate 2
  rejected_types:
    - reason: There is no browser component.
      type: component
    - reason: Subprocess integration tests cover the CLI without an external service.
      type: e2e
    - reason: There is no independently deployed API consumer.
      type: contract
    - reason: There is no deployment pipeline.
      type: smoke
    - reason: Standard-library-only scope uses deterministic Unicode and path partitions without new test dependencies.
      type: property-based
  deliberately_skipped:
    - why: Real USB mutation and hardware testing need separate approval and equipment access.
      what: Hardware playback, correct Stems, power loss, and recurring exports from both apps.
```

## Observed environment and remaining checks

Development used macOS, Python 3.15.1, Engine DJ 5.3.0, Rekordbox 7.2.19, and Lexicon 1.12.1.
A read-only inspection of the USB main database showed schema 3.0.2 and `Music/...` paths.
The three rollback journals were empty, which SQLite permits after a completed transaction.
After both exports were updated, an authorized real run linked all 705 tracks and deleted 704 exact copies, retaining one metadata-only source.
Post-run checks confirmed database logical preservation, unchanged retained audio/Stems/Pioneer assets, and no missing links.
The synthetic suite passes 68 tests. This evidence does not establish hardware playback or subsequent export behavior.

Before adopting routine use, test a small backed-up real export on Engine DJ Desktop and the SC Live 4.
Check track loading, cues, grids, and the correct Stems for every selected track.
Check Pioneer playback when hardware is available.
Then perform ordinary exports from both apps, including track additions/removals and playlist changes, and repeat deduplication.
Engine Sync Manager behavior after path rewriting is not established by these automated tests.

## Sources

- SQLite journal format and hot-journal rules, consulted for inactive-journal support: https://www.sqlite.org/fileformat.html
- SQLite locking and rollback recovery: https://www.sqlite.org/lockingv3.html
- SQLite persistent journals: https://www.sqlite.org/atomiccommit.html
- Python sqlite3 documentation, consulted through CPython v3.11.14: https://github.com/python/cpython/blob/v3.11.14/Doc/library/sqlite3.rst
- Shared relative-path implementation, inspected during development: https://github.com/jrgutier/rb2engine
- Lexicon Engine export guidance: https://www.lexicondj.com/manual/sync-engine-dj
- Lexicon Pioneer export guidance: https://www.lexicondj.com/manual/sync-to-pioneer-usb

Third-party path examples are implementation evidence, not manufacturer hardware certification.
