from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import json
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('dedup_dj_usb.py')


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-dj-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'USB # % ? é'
        for name in ('Contents', 'PIONEER', 'Engine Library/Music', 'Engine Library/Database2', 'Engine Library/Stems'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'Engine Library/Database2/m.db'
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript('CREATE TABLE Information(uuid TEXT, schemaVersionMajor INTEGER, schemaVersionMinor INTEGER, schemaVersionPatch INTEGER); INSERT INTO Information VALUES("synthetic-uuid",3,0,2); CREATE TABLE Track(id INTEGER PRIMARY KEY, path TEXT UNIQUE, originDatabaseUuid TEXT, originTrackId INTEGER, cues BLOB); CREATE TABLE Playlist(id INTEGER, trackId INTEGER); INSERT INTO Playlist VALUES(1,1);')
        (self.root / 'Engine Library/Stems/synthetic.stem').write_bytes(b'synthetic stems')
        (self.root / 'PIONEER/synthetic.pdb').write_bytes(b'synthetic pioneer')
        self.backups = Path(self.temp.name).resolve() / 'backups'
        self.backups.mkdir()

    def run_cli(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, '-B', str(SCRIPT), str(self.root), *args], input=input_text, text=True, capture_output=True)

    def add(self, source: str = 'Music/Artist/source.mp3', target: str = 'Artist/target.mp3', data: bytes = b'synthetic audio', track_id: int = 1) -> tuple[Path, Path]:
        src = self.root / 'Engine Library' / source
        dst = self.root / 'Contents' / target
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(data)
        dst.write_bytes(data)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO Track VALUES(?,?,?,?,?)', (track_id, source, 'origin', track_id, b'cues and grids'))
        return src, dst

    def tree(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def module(self):
        spec = importlib.util.spec_from_file_location('dedup_test_module', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def apply(self, delete: bool = False) -> subprocess.CompletedProcess[str]:
        if delete:
            return self.run_cli('--backup-dir', str(self.backups))
        module = self.module()
        output, errors = io.StringIO(), io.StringIO()
        plan = None
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                plan = module.preview(self.root)
                module.print_plan(plan)
                code = module.apply_plan(plan, self.backups, False)
            except (module.SafetyError, OSError, sqlite3.DatabaseError) as error:
                code = 1
                errors.write(str(error))
            finally:
                if plan is not None:
                    plan.close()
        return subprocess.CompletedProcess([], code, output.getvalue(), errors.getvalue())

    def test_default_cli_applies_cleanup_with_metadata_enabled(self) -> None:
        src, dst = self.add()
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), str(self.root), '--backup-dir', str(self.backups)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(src.exists())

    def test_dry_run_cli_never_mutates(self) -> None:
        self.add()
        before = self.tree()
        result = subprocess.run([sys.executable, '-B', str(SCRIPT), str(self.root), '--dry-run'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tree(), before)

    def test_default_cli_creates_usb_backup_without_prompts(self) -> None:
        src, dst = self.add()
        module = self.module()
        with patch.object(module, 'check_running_apps'), patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
            self.assertEqual(module.main([str(self.root)]), 0)
        self.assertFalse(src.exists())
        self.assertEqual(len(list((self.root / 'DJ USB Backups').glob('*/m.db'))), 1)

    def test_running_engine_or_rekordbox_blocks(self) -> None:
        module = self.module()
        for executable in ('/Applications/Engine DJ.app/Contents/MacOS/Engine DJ', '/Applications/rekordbox 7/rekordbox.app/Contents/MacOS/rekordbox'):
            result = subprocess.CompletedProcess([], 0, '123 ' + executable + '\n', '')
            with self.subTest(executable=executable), patch.object(module.subprocess, 'run', return_value=result):
                with self.assertRaises(module.SafetyError):
                    module.check_running_apps(self.root)

    def test_idle_lexicon_allowed_but_usb_access_blocked(self) -> None:
        module = self.module()
        processes = subprocess.CompletedProcess([], 0, '123 /Applications/Lexicon.app/Contents/MacOS/Lexicon\n', '')
        for files, blocked in (('p123\nn/Applications/Lexicon.app\n', False), ('p123\nn' + str(self.db) + '\n', True)):
            listing = subprocess.CompletedProcess([], 0, files, '')
            with patch.object(module.subprocess, 'run', side_effect=[processes, listing]):
                if blocked:
                    with self.assertRaises(module.SafetyError):
                        module.check_running_apps(self.root)
                else:
                    module.check_running_apps(self.root)

    def test_lexicon_usb_path_case_difference_blocks(self) -> None:
        module = self.module()
        processes = subprocess.CompletedProcess([], 0, '123 /Applications/Lexicon.app/Contents/MacOS/Lexicon\n', '')
        listing = subprocess.CompletedProcess([], 0, 'p123\nn' + str(self.db).swapcase() + '\n', '')
        with patch.object(module.subprocess, 'run', side_effect=[processes, listing]):
            with self.assertRaises(module.SafetyError):
                module.check_running_apps(self.root)

    def test_app_opened_after_preview_blocks_before_backup(self) -> None:
        self.add()
        module = self.module()
        with patch.object(module, 'check_running_apps', side_effect=[None, module.SafetyError('Rekordbox opened')]):
            self.assertEqual(module.main([str(self.root)]), 1)
        self.assertFalse((self.root / 'DJ USB Backups').exists())

    def test_wrong_library_manifest_refuses_recovery(self) -> None:
        self.add()
        self.assertEqual(self.apply(True).returncode, 0)
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE Information SET uuid="other-library"')
        before = self.tree()
        module = self.module()
        with self.assertRaises(module.SafetyError):
            module.restore_audio(self.root, manifest)
        self.assertEqual(self.tree(), before)

    def test_malformed_later_manifest_entry_writes_nothing(self) -> None:
        self.add()
        self.assertEqual(self.apply(True).returncode, 0)
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        record = json.loads(manifest.read_text().splitlines()[0])
        record['changes'].append({'source': '../bad'})
        manifest.write_text(json.dumps(record) + '\n')
        before = self.tree()
        module = self.module()
        with self.assertRaises((module.SafetyError, KeyError)):
            module.restore_audio(self.root, manifest)
        self.assertEqual(self.tree(), before)

    def test_resume_recorded_cleanup_after_apply_only(self) -> None:
        src, dst = self.add()
        self.assertEqual(self.apply(False).returncode, 0)
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        module = self.module()
        self.assertEqual(module.resume_cleanup(self.root, manifest), 0)
        self.assertFalse(src.exists())
        self.assertEqual(module.resume_cleanup(self.root, manifest), 0)

    def test_recovery_staging_never_overwrites_existing_audio(self) -> None:
        self.add()
        self.assertEqual(self.apply(True).returncode, 0)
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        module = self.module()
        with patch.object(module, 'publish_recovery', side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):
                module.restore_audio(self.root, manifest)
        source = self.root / 'Engine Library/Music/Artist/source.mp3'
        self.assertFalse(source.exists())
        self.assertEqual(len(list(source.parent.glob('.dedup-restore-*.part'))), 1)
        self.assertEqual(module.restore_audio(self.root, manifest), 0)
        self.assertEqual(source.read_bytes(), b'synthetic audio')

    def test_unicode_manifest_supports_recovery(self) -> None:
        src, dst = self.add(source='Music/CAFÉ/Track.mp3')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE Track SET path=?', ('music/cafe\u0301/TRACK.MP3',))
        self.assertEqual(self.apply(True).returncode, 0)
        module = self.module()
        self.assertEqual(module.restore_audio(self.root, next(self.backups.glob('*/manifest.jsonl'))), 0)
        self.assertEqual(src.read_bytes(), dst.read_bytes())

    def test_empty_lexicon_filename_is_not_evidence(self) -> None:
        module = self.module()
        records = [subprocess.CompletedProcess([], 0, '123 /Applications/Lexicon.app/Contents/MacOS/Lexicon\n', ''), subprocess.CompletedProcess([], 0, 'p123\nn\n', '')]
        with patch.object(module.subprocess, 'run', side_effect=records):
            with self.assertRaises(module.SafetyError):
                module.check_running_apps(self.root)

    def test_database_replacement_after_commit_retains_sources(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        replacement = Path(self.temp.name).resolve() / 'replacement.db'
        replacement.write_bytes(self.db.read_bytes())
        original = module.append_record
        def replace(stream, record) -> None:
            original(stream, record)
            if record['event'] == 'committed':
                os.replace(replacement, self.db)
        with patch.object(module, 'append_record', side_effect=replace):
            self.assertEqual(module.apply_plan(plan, self.backups, True), 2)
        self.assertTrue(src.exists())

    def test_partial_cleanup_retry_removes_only_recorded_duplicate(self) -> None:
        src, dst = self.add()
        unknown = self.root / 'Engine Library/Music/unknown.bin'
        unknown.write_bytes(b'unknown')
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        with patch.object(module, 'unlink_source', side_effect=OSError('synthetic failure')):
            self.assertEqual(module.apply_plan(plan, self.backups, True), 2)
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        self.assertEqual(module.resume_cleanup(self.root, manifest), 0)
        self.assertFalse(src.exists())
        self.assertEqual(unknown.read_bytes(), b'unknown')

    def test_multiple_lexicon_pids_require_complete_evidence(self) -> None:
        module = self.module()
        processes = subprocess.CompletedProcess([], 0, '123 /Applications/Lexicon.app/Contents/MacOS/Lexicon\n124 /Applications/Lexicon.app/Contents/Frameworks/Lexicon Helper\n', '')
        files = subprocess.CompletedProcess([], 0, 'p123\nn/Applications/Lexicon.app\n', '')
        with patch.object(module.subprocess, 'run', side_effect=[processes, files]):
            with self.assertRaises(module.SafetyError):
                module.check_running_apps(self.root)

    def test_null_manifest_refuses_without_writes(self) -> None:
        self.add()
        manifest = self.backups / 'null.jsonl'
        manifest.write_text('null\n')
        module = self.module()
        before = self.tree()
        with self.assertRaises(module.SafetyError):
            module.restore_audio(self.root, manifest)
        self.assertEqual(before, self.tree())

    def test_fat_synthetic_inode_does_not_create_false_change(self) -> None:
        module = self.module()
        first = type('Stat', (), {'st_dev': 1, 'st_ino': 2**64 - 10, 'st_size': 15, 'st_mtime_ns': 7, 'st_ctime_ns': 8})()
        second = type('Stat', (), {'st_dev': 1, 'st_ino': 2**64 - 20, 'st_size': 15, 'st_mtime_ns': 7, 'st_ctime_ns': 8})()
        self.assertEqual(module.identity(first), module.identity(second))

    def test_appledouble_journal_metadata_is_not_sqlite_journal(self) -> None:
        self.add()
        metadata = self.db.parent / '._m.db-journal'
        metadata.write_bytes(bytes.fromhex('0005160700020000') + bytes(4088))
        module = self.module()
        self.assertEqual(module.check_sidecars(self.root), {})

    def test_empty_process_evidence_refuses(self) -> None:
        module = self.module()
        with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            with self.assertRaises(module.SafetyError):
                module.check_running_apps(self.root)

    def test_missing_probe_keeps_exact_matches(self) -> None:
        self.add()
        self.add(source='Music/unmatched.mp3', target='unmatched.mp3', data=b'other', track_id=2)[1].write_bytes(b'different')
        module = self.module()
        with patch.object(module.shutil, 'which', return_value=None):
            plan = module.preview(self.root, match_metadata=True)
        self.addCleanup(plan.close)
        self.assertEqual(len(plan.matches), 1)
        self.assertEqual(plan.counts['unmatched'], 1)

    def test_process_detection_failure_refuses_mutation(self) -> None:
        module = self.module()
        with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'denied')):
            with self.assertRaises(module.SafetyError):
                module.check_running_apps(self.root)

    def test_inactive_journal_timestamp_change_is_not_transaction_change(self) -> None:
        self.add()
        journal = Path(str(self.db) + '-journal')
        journal.write_bytes(b'')
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        os.utime(journal, ns=(1, 1))
        self.assertEqual(module.apply_plan(plan, self.backups), 0)

    def test_metadata_match_retains_nonidentical_audio(self) -> None:
        src, dst = self.add()
        dst.write_bytes(b'different export tags and bytes')
        module = self.module()
        metadata = ('artist', 'title (extended mix)', 200.0, 'mp3', 44100, 2)
        with patch.object(module, 'probe_metadata', return_value=metadata):
            plan = module.preview(self.root, match_metadata=True)
        self.addCleanup(plan.close)
        self.assertEqual(len(plan.matches), 1)
        self.assertEqual(plan.matches[0].method, 'metadata')
        self.assertEqual(plan.removable_bytes, 0)
        self.assertEqual(module.apply_plan(plan, self.backups, True), 0)
        self.assertTrue(src.exists())
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        module.restore_audio(self.root, manifest)
        self.assertEqual(src.read_bytes(), b'synthetic audio')

    def test_metadata_ambiguity_is_not_resolved_by_path_order(self) -> None:
        src, dst = self.add()
        dst.write_bytes(b'export version')
        (self.root / 'Contents/another.mp3').write_bytes(b'another version')
        module = self.module()
        with patch.object(module, 'probe_metadata', return_value=('artist', 'title', 200.0, 'mp3', 44100, 2)):
            plan = module.preview(self.root, match_metadata=True)
        self.addCleanup(plan.close)
        self.assertEqual(len(plan.matches), 0)
        self.assertEqual(plan.counts['conflicts'], 1)

    def test_metadata_duration_or_version_mismatch_is_not_matched(self) -> None:
        src, dst = self.add()
        dst.write_bytes(b'export version')
        module = self.module()
        for candidate in (('artist', 'title (radio edit)', 200.0, 'mp3', 44100, 2), ('artist', 'title', 210.0, 'mp3', 44100, 2), None):
            def metadata(root, relative):
                return ('artist', 'title', 200.0, 'mp3', 44100, 2) if relative.startswith('Engine Library/') else candidate
            with self.subTest(candidate=candidate), patch.object(module, 'probe_metadata', side_effect=metadata):
                plan = module.preview(self.root, match_metadata=True)
                self.assertEqual(len(plan.matches), 0)
                plan.close()

    def test_metadata_destination_changed_after_probe_is_rejected(self) -> None:
        src, dst = self.add()
        dst.write_bytes(b'export tags')
        module = self.module()
        def metadata(root, relative):
            if relative.startswith('Engine Library/'):
                dst.write_bytes(b'changed after metadata indexing')
            return ('artist', 'title', 200.0, 'mp3', 44100, 2)
        with patch.object(module, 'probe_metadata', side_effect=metadata):
            with self.assertRaises(module.SafetyError):
                module.preview(self.root, match_metadata=True)

    def test_probe_metadata_parses_tags_without_shell(self) -> None:
        src, dst = self.add()
        module = self.module()
        result = subprocess.CompletedProcess([], 0, json.dumps({'format': {'duration': '200.1', 'tags': {'ARTIST': '  ARTIST ', 'title': 'Title (Extended Mix)'}}, 'streams': [{'codec_type': 'audio', 'codec_name': 'mp3', 'sample_rate': '44100', 'channels': 2}]}), '')
        with patch.object(module.shutil, 'which', return_value='/usr/bin/ffprobe'), patch.object(module.subprocess, 'run', return_value=result) as runner:
            self.assertEqual(module.probe_metadata(self.root, 'Contents/Artist/target.mp3'), ('artist', 'title (extended mix)', 200.1, 'mp3', 44100, 2))
            self.assertNotIn('shell', runner.call_args.kwargs)

    def test_metadata_preview_writes_nothing(self) -> None:
        src, dst = self.add()
        dst.write_bytes(b'other export')
        module = self.module()
        before = self.tree()
        with patch.object(module, 'probe_metadata', return_value=('artist', 'title', 200.0, 'mp3', 44100, 2)):
            plan = module.preview(self.root, match_metadata=True)
        self.addCleanup(plan.close)
        self.assertEqual(self.tree(), before)

    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file(), 'The requested script is not implemented')

    def test_preview_writes_nothing(self) -> None:
        self.add()
        before = self.tree()
        result = self.run_cli('--dry-run', '--verbose')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Proposed updates: 1', result.stdout)
        self.assertEqual(before, self.tree())

    def test_apply_preserves_non_path_values(self) -> None:
        src, dst = self.add()
        assets = self.tree()
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(src.exists())
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT * FROM Track').fetchone(), (1, '../Contents/Artist/target.mp3', 'origin', 1, b'cues and grids'))
            self.assertEqual(conn.execute('SELECT * FROM Playlist').fetchall(), [(1, 1)])
        for name, data in assets.items():
            if name != 'Engine Library/Database2/m.db':
                self.assertEqual((self.root / name).read_bytes(), data)
        self.assertEqual(len(list(self.backups.glob('*/m.db'))), 1)

    def test_delete_then_recover_audio(self) -> None:
        src, dst = self.add()
        result = self.apply(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(src.exists())
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        result = self.run_cli('--restore-audio', str(manifest))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(src.read_bytes(), dst.read_bytes())
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT path FROM Track').fetchone()[0], '../Contents/Artist/target.mp3')

    def test_idempotent_second_run(self) -> None:
        self.add()
        self.assertEqual(self.apply(True).returncode, 0)
        result = self.run_cli('--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Already linked: 1', result.stdout)
        self.assertIn('Proposed updates: 0', result.stdout)

    def test_middle_difference_is_not_match(self) -> None:
        src, dst = self.add(data=b'A' * 10000 + b'X' + b'B' * 10000)
        dst.write_bytes(b'A' * 10000 + b'Y' + b'B' * 10000)
        result = self.apply(True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(src.exists())
        self.assertIn('Unmatched: 1', result.stdout)

    def test_missing_source_never_matches(self) -> None:
        src, dst = self.add()
        src.unlink()
        result = self.apply(True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Missing: 1', result.stdout)

    def test_existing_target_conflict(self) -> None:
        src, dst = self.add()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO Track VALUES(2,?,"origin",2,NULL)', ('../Contents/Artist/target.mp3',))
        result = self.apply(True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(src.exists())
        self.assertIn('Conflicts: 1', result.stdout)

    def test_duplicate_targets_choose_free_path(self) -> None:
        self.add(target='a.mp3')
        (self.root / 'Contents/b.mp3').write_bytes(b'synthetic audio')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO Track VALUES(2,"../Contents/a.mp3","origin",2,NULL)')
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT path FROM Track WHERE id=1').fetchone()[0], '../Contents/b.mp3')

    def test_multiple_source_references_keep_audio_if_one_conflicts(self) -> None:
        src, dst = self.add()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('INSERT INTO Track VALUES(2,"music/artist/SOURCE.mp3","origin",2,NULL)')
        result = self.apply(True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue(src.exists())

    def test_unicode_and_case_source_resolution(self) -> None:
        src, dst = self.add(source='Music/CAFÉ/Track.mp3', target='real spelling.mp3')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE Track SET path=?', ('music/cafe\u0301/TRACK.MP3',))
        result = self.apply(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(src.exists())

    def test_unsafe_paths_are_skipped(self) -> None:
        self.add()
        for bad in ('/etc/passwd', 'Music/../../outside.mp3', '../Contents/../outside.mp3', 'file://music.mp3', 'Music/a\\b.mp3'):
            with self.subTest(path=bad), contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE Track SET path=?', (bad,))
            result = self.run_cli('--dry-run')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('Unsafe or unsupported: 1', result.stdout)

    def test_symlinks_rejected(self) -> None:
        src, dst = self.add()
        src.unlink()
        src.symlink_to(dst)
        self.assertEqual(self.run_cli('--dry-run').returncode, 1)

    def test_case_ambiguity_rejected(self) -> None:
        self.add(target='a.mp3')
        module = self.module()
        listing = [(str(self.root / 'Contents'), [], ['a.mp3', 'A.mp3'])]
        with patch.object(module.os, 'walk', return_value=listing):
            with self.assertRaisesRegex(module.SafetyError, 'Ambiguous'):
                module.inventory(self.root, 'Contents')

    def test_journals_rejected_without_writes(self) -> None:
        self.add()
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(str(self.db) + suffix)
            sidecar.write_bytes(b'synthetic unresolved journal')
            before = self.tree()
            result = self.run_cli('--dry-run')
            self.assertEqual(result.returncode, 1)
            self.assertEqual(self.tree(), before)
            sidecar.unlink()

    def test_inactive_journals_allow_zero_write_preview(self) -> None:
        self.add()
        for payload in (b'', bytes(4096)):
            with self.subTest(length=len(payload)):
                for name in ('m.db-journal', 'hm.db-journal', 'stm.db-journal'):
                    (self.db.parent / name).write_bytes(payload)
                before = self.tree()
                result = self.run_cli('--dry-run')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.tree(), before)

    def test_apply_retains_inactive_auxiliary_journals(self) -> None:
        self.add()
        for number, payload in enumerate((b'', bytes(4096)), 2):
            with self.subTest(length=len(payload)):
                self.add(source=f'Music/new{number}.mp3', target=f'new{number}.mp3', data=bytes([number]), track_id=number)
                for name in ('m.db-journal', 'hm.db-journal', 'stm.db-journal'):
                    (self.db.parent / name).write_bytes(payload)
                result = self.apply(True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(Path(str(self.db) + '-journal').exists())
                self.assertEqual((self.db.parent / 'hm.db-journal').read_bytes(), payload)
                self.assertEqual((self.db.parent / 'stm.db-journal').read_bytes(), payload)

    def test_nonzero_journal_header_still_rejected(self) -> None:
        self.add()
        Path(str(self.db) + '-journal').write_bytes(bytes.fromhex('d9d505f920a163d7') + bytes(4096))
        before = self.tree()
        self.assertEqual(self.run_cli('--dry-run').returncode, 1)
        self.assertEqual(self.tree(), before)

    def test_journal_change_since_preview_blocks_apply(self) -> None:
        self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        Path(str(self.db) + '-journal').write_bytes(b'')
        with self.assertRaises(module.SafetyError):
            module.apply_plan(plan, self.backups)

    def test_unsupported_schema_rejected(self) -> None:
        self.add()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE Information SET schemaVersionMajor=99')
        self.assertEqual(self.apply().returncode, 1)

    def test_trigger_side_effect_rolls_back(self) -> None:
        self.add()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript('CREATE TRIGGER evil AFTER UPDATE OF path ON Track BEGIN UPDATE Track SET cues=NULL; END;')
        before = self.db.read_bytes()
        self.assertEqual(self.apply().returncode, 1)
        self.assertEqual(self.db.read_bytes(), before)

    def test_empty_origins_rejected(self) -> None:
        self.add()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE Track SET originDatabaseUuid=""')
        self.assertEqual(self.apply().returncode, 1)

    def test_cleanup_requires_apply(self) -> None:
        self.add()
        self.assertNotEqual(self.run_cli('--delete-duplicates').returncode, 0)

    def test_detected_running_app_blocks_before_backup(self) -> None:
        self.add()
        before = self.tree()
        module = self.module()
        with patch.object(module, 'check_running_apps', side_effect=module.SafetyError('Close Engine DJ')):
            self.assertEqual(module.main([str(self.root)]), 1)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / 'DJ USB Backups').exists())

    def test_volume_selection(self) -> None:
        module = self.module()
        volumes = Path(self.temp.name) / 'Volumes'
        volumes.mkdir()
        (volumes / 'First').mkdir()
        (volumes / 'Second').mkdir()
        with patch('builtins.input', return_value='2'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.select_volume(volumes), volumes / 'Second')
        with patch('builtins.input', return_value='q'):
            with self.assertRaises(module.SafetyError):
                module.select_volume(volumes)

    def test_low_space_blocks_mutation(self) -> None:
        self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        with patch.object(module.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaises(module.SafetyError):
                module.apply_plan(plan, self.backups, False)
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_changed_destination_blocks_commit(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        dst.write_bytes(b'changed content')
        with self.assertRaises(module.SafetyError):
            module.apply_plan(plan, self.backups, True)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT path FROM Track').fetchone()[0], 'Music/Artist/source.mp3')
        self.assertTrue(src.exists())

    def test_concurrent_lock_blocks_apply(self) -> None:
        self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        with module.tool_lock(self.root):
            with self.assertRaises(module.SafetyError):
                module.apply_plan(plan, self.backups, False)

    def test_partial_deletion_keeps_updated_database(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        def failed(*args) -> None:
            raise OSError('synthetic deletion failure')
        with patch.object(module, 'unlink_source', side_effect=failed):
            result = module.apply_plan(plan, self.backups, True)
        self.assertEqual(result, 2)
        self.assertTrue(src.exists())
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertTrue(conn.execute('SELECT path FROM Track').fetchone()[0].startswith('../Contents/'))

    def test_recovery_rejects_wrong_music_prefix(self) -> None:
        self.add()
        module = self.module()
        manifest = self.backups / 'unsafe.jsonl'
        manifest.write_text(json.dumps({'event': 'prepared', 'format': 1, 'changes': [{'source': 'Music/outside.mp3', 'destination': 'Contents/Artist/target.mp3', 'sha256': hashlib.sha256(b'synthetic audio').hexdigest(), 'bytes': len(b'synthetic audio')}]}) + '\n')
        with self.assertRaises(module.SafetyError):
            module.restore_audio(self.root, manifest)
        self.assertFalse((self.root / 'Music').exists())

    def test_log_failure_after_commit_reports_committed_state(self) -> None:
        self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        original = module.append_record
        def failing(stream, record) -> None:
            if record['event'] != 'prepared':
                raise OSError('synthetic manifest disk failure')
            original(stream, record)
        with patch.object(module, 'append_record', side_effect=failing):
            with self.assertRaisesRegex(module.SafetyError, 'Paths committed'):
                module.apply_plan(plan, self.backups, True)

    def test_interruption_before_commit_preserves_source(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        with patch.object(module, 'verify_match', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                module.apply_plan(plan, self.backups, True)
        self.assertTrue(src.exists())
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT path FROM Track').fetchone()[0], 'Music/Artist/source.mp3')

    def test_changed_source_before_delete_retains_audio(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        original = module.append_record
        def change_after_commit(stream, record) -> None:
            original(stream, record)
            if record['event'] == 'committed':
                src.write_bytes(b'changed source')
        with patch.object(module, 'append_record', side_effect=change_after_commit):
            self.assertEqual(module.apply_plan(plan, self.backups, True), 2)
        self.assertEqual(src.read_bytes(), b'changed source')

    def test_backup_failure_preserves_database(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        before = self.db.read_bytes()
        original = module.os.open
        def fail_backup(path, *args, **kwargs):
            if isinstance(path, Path) and path.name == 'm.db' and path.parent.parent == self.backups:
                raise OSError('synthetic backup failure')
            return original(path, *args, **kwargs)
        with patch.object(module.os, 'open', side_effect=fail_backup):
            with self.assertRaises(OSError):
                module.apply_plan(plan, self.backups, True)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(src.exists())

    def test_full_recovery_restores_database_after_audio(self) -> None:
        src, dst = self.add()
        before = self.tree()
        self.assertEqual(self.apply(True).returncode, 0)
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        self.assertEqual(self.run_cli('--restore-audio', str(manifest)).returncode, 0)
        with contextlib.closing(sqlite3.connect(manifest.parent / 'm.db')) as backup, contextlib.closing(sqlite3.connect(self.db)) as live:
            backup.backup(live)
        for name, data in before.items():
            if name != 'Engine Library/Database2/m.db':
                self.assertEqual((self.root / name).read_bytes(), data)
        module = self.module()
        with contextlib.closing(sqlite3.connect(manifest.parent / 'm.db')) as backup, contextlib.closing(sqlite3.connect(self.db)) as live:
            self.assertEqual(module.logical_state(live), module.logical_state(backup))

    def test_interruption_after_delete_can_recover(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        original = module.append_record
        def interrupt(stream, record) -> None:
            if record['event'] == 'deleted':
                raise KeyboardInterrupt
            original(stream, record)
        with patch.object(module, 'append_record', side_effect=interrupt):
            with self.assertRaisesRegex(module.SafetyError, 'Paths committed'):
                module.apply_plan(plan, self.backups, True)
        self.assertFalse(src.exists())
        manifest = next(self.backups.glob('*/manifest.jsonl'))
        module.restore_audio(self.root, manifest)
        self.assertEqual(src.read_bytes(), dst.read_bytes())

    def test_changed_database_rejected(self) -> None:
        self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE Track SET cues=?', (b'new cues',))
        with self.assertRaises(module.SafetyError):
            module.apply_plan(plan, self.backups, True)

    def test_directory_sync_failure_blocks_commit(self) -> None:
        src, dst = self.add()
        module = self.module()
        plan = module.preview(self.root)
        self.addCleanup(plan.close)
        with patch.object(module, 'sync_directory', side_effect=module.SafetyError('synthetic sync failure')):
            with self.assertRaises(module.SafetyError):
                module.apply_plan(plan, self.backups, True)
        self.assertTrue(src.exists())
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT path FROM Track').fetchone()[0], 'Music/Artist/source.mp3')

    def test_unknown_files_preserved(self) -> None:
        self.add()
        unknown = self.root / 'Engine Library/Music/unknown.bin'
        unknown.write_bytes(b'unknown')
        self.assertEqual(self.apply(True).returncode, 0)
        self.assertEqual(unknown.read_bytes(), b'unknown')


if __name__ == '__main__':
    unittest.main()
