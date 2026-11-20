from __future__ import annotations

import argparse
import ctypes
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import subprocess
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import unicodedata
import uuid
from typing import Any, Iterator

SUPPORTED_SCHEMAS = {(3, 0, 1), (3, 0, 2)}
BUFFER_SIZE = 1024 * 1024
SPACE_MARGIN = 16 * 1024 * 1024
FAT32_LIMIT = 4294967295


class SafetyError(Exception):
    pass


def key(value: str) -> str:
    return unicodedata.normalize('NFC', value).casefold()


def identity(info: os.stat_result) -> tuple[int, ...]:
    inode = info.st_ino if info.st_ino < 2**63 else 0
    return (info.st_dev, inode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def checked_root(root: Path) -> Path:
    root = Path(os.path.abspath(root.expanduser()))
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise SafetyError(f'Not a regular directory (or a link): {current}')
    return root


@contextmanager
def parent_fd(root: Path, relative: str) -> Iterator[tuple[int, str]]:
    parts = relative.split('/')
    if not parts or any(part in ('', '.', '..') for part in parts):
        raise SafetyError(f'Unsafe relative path: {relative!r}')
    descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def file_digest(root: Path, relative: str) -> tuple[str, tuple[int, ...]]:
    with parent_fd(root, relative) as (directory, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise SafetyError(f'Not an independent regular file: {relative!r}')
            digest = hashlib.sha256()
            while chunk := stream.read(BUFFER_SIZE):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise SafetyError(f'File changed during hashing: {relative!r}')
            return digest.hexdigest(), identity(after)


def inventory(root: Path, relative: str) -> dict[str, str]:
    base = root / relative
    if not stat.S_ISDIR(base.lstat().st_mode):
        raise SafetyError(f'Unsafe directory: {base}')
    result: dict[str, str] = {}
    names: set[str] = set()
    for directory, subdirs, files in os.walk(base, followlinks=False):
        for name in sorted(subdirs + files):
            path = Path(directory) / name
            spelling = path.relative_to(root).as_posix()
            normalized = key(spelling)
            if normalized in names:
                raise SafetyError(f'Ambiguous case or Unicode spelling: {spelling!r}')
            names.add(normalized)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SafetyError(f'Link or special file rejected: {spelling!r}')
            result[normalized] = spelling
    return result


def path_kind(path: Any) -> tuple[str, str]:
    if not isinstance(path, str) or not path or '\\' in path or '\x00' in path:
        return 'unsafe', ''
    parts = path.split('/')
    if key(parts[0]) == 'music' and all(part not in ('', '.', '..') for part in parts):
        return 'source', 'Engine Library/' + path
    if len(parts) > 2 and parts[0] == '..' and key(parts[1]) == 'contents' and all(part not in ('', '.', '..') for part in parts[2:]):
        return 'linked', '/'.join(parts[1:])
    return 'unsafe', ''


def check_sidecars(root: Path) -> dict[str, tuple[int, ...]]:
    database_dir = root / 'Engine Library/Database2'
    journals: dict[str, tuple[int, ...]] = {}
    for path in database_dir.iterdir():
        if path.is_symlink():
            raise SafetyError(f'Database directory contains a link: {path}')
        if path.name.startswith('._'):
            continue
        if path.name.endswith(('-wal', '-shm')):
            raise SafetyError(f'WAL/SHM state is not supported: {path}. Do not delete sidecars.')
        if path.name.endswith('-journal'):
            relative = path.relative_to(root).as_posix()
            with parent_fd(root, relative) as (directory, name):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise SafetyError(f'Unsafe journal file: {path}')
                    header = stream.read(28)
                    after = os.fstat(stream.fileno())
                    current = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if identity(before) != identity(after) or identity(after) != identity(current):
                        raise SafetyError(f'Journal changed during inspection: {path}')
                    if before.st_size != 0 and (len(header) != 28 or header != bytes(28)):
                        raise SafetyError(f'Unresolved or unrecognized rollback journal: {path}. Recovery is required; do not delete it.')
                    journals[path.name] = identity(after)[:3]
    return journals


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def logical_state(conn: sqlite3.Connection, changes: dict[int, str] | None = None) -> tuple[Any, ...]:
    schema = tuple(conn.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall())
    tables = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cursor = conn.execute('SELECT * FROM ' + quoted(name))
        columns = [column[0] for column in cursor.description]
        rows = cursor.fetchall()
        if name == 'Track' and changes:
            id_index, path_index = columns.index('id'), columns.index('path')
            adjusted = []
            for row in rows:
                values = list(row)
                if values[id_index] in changes:
                    values[path_index] = changes[values[id_index]]
                adjusted.append(tuple(values))
            rows = adjusted
        tables.append((name, tuple(columns), Counter(rows)))
    return schema, tables


def health(conn: sqlite3.Connection) -> tuple[Any, ...]:
    integrity = conn.execute('PRAGMA integrity_check').fetchall()
    if integrity != [('ok',)]:
        raise SafetyError(f'Database integrity check failed: {integrity!r}')
    return tuple(sorted(conn.execute('PRAGMA foreign_key_check').fetchall(), key=repr))


def validate_schema(conn: sqlite3.Connection) -> None:
    try:
        versions = conn.execute('SELECT uuid,schemaVersionMajor,schemaVersionMinor,schemaVersionPatch FROM Information').fetchall()
        if len(versions) != 1 or not isinstance(versions[0][0], str) or not versions[0][0].strip() or tuple(versions[0][1:]) not in SUPPORTED_SCHEMAS:
            raise SafetyError('Unsupported schema or invalid database identity. Supported schemas: 3.0.1, 3.0.2.')
        required = {'id', 'path', 'originDatabaseUuid', 'originTrackId'}
        columns = {row[1] for row in conn.execute('PRAGMA table_info(Track)')}
        if not required.issubset(columns):
            raise SafetyError('Track schema lacks required identity/path fields.')
        for track_id, origin, origin_id in conn.execute('SELECT id,originDatabaseUuid,originTrackId FROM Track'):
            if not isinstance(track_id, int) or track_id <= 0 or not isinstance(origin, str) or not origin.strip() or not isinstance(origin_id, int) or origin_id <= 0:
                raise SafetyError(f'Invalid nonempty track/origin identity on row {track_id!r}.')
        unique_path = False
        for index in conn.execute('PRAGMA index_list(Track)').fetchall():
            fields = [row[2] for row in conn.execute('PRAGMA index_info(' + quoted(index[1]) + ')')]
            if index[2] and fields == ['path'] and not index[4]:
                unique_path = True
        if not unique_path:
            raise SafetyError('Expected UNIQUE(path) constraint is absent.')
        for name, table, sql in conn.execute("SELECT name,tbl_name,sql FROM sqlite_master WHERE type='trigger'"):
            text = re.sub(r'\s+', ' ', sql or '').strip().casefold()
            if table != 'Track' or not re.search(r'\bupdate\b.*?\bon track\b', text.split(' begin ')[0]):
                continue
            allowed = False
            if name == 'trigger_after_update_Track_check_Id':
                expected = "create trigger trigger_after_update_track_check_id before update on track when new.id <> old.id begin select raise(abort, 'changing track id''s are not allowed'); end"
                allowed = text == expected
            elif name == 'trigger_after_update_Track_fix_origin':
                expected = "create trigger trigger_after_update_track_fix_origin after update on track when ifnull(new.origintrackid, 0) = 0 or ifnull(new.origindatabaseuuid, '') = '' begin update track set origintrackid = new.id, origindatabaseuuid = (select uuid from information) where track.id = new.id; end"
                allowed = text == expected
            elif name == 'trigger_after_update_only_Track_timestamp':
                match = re.fullmatch(r"create trigger trigger_after_update_only_track_timestamp after update of (.+) on track for each row begin update track set lastedittime = strftime\('%s'\) where rowid=new.rowid; end", text)
                allowed = bool(match and 'path' not in {field.strip() for field in match[1].split(',')})
            if not allowed:
                raise SafetyError(f'Unexplained Track update trigger: {name}')
    except sqlite3.DatabaseError as error:
        raise SafetyError(f'Unsupported Engine schema: {error}') from error


def probe_metadata(root: Path, relative: str) -> tuple[str, str, float, str, int, int] | None:
    executable = shutil.which('ffprobe')
    if executable is None:
        raise SafetyError('Metadata inspection requires ffprobe.')
    before = identity((root / relative).lstat())
    result = subprocess.run([executable, '-v', 'error', '-protocol_whitelist', 'file', '-show_entries', 'format=duration:format_tags=artist,title:stream=codec_type,codec_name,sample_rate,channels:stream_tags=artist,title', '-of', 'json', str(root / relative)], capture_output=True, text=True, timeout=30)
    if identity((root / relative).lstat()) != before:
        raise SafetyError(f'File changed during metadata inspection: {relative!r}')
    if result.returncode:
        return None
    try:
        data = json.loads(result.stdout)
        audio = [stream for stream in data.get('streams', []) if stream.get('codec_type') == 'audio']
        if len(audio) != 1:
            return None
        stream = audio[0]
        tags = {name.casefold(): value for name, value in {**stream.get('tags', {}), **data.get('format', {}).get('tags', {})}.items()}
        artist = ' '.join(key(str(tags.get('artist', ''))).split())
        title = ' '.join(key(str(tags.get('title', ''))).split())
        duration = float(data['format']['duration'])
        codec = stream['codec_name']
        rate, channels = int(stream['sample_rate']), int(stream['channels'])
        if not artist or not title or not math.isfinite(duration) or duration <= 0 or rate <= 0 or channels <= 0:
            return None
        return artist, title, duration, codec, rate, channels
    except (ValueError, KeyError, TypeError):
        return None


@dataclass(slots=True)
class Match:
    track_id: int
    old_path: str
    new_path: str
    source: str
    destination: str
    digest: str
    source_stat: tuple[int, ...]
    destination_stat: tuple[int, ...]
    method: str = 'sha256'
    destination_digest: str = ''


@dataclass(slots=True)
class Plan:
    root: Path
    database_stat: tuple[int, ...]
    database_digest: str
    snapshot: sqlite3.Connection
    baseline: tuple[Any, ...]
    foreign_keys: tuple[Any, ...]
    journals: dict[str, tuple[int, ...]] = field(default_factory=dict)
    matches: list[Match] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)
    messages: list[str] = field(default_factory=list)
    removable_bytes: int = 0

    def close(self) -> None:
        self.snapshot.close()


def preview(root: Path, match_metadata: bool = False) -> Plan:
    if match_metadata and shutil.which('ffprobe') is None:
        print('ffprobe unavailable; continuing exact-hash matching only. Unmatched source files will be retained.')
        match_metadata = False
    root = checked_root(root)
    for name in ('Contents', 'PIONEER', 'Engine Library', 'Engine Library/Music', 'Engine Library/Database2'):
        checked_root(root / name)
    journals = check_sidecars(root)
    database = root / 'Engine Library/Database2/m.db'
    digest, db_stat = file_digest(root, 'Engine Library/Database2/m.db')
    memory = sqlite3.connect(':memory:', isolation_level=None)
    try:
        conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=0, isolation_level=None)
        try:
            conn.backup(memory)
        finally:
            conn.close()
        if check_sidecars(root) != journals:
            raise SafetyError('Journal state changed during preview.')
        if file_digest(root, 'Engine Library/Database2/m.db') != (digest, db_stat):
            raise SafetyError('Database changed during preview.')
        validate_schema(memory)
        foreign_keys = health(memory)
        contents = inventory(root, 'Contents')
        music = inventory(root, 'Engine Library/Music')
        sizes: dict[int, list[str]] = defaultdict(list)
        for actual in contents.values():
            sizes[(root / actual).stat().st_size].append(actual)
        plan = Plan(root, db_stat, digest, memory, logical_state(memory), foreign_keys, journals)
        rows = memory.execute('SELECT id,path FROM Track ORDER BY id').fetchall()
        plan.counts['total'] = len(rows)
        occupied: set[str] = set()
        source_references: dict[str, set[int]] = defaultdict(set)
        for track_id, path in rows:
            kind, relative = path_kind(path)
            if kind == 'linked':
                occupied.add(key(relative))
            elif kind == 'source':
                source_references[key(relative)].add(track_id)
        cache: dict[str, tuple[str, tuple[int, ...]]] = {}
        def hashed(relative: str) -> tuple[str, tuple[int, ...]]:
            old = cache.get(relative)
            if old and identity((root / relative).lstat()) == old[1]:
                return old
            cache[relative] = file_digest(root, relative)
            return cache[relative]
        metadata_index: dict[tuple[Any, ...], list[tuple[str, float]]] | None = None
        metadata_stats: dict[str, tuple[int, ...]] = {}
        for track_id, path in rows:
            kind, relative = path_kind(path)
            if kind == 'unsafe':
                plan.counts['unsafe'] += 1
                plan.messages.append(f'Row {track_id}: unsafe or unsupported path {path!r}')
                continue
            actual = (contents if kind == 'linked' else music).get(key(relative))
            if actual is None:
                plan.counts['missing'] += 1
                plan.messages.append(f'Row {track_id}: missing {path!r}')
                continue
            if kind == 'linked':
                hashed(actual)
                plan.counts['linked'] += 1
                continue
            source_hash, source_stat = hashed(actual)
            candidates = sorted(sizes[source_stat[2]], key=lambda candidate: (key(Path(candidate).name) != key(Path(actual).name), key(candidate), candidate))
            identical = [(candidate, hashed(candidate)) for candidate in candidates if hashed(candidate)[0] == source_hash]
            available = [(candidate, info) for candidate, info in identical if key(candidate) not in occupied]
            method = 'sha256'
            if not identical and match_metadata:
                if metadata_index is None:
                    metadata_index = defaultdict(list)
                    print('Reading audio metadata with ffprobe; source copies for metadata matches will be retained.', flush=True)
                    for candidate in sorted(contents.values()):
                        before_probe = identity((root / candidate).lstat())
                        metadata = probe_metadata(root, candidate)
                        if identity((root / candidate).lstat()) != before_probe:
                            raise SafetyError('Destination changed during metadata indexing.')
                        metadata_stats[candidate] = before_probe
                        if metadata is not None:
                            metadata_index[(metadata[0], metadata[1], *metadata[3:])].append((candidate, metadata[2]))
                metadata = probe_metadata(root, actual)
                if identity((root / actual).lstat()) != source_stat:
                    raise SafetyError('Source changed during metadata matching.')
                possible = [] if metadata is None else [candidate for candidate, duration in metadata_index.get((metadata[0], metadata[1], *metadata[3:]), []) if abs(duration - metadata[2]) <= 0.25]
                if len(possible) == 1 and key(possible[0]) not in occupied:
                    destination_info = hashed(possible[0])
                    if destination_info[1] != metadata_stats[possible[0]]:
                        raise SafetyError('Destination changed after metadata inspection.')
                    available = [(possible[0], destination_info)]
                    method = 'metadata'
                elif possible:
                    plan.counts['conflicts'] += 1
                    plan.messages.append(f'Row {track_id}: ambiguous or occupied metadata match {path!r}')
                    continue
            if not available:
                category = 'conflicts' if identical else 'unmatched'
                plan.counts[category] += 1
                plan.messages.append(f'Row {track_id}: {category} {path!r}')
                continue
            destination, (destination_hash, dest_stat) = available[0]
            occupied.add(key(destination))
            new_path = '../' + destination
            plan.matches.append(Match(track_id, path, new_path, actual, destination, source_hash, source_stat, dest_stat, method, destination_hash))
            plan.counts[method] += 1
            plan.messages.append(f'Row {track_id} [{method}]: {path!r} -> {new_path!r}')
        planned_ids = {match.track_id for match in plan.matches}
        removable = {match.source: match.source_stat[2] for match in plan.matches if match.method == 'sha256' and source_references[key(match.source)].issubset(planned_ids)}
        plan.removable_bytes = sum(removable.values()) if not plan.counts['unsafe'] else 0
        return plan
    except BaseException:
        memory.close()
        raise


