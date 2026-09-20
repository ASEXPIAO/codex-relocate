import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_relocate import windows as win
from codex_relocate.engine import Engine, MigrationError, content, snapshot, exists, is_link


@unittest.skipUnless(os.name == 'nt', 'Windows filesystem integration tests')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relocate-test-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.dest = self.root / 'destination'
        self.engine = Engine(self.root / 'jobs')
        (self.source / 'hello.txt').write_text('hello', encoding='utf-8')

    def tearDown(self):
        # TemporaryDirectory is confined to its fixture root; Python >=3.8 does
        # not traverse Windows junctions when removing directory trees.
        self.temp.cleanup()

    def move(self):
        return self.engine.migrate(self.source, self.dest)

    def test_move_release_preserves_external_junction_and_named_stream(self):
        external = self.root / 'external'
        external.mkdir()
        (external / 'keep.txt').write_text('untouched')
        win.make_junction(self.source / 'external-link', external)
        (self.source / '空目录').mkdir()
        (self.source / '中文 😀.txt').write_text('数据', encoding='utf-8')
        with open(str(self.source / 'hello.txt') + ':metadata', 'wb') as f:
            f.write(b'named-stream')
        expected = content(snapshot(self.source))
        job = self.move()
        self.assertEqual(job['phase'], 'migrated')
        self.assertTrue(Path(job['backup']).exists())
        self.assertTrue(is_link(self.source))
        self.assertEqual(content(snapshot(self.dest)), expected)
        self.assertEqual((self.source / '中文 😀.txt').read_text(encoding='utf-8'), '数据')
        self.assertEqual(self.engine.release(job['id'])['phase'], 'released')
        self.assertFalse(exists(job['backup']))
        self.assertEqual((external / 'keep.txt').read_text(), 'untouched')
        self.assertEqual(self.engine.release(job['id'])['phase'], 'released')

    def test_existing_destination_and_overlapping_paths_refused(self):
        self.dest.mkdir()
        (self.dest / 'precious').write_text('keep')
        with self.assertRaises(MigrationError):
            self.move()
        self.assertEqual((self.dest / 'precious').read_text(), 'keep')
        with self.assertRaises(MigrationError):
            self.engine.migrate(self.source, self.source / 'nested')

    def test_changed_destination_blocks_release_and_rollback(self):
        job = self.move()
        (self.dest / 'hello.txt').write_text('new version')
        for op in (self.engine.release, self.engine.rollback):
            with self.assertRaises(MigrationError):
                op(job['id'])
        self.assertEqual((Path(job['backup']) / 'hello.txt').read_text(), 'hello')
        self.assertEqual((self.source / 'hello.txt').read_text(), 'new version')

    def test_added_staged_file_blocks_release(self):
        job = self.move()
        (Path(job['backup']) / 'unexpected').write_text('keep me')
        with self.assertRaises(MigrationError):
            self.engine.release(job['id'])
        self.assertTrue((Path(job['backup']) / 'unexpected').exists())

    def test_rollback_retains_destination(self):
        job = self.move()
        self.assertEqual(self.engine.rollback(job['id'])['phase'], 'rolled_back')
        self.assertFalse(is_link(self.source))
        self.assertEqual((self.dest / 'hello.txt').read_text(), 'hello')

    def test_junction_failure_restores_source_and_can_resume(self):
        with patch('codex_relocate.windows.make_junction', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                self.move()
        self.assertFalse(is_link(self.source))
        self.assertEqual((self.source / 'hello.txt').read_text(), 'hello')
        job = self.engine.jobs()[0]
        self.assertEqual(self.engine.resume(job['id'])['phase'], 'migrated')

    def test_interrupted_copy_between_files_resumes(self):
        (self.source / 'second.txt').write_text('second')
        original = win.copy_file
        calls = [0]
        def interrupted(s, d, **kwargs):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError('interrupted')
            return original(s, d, **kwargs)
        with patch('codex_relocate.windows.copy_file', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.move()
        job = self.engine.jobs()[0]
        self.assertEqual(job['phase'], 'copying')
        self.assertEqual(self.engine.resume(job['id'])['phase'], 'migrated')

    def test_owned_partial_copy_recovers_without_touching_source(self):
        def partial(s, d, **kwargs):
            Path(d).write_bytes(b'partial')
            raise OSError('power loss')
        with patch('codex_relocate.windows.copy_file', side_effect=partial):
            with self.assertRaises(OSError):
                self.move()
        self.assertFalse((self.dest / 'hello.txt').exists())
        self.assertEqual((self.source / 'hello.txt').read_text(), 'hello')
        job = self.engine.jobs()[0]
        self.assertEqual(self.engine.resume(job['id'])['phase'], 'migrated')
        self.assertEqual((self.dest / 'hello.txt').read_text(), 'hello')

    def test_release_interruption_resumes_without_touching_target(self):
        (self.source / 'second.txt').write_text('second')
        job = self.move()
        original = os.unlink
        calls = [0]
        def interrupted(path, *a, **kw):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError('simulated interruption')
            return original(path, *a, **kw)
        with patch('codex_relocate.engine.os.unlink', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.engine.release(job['id'])
        self.assertEqual(self.engine.load(job['id'])['phase'], 'releasing')
        self.assertEqual(self.engine.release(job['id'])['phase'], 'released')
        self.assertEqual((self.dest / 'second.txt').read_text(), 'second')

    def test_destination_replaced_by_junction_blocks_release(self):
        job = self.move()
        other = self.root / 'other'
        os.rename(self.dest, other)
        win.make_junction(self.dest, other)
        with self.assertRaises(MigrationError):
            self.engine.release(job['id'])
        self.assertTrue(Path(job['backup']).exists())

    def test_lock_prevents_concurrent_engine(self):
        with self.engine.locked():
            with self.assertRaises(MigrationError):
                self.move()

    def test_relative_escape_link_rejected(self):
        try:
            os.symlink('..\\outside', self.source / 'escape', target_is_directory=True)
        except OSError:
            self.skipTest('Developer Mode / symlink privilege unavailable')
        with self.assertRaises(MigrationError):
            self.move()

    def test_open_directory_handle_blocks_rename_without_data_loss(self):
        k = win.kernel()
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.CreateFileW(str(self.source), 0x80000000, 3, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, c.c_void_p(-1).value)
        try:
            with self.assertRaises(MigrationError):
                self.move()
            self.assertEqual((self.source / 'hello.txt').read_text(), 'hello')
        finally:
            k.CloseHandle(handle)
        job = self.engine.jobs()[0]
        self.assertEqual(self.engine.resume(job['id'])['phase'], 'migrated')

    def test_long_path_and_internal_junction(self):
        p = self.source
        for _ in range(6):
            p = p / ('nested-' + 'x' * 40)
            os.mkdir(win.extended(p))
        with open(win.extended(p / 'data.txt'), 'w') as f:
            f.write('long path')
        win.make_junction(self.source / 'internal', p)
        job = self.move()
        self.engine.release(job['id'])
        with open(win.extended(self.source / 'internal' / 'data.txt')) as f:
            self.assertEqual(f.read(), 'long path')

    def test_persistent_sandbox_service_is_not_app(self):
        with patch('codex_relocate.windows.processes', return_value={1: 'codex-windows-sandbox-service.exe', 2: 'codex.exe'}):
            self.assertEqual(win.app_processes(), [{'pid': 2, 'name': 'codex.exe'}])

    def test_source_mutation_during_copy_prevents_switch(self):
        original = win.copy_file
        def mutate(s, d, **kwargs):
            original(s, d, **kwargs)
            Path(s).write_text('changed while copying')
        with patch('codex_relocate.windows.copy_file', side_effect=mutate):
            with self.assertRaises(MigrationError):
                self.move()
        self.assertFalse(is_link(self.source))
        self.assertEqual((self.source / 'hello.txt').read_text(), 'changed while copying')

    def test_corrupted_copy_prevents_switch(self):
        def corrupt(s, d, **kwargs):
            Path(d).write_text('wrong content')
        with patch('codex_relocate.windows.copy_file', side_effect=corrupt):
            with self.assertRaises(MigrationError):
                self.move()
        self.assertFalse(is_link(self.source))
        self.assertEqual((self.source / 'hello.txt').read_text(), 'hello')

    def test_changed_stream_blocks_release(self):
        job = self.move()
        with open(str(self.dest / 'hello.txt') + ':new-stream', 'w') as f:
            f.write('new data')
        with self.assertRaises(MigrationError):
            self.engine.release(job['id'])
        self.assertTrue(Path(job['backup']).exists())

    def test_resume_after_crash_between_rename_and_junction(self):
        with patch('codex_relocate.windows.make_junction', side_effect=OSError('injected')):
            with self.assertRaises(OSError):
                self.move()
        job = self.engine.jobs()[0]
        os.rename(self.source, job['backup'])
        self.assertEqual(self.engine.resume(job['id'])['phase'], 'migrated')

    def test_resume_interrupted_rollback(self):
        job = self.move()
        self.engine.save(job, 'rollback_pending')
        os.rmdir(self.source)
        self.assertEqual(self.engine.resume(job['id'])['phase'], 'rolled_back')
        self.assertEqual((self.source / 'hello.txt').read_text(), 'hello')

    def test_insufficient_space_leaves_source_untouched(self):
        usage = __import__('shutil').disk_usage(self.root)
        with patch('codex_relocate.engine.shutil.disk_usage', return_value=type(usage)(100, 99, 1)):
            with self.assertRaises(MigrationError):
                self.move()
        self.assertFalse(self.dest.exists())

    def test_link_ancestor_refused(self):
        alias = self.root / 'alias'
        win.make_junction(alias, self.source)
        with self.assertRaises(MigrationError):
            self.engine.migrate(alias, self.dest)

    def test_directory_named_stream_refused(self):
        with open(str(self.source) + ':metadata', 'w') as f:
            f.write('directory stream')
        with self.assertRaises(MigrationError):
            self.move()

    def test_readonly_file_release(self):
        import stat
        os.chmod(self.source / 'hello.txt', stat.S_IREAD)
        job = self.move()
        self.assertEqual(self.engine.release(job['id'])['phase'], 'released')
        self.assertEqual((self.source / 'hello.txt').read_text(), 'hello')

    def test_diagnostic_identifies_our_open_directory(self):
        k = win.kernel()
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.CreateFileW(str(self.source), 0x80000000, 3, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, c.c_void_p(-1).value)
        try:
            result = win.diagnose(self.source)
            self.assertFalse(result.get('incomplete'), result)
            self.assertTrue(any(h['pid'] == os.getpid() for h in result['handles']), result)
        finally:
            k.CloseHandle(handle)

    def test_diagnostic_timeout_is_explicitly_incomplete(self):
        import subprocess
        with patch('codex_relocate.windows.subprocess.run', side_effect=subprocess.TimeoutExpired('probe', 20)):
            self.assertTrue(win.diagnose(self.source)['incomplete'])


if __name__ == '__main__':
    unittest.main()
