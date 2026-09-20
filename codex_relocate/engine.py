"""A journaled copy / verify / switch / explicit-release transaction."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import uuid

from . import windows as win

REPARSE = 0x400
JUNCTION = 0xA0000003
SYMLINK = 0xA000000C


class MigrationError(RuntimeError):
    pass


def full(path):
    value = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))
    return win.long_path(value)


def inside(path, parent):
    try:
        return os.path.commonpath([str(full(path)), str(full(parent))]).lower() == str(full(parent)).lower()
    except ValueError:
        return False


def exists(path):
    return os.path.lexists(win.extended(path))


def attrs(path):
    return os.lstat(win.extended(path))


def is_link(path):
    return bool(attrs(path).st_file_attributes & REPARSE)


def identity(path):
    s = attrs(path)
    return [s.st_dev, s.st_ino]


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def digest(path):
    h = hashlib.sha256()
    with open(win.extended(path), 'rb') as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def snapshot(root, hashes=True, progress=lambda message: None):
    """Enumerate links as entries, never as trees; fail on inaccessible data."""
    root = full(root)
    if is_link(root):
        raise MigrationError('Root is already a link; inspect its real location first.')
    result = {}
    stack = [Path(win.extended(root))]
    base = stack[0]
    count = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as iterator:
            entries = list(iterator)
        for e in entries:
            p = Path(e.path)
            rel = str(p.relative_to(base))
            s = os.lstat(p)
            if s.st_file_attributes & REPARSE:
                tag = s.st_reparse_tag
                if tag not in (JUNCTION, SYMLINK):
                    raise MigrationError('Unsupported reparse point (cloud placeholder or mount): ' + rel)
                target = os.readlink(p)
                if tag == SYMLINK and not os.path.isabs(target):
                    resolved = full(root / rel).parent / target
                    if not inside(full(resolved), root):
                        raise MigrationError('Relative link escapes source; relocate it manually: ' + rel)
                result[rel] = {'kind': 'link', 'tag': tag, 'target': target,
                               'directory': bool(s.st_file_attributes & 0x10)}
            elif stat.S_ISDIR(s.st_mode):
                # Directory ADS are uncommon; fail rather than silently drop them.
                if win.streams(p):
                    raise MigrationError('Directory has named streams; not supported: ' + rel)
                result[rel] = {'kind': 'dir'}
                stack.append(p)
            elif stat.S_ISREG(s.st_mode):
                if s.st_file_attributes & 0x4000:
                    raise MigrationError('EFS-encrypted files are not supported: ' + rel)
                item = {'kind': 'file', 'size': s.st_size, 'mtime_ns': s.st_mtime_ns,
                        'readonly': bool(s.st_file_attributes & 1), 'streams': {}}
                if hashes:
                    item['sha256'] = digest(p)
                for name, size in win.streams(p):
                    item['streams'][name] = {'size': size}
                    if hashes:
                        item['streams'][name]['sha256'] = digest(str(p) + name)
                # Detect writes during hashing. External writers must still be closed.
                after = os.stat(p, follow_symlinks=False)
                if (after.st_size, after.st_mtime_ns, after.st_ino) != (s.st_size, s.st_mtime_ns, s.st_ino):
                    raise MigrationError('File changed while reading: ' + rel)
                result[rel] = item
                count += 1
                if count % 250 == 0:
                    progress('Verified {} files…'.format(count))
            else:
                raise MigrationError('Unsupported filesystem entry: ' + rel)
    if win.streams(root):
        raise MigrationError('Root directory has named streams; not supported.')
    return result


def content(manifest):
    # Timestamps are used for source stability, not as a substitute for hashes.
    return {p: {k: v for k, v in item.items() if k not in ('mtime_ns', 'readonly')}
            for p, item in manifest.items()}


def summary(manifest):
    files = [x for x in manifest.values() if x['kind'] == 'file']
    return {'files': len(files), 'links': sum(x['kind'] == 'link' for x in manifest.values()),
            'bytes': sum(x['size'] + sum(s['size'] for s in x['streams'].values()) for x in files)}


def no_link_ancestors(path):
    for p in [full(path)] + list(full(path).parents):
        if exists(p) and is_link(p):
            raise MigrationError('Path crosses an existing link; use its physical location: ' + str(p))


def requires_app_exit(source):
    parts = {p.lower() for p in full(source).parts}
    configured = os.environ.get('CODEX_HOME')
    return bool(parts & {'.codex', 'chatgpt', 'codex', 'codexdata', 'codex-runtimes'}) or bool(
        configured and inside(source, configured))


def check_apps(source):
    if requires_app_exit(source):
        running = win.app_processes()
        if running:
            raise MigrationError('Close ChatGPT/Codex and run this tool independently. Running: ' +
                                 ', '.join('{} ({})'.format(p['name'], p['pid']) for p in running))


def validate_paths(source, destination, state_dir):
    source, destination = full(source), full(destination)
    if not exists(source) or not Path(win.extended(source)).is_dir():
        raise MigrationError('Source folder does not exist.')
    no_link_ancestors(source)
    no_link_ancestors(destination)
    if exists(destination):
        raise MigrationError('Destination must be a NEW folder. Existing data will not be overwritten.')
    if not destination.parent.is_dir():
        raise MigrationError('Create/select an existing destination parent folder first.')
    if inside(source, destination) or inside(destination, source):
        raise MigrationError('Source and destination must not overlap.')
    sv, dv = win.volume(source), win.volume(destination)
    if any(v['filesystem'] != 'NTFS' or not v['fixed'] for v in (sv, dv)):
        raise MigrationError('Both locations must be on fixed local NTFS volumes.')
    # Same-volume moves are useful in tests but not advertised as freeing a drive.
    forbidden = [Path(source.anchor), Path.home(), full(os.environ.get('WINDIR', r'C:\Windows')),
                 full(os.environ.get('ProgramFiles', r'C:\Program Files')),
                 full(os.environ.get('ProgramData', r'C:\ProgramData'))]
    for p in forbidden:
        if source == p or destination == p or inside(p, source):
            raise MigrationError('Refusing a drive root, user root, or system ancestor.')
    for name in ('WINDIR', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData'):
        if os.environ.get(name) and (inside(source, os.environ[name]) or inside(destination, os.environ[name])):
            raise MigrationError('System/application installation directories are not supported.')
    for protected in (full(state_dir), Path.cwd(), full(sys.executable)):
        if inside(protected, source) or inside(protected, destination):
            raise MigrationError('Tool, working directory, or journal is inside the selected tree.')
    check_apps(source)
    return source, destination


class Engine:
    def __init__(self, state_dir=None, progress=None):
        self.state_dir = full(state_dir or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CodexRelocate' / 'jobs')
        self.state_dir.mkdir(parents=True, exist_ok=True)
        no_link_ancestors(self.state_dir)
        self.progress = progress or (lambda message: None)

    @contextmanager
    def locked(self):
        import msvcrt
        with (self.state_dir / '.lock').open('a+b') as f:
            if os.fstat(f.fileno()).st_size == 0:
                f.write(b'0')
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MigrationError('Another migration is running.') from exc
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    def load(self, job_id):
        if len(job_id) != 32 or any(c not in '0123456789abcdef' for c in job_id):
            raise MigrationError('Invalid job ID.')
        j = json.loads((self.state_dir / (job_id + '.json')).read_text(encoding='utf-8'))
        j['manifest'] = json.loads((self.state_dir / (job_id + '.manifest')).read_text(encoding='utf-8'))
        if j['id'] != job_id or j['schema'] != 1:
            raise MigrationError('Unknown journal schema.')
        s, d, b = (full(j[k]) for k in ('source', 'destination', 'backup'))
        if b != s.with_name(s.name + '.relocate-' + job_id) or inside(s, d) or inside(d, s):
            raise MigrationError('Invalid journal paths.')
        return j

    def save(self, job, phase=None):
        if phase:
            job['phase'] = phase
        job['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        manifest_path = self.state_dir / (job['id'] + '.manifest')
        if not manifest_path.exists():
            atomic_json(manifest_path, job['manifest'])
        atomic_json(self.state_dir / (job['id'] + '.json'), {k: v for k, v in job.items() if k != 'manifest'})

    def jobs(self):
        return sorted([self.load(p.stem) for p in self.state_dir.glob('*.json')],
                      key=lambda x: x['updated_at'], reverse=True)

    def migrate(self, source, destination):
        with self.locked():
            source, destination = validate_paths(source, destination, self.state_dir)
            self.progress('Hashing source (including named streams)…')
            manifest = snapshot(source, progress=self.progress)
            size = summary(manifest)['bytes']
            if shutil.disk_usage(destination.parent).free < size + max(64 * 1024 * 1024, size // 100):
                raise MigrationError('Not enough destination space (includes a safety margin).')
            job_id = uuid.uuid4().hex
            job = {'schema': 1, 'id': job_id, 'source': str(source), 'destination': str(destination),
                   'backup': str(source.with_name(source.name + '.relocate-' + job_id)),
                   'source_identity': identity(source), 'manifest': manifest, 'summary': summary(manifest),
                   'free_before': shutil.disk_usage(source.anchor).free}
            self.save(job, 'planned')
            return self._resume(job)

    def resume(self, job_id):
        with self.locked():
            return self._resume(self.load(job_id))

    def _assert_directory(self, path, expected):
        no_link_ancestors(path)
        if not exists(path) or is_link(path) or identity(path) != expected:
            raise MigrationError('Directory identity changed; original data retained: ' + str(path))

    def _resume(self, job):
        s, d, b = (full(job[k]) for k in ('source', 'destination', 'backup'))
        check_apps(s)
        if job['phase'] in ('migrated', 'released', 'rolled_back', 'releasing'):
            return job
        if job['phase'] == 'rollback_pending':
            return self._rollback(job)
        if job['phase'] == 'planned':
            self._assert_directory(s, job['source_identity'])
            if exists(d):
                raise MigrationError('Destination appeared after planning; no data overwritten.')
            os.mkdir(win.extended(d))
            job['destination_identity'] = identity(d)
            self.save(job, 'copying')
        self._assert_directory(d, job['destination_identity'])
        if job['phase'] == 'copying':
            self._assert_directory(s, job['source_identity'])
            win.copy_acl(s, d)
            if snapshot(s, progress=self.progress) != job['manifest']:
                raise MigrationError('Source changed. Keep this job as evidence and start with a new destination.')
            self._recover_partial(job, d)
            self.progress('Copying; source remains untouched…')
            # A file becomes visible at its final name only after CopyFile and
            # content verification succeed. Owned partial copies are resumable.
            for rel, item in sorted(job['manifest'].items(), key=lambda kv: (len(Path(kv[0]).parts), kv[0])):
                src, dst = s / rel, d / rel
                if exists(dst):
                    if item['kind'] == 'dir' and not is_link(dst) and Path(win.extended(dst)).is_dir():
                        continue
                    if item['kind'] == 'link' and is_link(dst) and os.readlink(win.extended(dst)) == item['target']:
                        continue
                    if item['kind'] == 'file' and not is_link(dst) and digest(dst) == item['sha256']:
                        continue
                    raise MigrationError('Incomplete/conflicting destination entry retained: ' + rel)
                if item['kind'] == 'dir':
                    os.mkdir(win.extended(dst))
                    win.copy_acl(src, dst)
                elif item['kind'] == 'link':
                    if item['tag'] == JUNCTION:
                        target = item['target']
                        if target.startswith('\\\\?\\'):
                            target = target[4:]
                        win.make_junction(dst, target)
                    else:
                        os.symlink(item['target'], win.extended(dst), target_is_directory=item['directory'])
                else:
                    partial = d.parent / ('.relocate-part-' + job['id'])
                    with open(win.extended(partial), 'xb'):
                        pass
                    job['partial_identity'] = identity(partial)
                    self.save(job)
                    # The temporary sibling must not inherit broad target-parent
                    # access while it holds private application state.
                    win.copy_acl(src, partial)
                    win.copy_file(src, partial, overwrite=True)
                    if digest(partial) != item['sha256']:
                        raise MigrationError('Copied file hash mismatch; original retained: ' + rel)
                    os.rename(win.extended(partial), win.extended(dst))
                    job.pop('partial_identity', None)
                    self.save(job)
            self.progress('Verifying source and destination SHA-256…')
            if snapshot(s, progress=self.progress) != job['manifest']:
                raise MigrationError('Source changed during copy; no switch performed.')
            if content(snapshot(d, progress=self.progress)) != content(job['manifest']):
                raise MigrationError('Destination verification failed; no switch performed.')
            self.save(job, 'switch_pending')
        if job['phase'] == 'switch_pending':
            # Re-verify on every attempt, including after an interrupted switch.
            original = b if exists(b) else s
            self._assert_directory(original, job['source_identity'])
            if content(snapshot(original, progress=self.progress)) != content(job['manifest']):
                raise MigrationError('Original changed before switch.')
            if content(snapshot(d, progress=self.progress)) != content(job['manifest']):
                raise MigrationError('Destination changed before switch.')
            if not exists(b):
                try:
                    os.rename(win.extended(s), win.extended(b))
                except OSError as exc:
                    raise MigrationError('Cannot rename source. Run Diagnose to identify open handles; '
                                         'do not assume administrator rights fix it. ' + str(exc)) from exc
            if not exists(s):
                try:
                    win.make_junction(s, d)
                except BaseException:
                    if not exists(s):
                        os.rename(win.extended(b), win.extended(s))
                    raise
            if not is_link(s) or not os.path.samefile(win.extended(s), win.extended(d)):
                raise MigrationError('Source entry is not the expected junction; original retained at ' + str(b))
            self._write_probe(s, d)
            self.save(job, 'migrated')
            self.progress('Switched. Original retained. Choose Release space after checking your files.')
        return job

    def _recover_partial(self, job, destination):
        partial = destination.parent / ('.relocate-part-' + job['id'])
        if exists(partial):
            if is_link(partial) or identity(partial) != job.get('partial_identity'):
                raise MigrationError('Unknown partial entry; preserved: ' + str(partial))
            os.chmod(win.extended(partial), stat.S_IWRITE | stat.S_IREAD)
            os.unlink(win.extended(partial))
        job.pop('partial_identity', None)
        self.save(job)

    def _write_probe(self, source, destination):
        name = '.relocate-probe-' + uuid.uuid4().hex
        p = source / name
        try:
            with open(win.extended(p), 'xb') as f:
                f.write(b'codex-relocate')
            with open(win.extended(destination / name), 'rb') as f:
                if f.read() != b'codex-relocate':
                    raise MigrationError('Write-through verification failed.')
        finally:
            if exists(p):
                os.unlink(win.extended(p))

    def _check_switched(self, job):
        s, d, b = (full(job[k]) for k in ('source', 'destination', 'backup'))
        self._assert_directory(d, job['destination_identity'])
        if not exists(s) or not is_link(s) or not os.path.samefile(win.extended(s), win.extended(d)):
            raise MigrationError('Expected source junction is missing/changed; no cleanup performed.')
        if exists(b):
            self._assert_directory(b, job['source_identity'])
        return s, d, b

    def release(self, job_id):
        with self.locked():
            job = self.load(job_id)
            if job['phase'] == 'released':
                return job
            if job['phase'] not in ('migrated', 'releasing'):
                raise MigrationError('Only a verified switched job can release its original.')
            check_apps(job['source'])
            s, d, b = self._check_switched(job)
            self.progress('Rechecking destination and retained original before release…')
            if content(snapshot(d, progress=self.progress)) != content(job['manifest']):
                raise MigrationError('Destination changed since migration. Original retained; automatic release refused.')
            remaining = snapshot(b, progress=self.progress) if exists(b) else {}
            baseline = content(job['manifest'])
            if any(baseline.get(p) != v for p, v in content(remaining).items()):
                raise MigrationError('Retained original contains changed/unexpected entries; nothing removed.')
            if job['phase'] == 'migrated' and content(remaining) != baseline:
                raise MigrationError('Original is missing entries; automatic release refused.')
            self.save(job, 'releasing')
            # Every path originates in the verified no-follow inventory. Child
            # entries go first; rmdir on a junction removes only its entry.
            for rel in sorted(remaining, key=lambda p: len(Path(p).parts), reverse=True):
                p = b / rel
                if not inside(p, b) or p == b:
                    raise MigrationError('Cleanup path escaped its staging directory.')
                item = remaining[rel]
                if item['kind'] == 'link':
                    if not is_link(p) or os.readlink(win.extended(p)) != item['target']:
                        raise MigrationError('Link changed during cleanup.')
                    (os.rmdir if item['directory'] else os.unlink)(win.extended(p))
                elif item['kind'] == 'dir':
                    if is_link(p):
                        raise MigrationError('Directory became a link during cleanup.')
                    os.rmdir(win.extended(p))
                else:
                    if is_link(p):
                        raise MigrationError('File became a link during cleanup.')
                    if item['readonly']:
                        os.chmod(win.extended(p), stat.S_IWRITE | stat.S_IREAD)
                    os.unlink(win.extended(p))
            if exists(b):
                os.rmdir(win.extended(b))
            job['free_after'] = shutil.disk_usage(s.anchor).free
            job['observed_free_change'] = job['free_after'] - job['free_before']
            self.save(job, 'released')
            self.progress('Original removed. Report includes actual free-space change (other apps may affect it).')
            return job

    def rollback(self, job_id):
        with self.locked():
            job = self.load(job_id)
            if job['phase'] not in ('migrated', 'rollback_pending'):
                raise MigrationError('Rollback is available only before Release space.')
            check_apps(job['source'])
            return self._rollback(job)

    def _rollback(self, job):
        s, d, b = (full(job[k]) for k in ('source', 'destination', 'backup'))
        self._assert_directory(d, job['destination_identity'])
        original = b if exists(b) else s
        self._assert_directory(original, job['source_identity'])
        expected = content(job['manifest'])
        if content(snapshot(original, progress=self.progress)) != expected or content(snapshot(d, progress=self.progress)) != expected:
            raise MigrationError('Data changed after migration; automatic rollback refused. Both copies retained.')
        if exists(s) and is_link(s):
            if not os.path.samefile(win.extended(s), win.extended(d)):
                raise MigrationError('Source junction changed; rollback refused.')
        elif exists(s) and original != s:
            raise MigrationError('Source path occupied; rollback refused.')
        self.save(job, 'rollback_pending')
        if exists(s) and is_link(s):
            os.rmdir(win.extended(s))
        if exists(b):
            try:
                os.rename(win.extended(b), win.extended(s))
            except BaseException:
                if not exists(s):
                    win.make_junction(s, d)
                raise
        self.save(job, 'rolled_back')
        return job
