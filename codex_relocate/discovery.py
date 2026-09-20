"""Read-only candidates; no scanning of chat contents or authentication files."""
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import shutil

from .engine import exists, is_link, full, snapshot, summary


def documents_folder():
    # SHGetFolderPath respects redirected Documents (including OneDrive).
    buf = c.create_unicode_buffer(32768)
    shell = c.WinDLL('shell32')
    shell.SHGetFolderPathW.argtypes = [w.HWND, c.c_int, w.HANDLE, w.DWORD, w.LPWSTR]
    if shell.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
        return Path(buf.value)
    return Path.home() / 'Documents'


def candidates():
    docs = documents_folder()
    raw = [('Documents', docs / 'ChatGPT'), ('Documents', docs / 'Codex'),
           ('Application state', full(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))),
           ('Runtime cache', Path.home() / '.cache' / 'codex-runtimes')]
    seen, out = set(), []
    for category, path in raw:
        if not exists(path) or str(path).lower() in seen:
            continue
        seen.add(str(path).lower())
        linked = is_link(path)
        out.append({'category': category, 'path': str(path), 'already_linked': linked,
                    'physical_path': str(path.resolve())})
    return out


def scan(path):
    path = full(path)
    if is_link(path):
        return {'path': str(path), 'already_linked': True, 'physical_path': str(path.resolve()),
                'note': 'This entry is a link. Its target is not counted as source-drive usage.'}
    return {'path': str(path), 'already_linked': False, **summary(snapshot(path, hashes=False)),
            'free': shutil.disk_usage(path.anchor).free}