def print_plan(plan: Plan, verbose: bool = False) -> None:
    print(f'Target: {str(plan.root)!r}')
    print(f'Total rows: {plan.counts["total"]}\nAlready linked: {plan.counts["linked"]}\nProposed updates: {len(plan.matches)}\nConflicts: {plan.counts["conflicts"]}\nUnmatched: {plan.counts["unmatched"]}\nMissing: {plan.counts["missing"]}\nUnsafe or unsupported: {plan.counts["unsafe"]}\nUnique removable bytes: {plan.removable_bytes}')
    print(f'Exact SHA-256 matches: {plan.counts["sha256"]}\nMetadata-only matches (source retained): {plan.counts["metadata"]}')
    if verbose:
        print('\n'.join(plan.messages))


def partial_code(plan: Plan) -> int:
    return 2 if any(plan.counts[name] for name in ('conflicts', 'unmatched', 'missing', 'unsafe')) else 0


@contextmanager
def tool_lock(root: Path) -> Iterator[None]:
    lock = root / '.dedup-dj-usb.lock'
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SafetyError(f'Another run or interrupted run owns {lock}. Verify no run remains before removing this empty lock directory.') from error
    try:
        yield
    finally:
        lock.rmdir()


def verify_match(root: Path, match: Match) -> None:
    if file_digest(root, match.source) != (match.digest, match.source_stat):
        raise SafetyError(f'Source changed: {match.source!r}')
    if file_digest(root, match.destination) != (match.destination_digest or match.digest, match.destination_stat):
        raise SafetyError(f'Destination changed: {match.destination!r}')


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise SafetyError(f'Filesystem cannot sync recovery directory {directory}: {error}. Use a backup directory on your computer.') from error
    finally:
        os.close(descriptor)


def append_record(stream: Any, record: dict[str, Any]) -> None:
    stream.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def unlink_source(root: Path, match: Match) -> None:
    with parent_fd(root, match.source) as (directory, name):
        if identity(os.stat(name, dir_fd=directory, follow_symlinks=False)) != match.source_stat:
            raise SafetyError(f'Source changed before unlink: {match.source!r}')
        os.unlink(name, dir_fd=directory)


def remove_empty_parents(root: Path, source: str) -> None:
    directory = (root / source).parent
    boundary = root / 'Engine Library/Music'
    while directory != boundary:
        relative = directory.relative_to(root).as_posix()
        try:
            with parent_fd(root, relative) as (parent, name):
                os.rmdir(name, dir_fd=parent)
        except OSError:
            break
        directory = directory.parent


def apply_plan(plan: Plan, backup_dir: Path, delete: bool = False) -> int:
    root = plan.root
    backup_dir = checked_root(backup_dir)
    for protected in ('Contents', 'PIONEER', 'Engine Library'):
        protected_path = root / protected
        if key(str(backup_dir)) == key(str(protected_path)) or key(str(backup_dir)).startswith(key(str(protected_path)) + '/') or any(parent.samefile(protected_path) for parent in (backup_dir, *backup_dir.parents)):
            raise SafetyError('Backup directory must be outside Contents, PIONEER, and Engine Library.')
    required = (root / 'Engine Library/Database2/m.db').stat().st_size * 2 + SPACE_MARGIN
    if required > FAT32_LIMIT or shutil.disk_usage(backup_dir).free < required or shutil.disk_usage(root).free < required:
        raise SafetyError('Insufficient free space or conservative FAT32 backup limit exceeded.')
    if not plan.matches:
        return partial_code(plan)
    with tool_lock(root):
        if check_sidecars(root) != plan.journals:
            raise SafetyError('Journal state changed since preview.')
        if file_digest(root, 'Engine Library/Database2/m.db') != (plan.database_digest, plan.database_stat):
            raise SafetyError('Database changed since preview.')
        conn = sqlite3.connect((root / 'Engine Library/Database2/m.db').as_uri() + '?mode=rw', uri=True, timeout=0, isolation_level=None)
        recovery: Path | None = None
        stream = None
        committed = False
        try:
            if conn.execute('PRAGMA journal_mode').fetchone()[0] not in ('delete', 'truncate', 'persist'):
                raise SafetyError('Only rollback journal modes DELETE, TRUNCATE, and PERSIST are supported.')
            if 'm.db-journal' in plan.journals:
                if conn.execute('PRAGMA journal_mode=PERSIST').fetchone()[0] != 'persist':
                    raise SafetyError('Cannot retain the existing inactive rollback journal safely.')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN EXCLUSIVE')
            checked_root(root)
            if check_sidecars(root) != plan.journals:
                raise SafetyError('Journal changed before exclusive transaction.')
            if file_digest(root, 'Engine Library/Database2/m.db') != (plan.database_digest, plan.database_stat):
                raise SafetyError('Database file was replaced before transaction.')
            validate_schema(conn)
            if logical_state(conn) != plan.baseline or health(conn) != plan.foreign_keys:
                raise SafetyError('Database changed before exclusive transaction.')
            for match in plan.matches:
                verify_match(root, match)
            inventory(root, 'Contents')
            inventory(root, 'Engine Library/Music')
            recovery = backup_dir / ('dedup-' + uuid.uuid4().hex)
            recovery.mkdir(mode=0o700)
            print(f'Recovery directory: {str(recovery)!r}', flush=True)
            backup_path = recovery / 'm.db'
            descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            backup = sqlite3.connect(backup_path)
            try:
                plan.snapshot.backup(backup)
                if logical_state(backup) != plan.baseline or health(backup) != plan.foreign_keys:
                    raise SafetyError('SQLite backup did not preserve the baseline.')
            finally:
                backup.close()
            with backup_path.open('rb') as backup_file:
                os.fsync(backup_file.fileno())
            stream = (recovery / 'manifest.jsonl').open('x', encoding='utf-8')
            append_record(stream, {'event': 'prepared', 'format': 1, 'root': str(root), 'backup': 'm.db', 'backup_sha256': hashlib.sha256(backup_path.read_bytes()).hexdigest(), 'changes': [{'id': match.track_id, 'old_path': match.old_path, 'new_path': match.new_path, 'source': match.source, 'destination': match.destination, 'sha256': match.digest, 'bytes': match.source_stat[2], 'method': match.method, 'destination_sha256': match.destination_digest or match.digest} for match in plan.matches]})
            sync_directory(recovery)
            sync_directory(backup_dir)
            print(f'Backup and recovery manifest: {str(recovery)!r}', flush=True)
            changes = {match.track_id: match.new_path for match in plan.matches}
            for match in plan.matches:
                cursor = conn.execute('UPDATE Track SET path=? WHERE id=? AND path=?', (match.new_path, match.track_id, match.old_path))
                if cursor.rowcount != 1:
                    raise SafetyError('Planned row no longer matches.')
            if logical_state(conn) != logical_state(plan.snapshot, changes) or health(conn) != plan.foreign_keys:
                raise SafetyError('Non-path database values or foreign-key baseline changed. Transaction aborted.')
            for match in plan.matches:
                verify_match(root, match)
            checked_root(root)
            if identity((root / 'Engine Library/Database2/m.db').lstat())[:2] != plan.database_stat[:2]:
                raise SafetyError('Database inode changed before commit.')
            conn.execute('COMMIT')
            committed = True
            current_journals = check_sidecars(root)
            auxiliary_before = {name: info for name, info in plan.journals.items() if name != 'm.db-journal'}
            if {name: info for name, info in current_journals.items() if name != 'm.db-journal'} != auxiliary_before:
                raise SafetyError('Auxiliary journal changed; cleanup stopped.')
            append_record(stream, {'event': 'committed'})
            result = partial_code(plan)
            if delete:
                if plan.counts['unsafe']:
                    append_record(stream, {'event': 'cleanup_skipped', 'reason': 'Unsupported paths prevent a complete reference check.'})
                    return 2
                processed: set[str] = set()
                for match in plan.matches:
                    if match.source in processed or match.method != 'sha256':
                        continue
                    processed.add(match.source)
                    try:
                        conn.execute('BEGIN EXCLUSIVE')
                        if identity((root / 'Engine Library/Database2/m.db').lstat())[:2] != plan.database_stat[:2]:
                            raise SafetyError('Database replaced after commit; cleanup stopped.')
                        if logical_state(conn) != logical_state(plan.snapshot, changes) or health(conn) != plan.foreign_keys:
                            raise SafetyError('Database changed after commit; cleanup stopped.')
                        verify_match(root, match)
                        remaining = conn.execute('SELECT path FROM Track').fetchall()
                        source_info = match.source_stat[:2]
                        for (path,) in remaining:
                            kind, relative = path_kind(path)
                            if kind == 'unsafe':
                                raise SafetyError('Unresolved database reference; source retained.')
                            if kind == 'source':
                                music = inventory(root, 'Engine Library/Music')
                                actual = music.get(key(relative))
                                if actual and (key(actual) == key(match.source) or source_info[1] != 0 and identity((root / actual).lstat())[:2] == source_info):
                                    raise SafetyError('A database row still references this physical source.')
                        append_record(stream, {'event': 'delete_intent', 'source': match.source})
                        unlink_source(root, match)
                        append_record(stream, {'event': 'deleted', 'source': match.source})
                        remove_empty_parents(root, match.source)
                    except (OSError, SafetyError, sqlite3.DatabaseError) as error:
                        result = 2
                        append_record(stream, {'event': 'cleanup_failed', 'source': match.source, 'reason': str(error)})
                        print(f'Paths committed; cleanup incomplete: {error}. Recovery manifest: {recovery / "manifest.jsonl"}', file=sys.stderr)
                        if str(error).startswith(('Database replaced', 'Database changed')):
                            break
                    finally:
                        if conn.in_transaction:
                            conn.execute('ROLLBACK')
            append_record(stream, {'event': 'finished', 'exit_code': result})
            return result
        except BaseException as error:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            if stream is not None:
                try:
                    append_record(stream, {'event': 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', 'committed': committed, 'reason': str(error)})
                except (OSError, ValueError):
                    print(f'Recovery log write failed at {recovery}. Original failure: {error}', file=sys.stderr)
            if committed:
                raise SafetyError(f'Paths committed; retain updated database and inspect recovery manifest at {recovery}. {error}') from error
            if recovery is not None:
                print(f'No changes committed. Incomplete backup evidence retained at {recovery}: {error}', file=sys.stderr)
            raise
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError as error:
                    print(f'Recovery log close failed at {recovery}: {error}', file=sys.stderr)
            conn.close()


def validated_manifest(root: Path, manifest: Path) -> tuple[dict[str, Any], sqlite3.Connection]:
    check_sidecars(root)
    with manifest.open(encoding='utf-8') as stream:
        first = json.loads(stream.readline())
    if not isinstance(first, dict) or first.get('event') != 'prepared' or first.get('format') != 1 or not isinstance(first.get('changes'), list) or first.get('backup') != 'm.db':
        raise SafetyError('Unsupported recovery manifest.')
    backup_path = manifest.parent / 'm.db'
    if hashlib.sha256(backup_path.read_bytes()).hexdigest() != first.get('backup_sha256'):
        raise SafetyError('Recovery backup hash does not match manifest.')
    backup = sqlite3.connect(backup_path.as_uri() + '?mode=ro', uri=True)
    try:
        validate_schema(backup)
        health(backup)
        live = sqlite3.connect((root / 'Engine Library/Database2/m.db').as_uri() + '?mode=ro', uri=True)
        try:
            validate_schema(live)
            if backup.execute('SELECT uuid FROM Information').fetchall() != live.execute('SELECT uuid FROM Information').fetchall():
                raise SafetyError('Recovery manifest belongs to a different library.')
            seen_ids: set[int] = set()
            seen_destinations: set[str] = set()
            for change in first['changes']:
                if not isinstance(change, dict):
                    raise SafetyError('Invalid manifest entry.')
                track_id = change.get('id')
                source, destination = change.get('source'), change.get('destination')
                method = change.get('method', 'sha256')
                if type(track_id) is not int or track_id <= 0 or track_id in seen_ids or method not in ('sha256', 'metadata'):
                    raise SafetyError('Invalid or duplicate manifest identity/method.')
                if not isinstance(source, str) or not source.startswith('Engine Library/Music/') or path_kind(source.removeprefix('Engine Library/'))[0] != 'source' or not isinstance(destination, str) or not destination.startswith('Contents/') or path_kind('../' + destination)[0] != 'linked' or key(destination) in seen_destinations:
                    raise SafetyError('Unsafe or conflicting recovery paths.')
                old_kind, old_relative = path_kind(change.get('old_path'))
                if change.get('new_path') != '../' + destination or old_kind != 'source' or key(old_relative) != key(source):
                    raise SafetyError('Manifest paths do not correspond to recorded audio.')
                if type(change.get('bytes')) is not int or change['bytes'] < 0 or not re.fullmatch('[0-9a-f]{64}', str(change.get('sha256', ''))):
                    raise SafetyError('Invalid recorded audio hash or size.')
                seen_ids.add(track_id)
                seen_destinations.add(key(destination))
                original = backup.execute('SELECT path,originDatabaseUuid,originTrackId FROM Track WHERE id=?', (track_id,)).fetchone()
                current = live.execute('SELECT path,originDatabaseUuid,originTrackId FROM Track WHERE id=?', (track_id,)).fetchone()
                if not original or original[0] != change['old_path'] or not current or current[1:] != original[1:] or current[0] not in (change['old_path'], change['new_path']):
                    raise SafetyError('Recorded track identity/path differs from this library.')
            return first, backup
        finally:
            live.close()
    except BaseException:
        backup.close()
        raise


def resume_cleanup(root: Path, manifest: Path) -> int:
    root = checked_root(root)
    record, backup = validated_manifest(root, manifest)
    database = root / 'Engine Library/Database2/m.db'
    database_inode = identity(database.lstat())[:2]
    changes = {change['id']: change['new_path'] for change in record['changes']}
    expected = logical_state(backup, changes)
    baseline_health = health(backup)
    result = 0
    try:
        with tool_lock(root):
            conn = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=0, isolation_level=None)
            try:
                for change in record['changes']:
                    if change.get('method', 'sha256') != 'sha256':
                        continue
                    conn.execute('BEGIN EXCLUSIVE')
                    try:
                        if identity(database.lstat())[:2] != database_inode or logical_state(conn) != expected or health(conn) != baseline_health:
                            raise SafetyError('Database differs from the recorded committed state; cleanup stopped.')
                        music = inventory(root, 'Engine Library/Music')
                        actual = music.get(key(change['source']))
                        if actual is None:
                            continue
                        digest, source_stat = file_digest(root, actual)
                        destination_digest, destination_stat = file_digest(root, change['destination'])
                        if digest != change['sha256'] or destination_digest != digest or source_stat[2] != change['bytes']:
                            raise SafetyError('Recorded duplicate changed; source retained.')
                        for (path,) in conn.execute('SELECT path FROM Track'):
                            kind, relative = path_kind(path)
                            if kind == 'unsafe':
                                raise SafetyError('Unsupported database reference; cleanup stopped.')
                            if kind == 'source' and key(relative) in music and (key(music[key(relative)]) == key(actual) or source_stat[1] != 0 and identity((root / music[key(relative)]).lstat())[:2] == source_stat[:2]):
                                raise SafetyError('A database row still needs this source.')
                        match = Match(change['id'], change['old_path'], change['new_path'], actual, change['destination'], digest, source_stat, destination_stat)
                        with manifest.open('a', encoding='utf-8') as stream:
                            append_record(stream, {'event': 'delete_intent', 'source': actual})
                            unlink_source(root, match)
                            append_record(stream, {'event': 'deleted', 'source': actual})
                        remove_empty_parents(root, actual)
                    except (SafetyError, OSError, sqlite3.DatabaseError) as error:
                        print(f'Cleanup stopped; database retained: {error}. Manifest: {manifest}', file=sys.stderr)
                        result = 2
                        break
                    finally:
                        conn.execute('ROLLBACK')
            finally:
                conn.close()
        deleted_records = []
        with manifest.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                    if entry.get('event') == 'deleted':
                        deleted_records.append(entry['source'])
                except (ValueError, KeyError):
                    continue
        deleted_sources = set(deleted_records)
        removed_bytes = sum(change['bytes'] for change in record['changes'] if change['source'] in deleted_sources)
        print(f'Cleanup result: {len(deleted_sources)} recorded deletions, {removed_bytes:,} bytes. Metadata sources retained. Exit code: {result}. Manifest: {manifest}')
        return result
    finally:
        backup.close()


def publish_recovery(directory: int, staging: str, destination: str) -> None:
    if sys.platform != 'darwin':
        raise SafetyError('Crash-safe recovery publication requires macOS renameatx_np.')
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renameatx_np
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(directory, os.fsencode(staging), directory, os.fsencode(destination), 4) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def restore_audio(root: Path, manifest: Path) -> int:
    root = checked_root(root)
    first, backup = validated_manifest(root, manifest)
    backup.close()
    music = inventory(root, 'Engine Library/Music')
    needed = 0
    for change in first['changes']:
        source = music.get(key(change['source']))
        if source is not None:
            if file_digest(root, source)[0] != change['sha256']:
                raise SafetyError('Existing source differs; use your independent audio backup.')
        elif change.get('method', 'sha256') == 'metadata':
            raise SafetyError('Metadata source is missing; use your independent audio backup.')
        else:
            digest, info = file_digest(root, change['destination'])
            if digest != change['sha256'] or info[2] != change['bytes']:
                raise SafetyError('Retained audio changed; use your independent audio backup.')
            needed += change['bytes']
    if shutil.disk_usage(root).free < needed + SPACE_MARGIN:
        raise SafetyError('Insufficient free space for complete audio recovery.')
    with tool_lock(root):
        for change in first['changes']:
            source, destination = change['source'], change['destination']
            if change.get('method', 'sha256') == 'metadata':
                if not isinstance(source, str) or not source.startswith('Engine Library/Music/') or path_kind(source.removeprefix('Engine Library/'))[0] != 'source':
                    raise SafetyError('Unsafe metadata recovery source.')
                if file_digest(root, source)[0] != change['sha256']:
                    raise SafetyError('Metadata-matched source changed; recover it from your independent backup.')
                continue
            if not isinstance(source, str) or not source.startswith('Engine Library/Music/') or path_kind(source.removeprefix('Engine Library/'))[0] != 'source' or not isinstance(destination, str) or not destination.startswith('Contents/') or path_kind('../' + destination)[0] != 'linked':
                raise SafetyError('Unsafe recovery path.')
            digest, dest_stat = file_digest(root, destination)
            if digest != change['sha256'] or dest_stat[2] != change['bytes']:
                raise SafetyError(f'Recovery destination no longer matches: {destination!r}')
            music = inventory(root, 'Engine Library/Music')
            if key(source) in music:
                if file_digest(root, music[key(source)])[0] != digest:
                    raise SafetyError(f'Existing source differs: {source!r}')
                continue
            if shutil.disk_usage(root).free < change['bytes'] + SPACE_MARGIN:
                raise SafetyError('Insufficient free space for audio recovery.')
            current = root
            for part in source.split('/')[:-1]:
                current /= part
                try:
                    current.mkdir()
                    sync_directory(current.parent)
                except FileExistsError:
                    pass
                checked_root(current)
            with parent_fd(root, source) as (directory, name), parent_fd(root, destination) as (dest_dir, dest_name):
                token = hashlib.sha256((str(first['backup_sha256']) + source).encode()).hexdigest()[:24]
                staging = '.dedup-restore-' + token + '.part'
                staging_relative = str(Path(source).parent / staging)
                if (root / staging_relative).exists():
                    stage_hash, stage_stat = file_digest(root, staging_relative)
                    if stage_hash != digest or stage_stat[2] != change['bytes']:
                        raise SafetyError(f'Incomplete recovery staging retained at {root / staging_relative}. Remove only this recorded staging file after confirming no recovery run remains, then retry.')
                else:
                    source_fd = os.open(dest_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dest_dir)
                    try:
                        output_fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                    except BaseException:
                        os.close(source_fd)
                        raise
                    with os.fdopen(source_fd, 'rb') as incoming, os.fdopen(output_fd, 'wb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing, BUFFER_SIZE)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if file_digest(root, staging_relative)[0] != digest:
                        raise SafetyError('Recovery staging verification failed; final source was not created.')
                if file_digest(root, destination) != (digest, dest_stat):
                    raise SafetyError('Destination changed; verified staging retained.')
                publish_recovery(directory, staging, name)
                os.fsync(directory)
        print('Verified source audio restored. Database unchanged. Retain the updated database or follow the README before restoring its backup.')
    return 0


def select_volume(volumes_dir: Path = Path('/Volumes')) -> Path:
    volumes = sorted((path for path in volumes_dir.iterdir() if not path.is_symlink() and path.is_dir() and os.path.ismount(path)), key=lambda path: (key(path.name), path.name))
    if volumes_dir != Path('/Volumes'):
        volumes = sorted((path for path in volumes_dir.iterdir() if not path.is_symlink() and path.is_dir()), key=lambda path: (key(path.name), path.name))
    if not volumes:
        raise SafetyError('No mounted volumes found. You can also supply an explicit export-root path.')
    for number, volume in enumerate(volumes, 1):
        marker = ' [DJ export]' if (volume / 'Contents').is_dir() and (volume / 'Engine Library/Database2/m.db').is_file() else ''
        print(f'{number}. {volume.name!r}{marker}')
    while True:
        answer = input('Select a volume number (q to cancel): ').strip()
        if answer.casefold() == 'q':
            raise SafetyError('Cancelled; nothing changed.')
        if answer.isdigit() and 1 <= int(answer) <= len(volumes):
            return volumes[int(answer) - 1]
        print('Enter a listed number, or q.')


def check_running_apps(root: Path) -> None:
    if sys.platform != 'darwin':
        raise SafetyError('Automatic DJ app detection currently requires macOS.')
    processes = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=10)
    if processes.returncode or processes.stderr.strip() or not processes.stdout.strip():
        raise SafetyError('Cannot inspect running applications; refusing mutation.')
    blocked: set[str] = set()
    lexicon_pids: list[str] = []
    for line in processes.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            raise SafetyError('Incomplete process inspection; refusing mutation.')
        process = fields[1].casefold()
        if '/engine dj.app/' in process or Path(process).name in ('engine dj', 'engine prime'):
            blocked.add('Engine DJ')
        elif '/rekordbox.app/' in process or Path(process).name == 'rekordbox':
            blocked.add('Rekordbox')
        elif '/lexicon.app/' in process or Path(process).name == 'lexicon':
            lexicon_pids.append(fields[0])
    if blocked:
        raise SafetyError('Close these applications before running: ' + ', '.join(sorted(blocked)))
    if lexicon_pids:
        files = subprocess.run(['/usr/sbin/lsof', '-n', '-P', '-a', '-p', ','.join(lexicon_pids), '-Fn'], capture_output=True, text=True, timeout=10)
        if files.returncode != 0 or files.stderr.strip() or not files.stdout.strip():
            raise SafetyError('Cannot inspect Lexicon USB access; close Lexicon before running.')
        observed: dict[str, int] = {}
        current_pid = ''
        for line in files.stdout.splitlines():
            if line.startswith('p') and line[1:].isdigit():
                current_pid = line[1:]
                observed.setdefault(current_pid, 0)
            elif line.startswith('n') and current_pid and line[1:]:
                observed[current_pid] += 1
            else:
                raise SafetyError('Malformed Lexicon handle evidence; close Lexicon before running.')
        if any(observed.get(pid, 0) == 0 for pid in lexicon_pids):
            raise SafetyError('Incomplete Lexicon handle inspection; close Lexicon before running.')
        target = key(str(root))
        for line in files.stdout.splitlines():
            if line.startswith('n'):
                reported = Path(line[1:])
                same_target = key(line[1:]) == target or key(line[1:]).startswith(target + '/')
                if not same_target and reported.is_absolute():
                    for ancestor in (reported, *reported.parents):
                        try:
                            if ancestor.samefile(root):
                                same_target = True
                                break
                        except OSError:
                            continue
                if same_target:
                    raise SafetyError('Lexicon has files open on the target USB. Stop its sync and close Lexicon before running.')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Select a DJ USB, match audio, back up, update paths, and remove exact duplicates. Normal runs authorize writes; use --dry-run for preview only.')
    parser.add_argument('usb', nargs='?', type=Path, help='Export root; omitted to select a mounted macOS volume interactively')
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Preview only; write nothing to the USB')
    parser.add_argument('--backup-dir', type=Path, help='Existing explicit backup directory, outside protected asset directories')
    parser.add_argument('--restore-audio', type=Path, metavar='MANIFEST', help='Restore removed source copies from retained verified Contents files; do not modify database')
    parser.add_argument('--resume-cleanup', type=Path, metavar='MANIFEST', help='Retry only recorded exact-duplicate cleanup after committed updates')
    args = parser.parse_args(argv)
    plan = None
    try:
        if sys.version_info < (3, 11) or os.name != 'posix':
            raise SafetyError('Python 3.11+ on a POSIX system is required. Interactive volume discovery targets macOS.')
        if (args.restore_audio or args.resume_cleanup) and args.dry_run or args.restore_audio and args.resume_cleanup:
            raise SafetyError('Recovery/retry modes cannot be combined with each other or --dry-run.')
        root = checked_root(args.usb if args.usb is not None else select_volume())
        mutation = not args.dry_run
        if mutation:
            check_running_apps(root)
            print(f'Target: {str(root)!r}. Normal runs update paths and remove verified exact duplicates. Use --dry-run for preview.')
        if args.restore_audio:
            print('Restoring verified audio copies; database remains unchanged.')
            return restore_audio(root, args.restore_audio)
        if args.resume_cleanup:
            return resume_cleanup(root, args.resume_cleanup)
        plan = preview(root, match_metadata=True)
        print_plan(plan, args.verbose)
        if args.dry_run:
            print('Preview only. No files written to the USB.')
            return partial_code(plan)
        check_running_apps(root)
        backup_dir = args.backup_dir or root / 'DJ USB Backups'
        if not plan.matches:
            return partial_code(plan)
        if args.backup_dir is None:
            required = (root / 'Engine Library/Database2/m.db').stat().st_size * 2 + SPACE_MARGIN
            if required > FAT32_LIMIT or shutil.disk_usage(root).free < required:
                raise SafetyError('Insufficient USB space for backup and transaction.')
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            sync_directory(root)
        return apply_plan(plan, backup_dir, True)
    except (SafetyError, OSError, sqlite3.DatabaseError, ValueError, KeyError, TypeError, EOFError, subprocess.TimeoutExpired) as error:
        print(f'Error: {str(error) or type(error).__name__}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Inspect any printed recovery directory before retrying.', file=sys.stderr)
        return 1
    finally:
        if plan is not None:
            plan.close()


if __name__ == '__main__':
    raise SystemExit(main())
