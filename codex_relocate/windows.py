"""Small Windows API boundary; no shell-built filesystem commands."""
import ctypes as c
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import struct
import subprocess
import sys


def extended(path):
    s = os.path.abspath(str(path))
    return s if s.startswith('\\\\?\\') else '\\\\?\\' + s


def kernel():
    if os.name != 'nt':
        raise OSError('Windows 10/11 is required / 需要 Windows 10 或 11')
    return c.WinDLL('kernel32', use_last_error=True)


def long_path(path):
    """Expand 8.3 names without resolving junctions; retain nonexistent suffixes."""
    p = Path(os.path.abspath(str(path)))
    tail = []
    k = kernel()
    k.GetLongPathNameW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD]
    buffer = c.create_unicode_buffer(32768)
    while True:
        count = k.GetLongPathNameW(extended(p), buffer, len(buffer))
        if 0 < count < len(buffer):
            value = buffer.value
            if value.startswith('\\\\?\\'):
                value = value[4:]
            return Path(value).joinpath(*reversed(tail))
        error = c.get_last_error()
        if error not in (2, 3) or p == p.parent:
            raise c.WinError(error)
        tail.append(p.name)
        p = p.parent


def volume(path):
    k = kernel()
    root = Path(path).anchor
    if not root or root.startswith('\\\\'):
        raise ValueError('Only local drive-letter paths are supported / 仅支持本地盘符路径')
    fs = c.create_unicode_buffer(64)
    k.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, c.c_void_p,
                                       c.c_void_p, c.c_void_p, w.LPWSTR, w.DWORD]
    if not k.GetVolumeInformationW(root, None, 0, None, None, None, fs, len(fs)):
        raise c.WinError(c.get_last_error())
    k.GetDriveTypeW.argtypes = [w.LPCWSTR]
    return {'root': root, 'filesystem': fs.value, 'fixed': k.GetDriveTypeW(root) == 3}


def make_junction(link, target):
    """Create a directory junction, failing if the entry already exists."""
    k = kernel()
    target = os.path.abspath(str(target))
    substitute = ('\\??\\' + target).encode('utf-16-le')
    display = target.encode('utf-16-le')
    payload = substitute + b'\0\0' + display + b'\0\0'
    data = struct.pack('<IHHHHHH', 0xA0000003, 8 + len(payload), 0,
                       0, len(substitute), len(substitute) + 2, len(display)) + payload
    os.mkdir(extended(link))
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, c.c_void_p, w.DWORD, c.c_void_p,
                                 w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
    h = k.CreateFileW(extended(link), 0x40000000, 7, None, 3, 0x02200000, None)
    try:
        if h == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        returned = w.DWORD()
        buf = c.create_string_buffer(data)
        if not k.DeviceIoControl(h, 0x900A4, buf, len(data), None, 0, c.byref(returned), None):
            raise c.WinError(c.get_last_error())
    except BaseException:
        if h != c.c_void_p(-1).value:
            k.CloseHandle(h)
            h = c.c_void_p(-1).value
        os.rmdir(extended(link))
        raise
    finally:
        if h != c.c_void_p(-1).value:
            k.CloseHandle(h)


def copy_file(source, destination, overwrite=False):
    # CopyFile preserves NTFS named streams; the engine hashes them too.
    k = kernel()
    k.CopyFileW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.BOOL]
    if not k.CopyFileW(extended(source), extended(destination), not overwrite):
        raise c.WinError(c.get_last_error())


def copy_acl(source, destination):
    """Keep source access rules instead of inheriting a broader target ACL."""
    a = c.WinDLL('advapi32', use_last_error=True)
    a.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    a.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p]
    needed = w.DWORD()
    a.GetFileSecurityW(extended(source), 4, None, 0, c.byref(needed))
    if not needed.value:
        raise c.WinError(c.get_last_error())
    buf = c.create_string_buffer(needed.value)
    if not a.GetFileSecurityW(extended(source), 4, buf, len(buf), c.byref(needed)):
        raise c.WinError(c.get_last_error())
    if not a.SetFileSecurityW(extended(destination), 0x80000004, buf):
        raise c.WinError(c.get_last_error())


def streams(path):
    class StreamData(c.Structure):
        _fields_ = [('size', c.c_longlong), ('name', w.WCHAR * 296)]
    k = kernel()
    k.FindFirstStreamW.argtypes = [w.LPCWSTR, c.c_int, c.POINTER(StreamData), w.DWORD]
    k.FindFirstStreamW.restype = w.HANDLE
    k.FindNextStreamW.argtypes = [w.HANDLE, c.POINTER(StreamData)]
    k.FindClose.argtypes = [w.HANDLE]
    data = StreamData()
    h = k.FindFirstStreamW(extended(path), 0, c.byref(data), 0)
    if h == c.c_void_p(-1).value:
        error = c.get_last_error()
        if error == 38:
            return []
        raise c.WinError(error)
    result = []
    try:
        while True:
            if data.name != '::$DATA':
                result.append((data.name, data.size))
            if not k.FindNextStreamW(h, c.byref(data)):
                if c.get_last_error() != 38:
                    raise c.WinError(c.get_last_error())
                break
    finally:
        k.FindClose(h)
    return result


def processes():
    class Entry(c.Structure):
        _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                    ('heap', c.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                    ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                    ('name', w.WCHAR * 260)]
    k = kernel()
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    k.CloseHandle.argtypes = [w.HANDLE]
    h = k.CreateToolhelp32Snapshot(2, 0)
    if h == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    entry = Entry()
    entry.size = c.sizeof(entry)
    out = {}
    try:
        ok = k.Process32FirstW(h, c.byref(entry))
        while ok:
            out[entry.pid] = entry.name
            ok = k.Process32NextW(h, c.byref(entry))
    finally:
        k.CloseHandle(h)
    return out


def app_processes():
    # A persistent sandbox service is deliberately NOT an application blocker.
    names = {'chatgpt.exe', 'codex.exe', 'codex-code-mode-host.exe', 'codex-app.exe'}
    return [{'pid': pid, 'name': name} for pid, name in processes().items()
            if name.lower() in names and pid != os.getpid()]


def directory_handles(root):
    """Best-effort handle evidence, not proof that an entry denies rename.

    Called in an isolated, time-bounded child process. No handles are closed in
    another process and no process is terminated.
    """
    k = kernel()
    n = c.WinDLL('ntdll')
    U = c.c_size_t
    class Entry(c.Structure):
        _fields_ = [('obj', c.c_void_p), ('pid', U), ('handle', U), ('access', w.ULONG),
                    ('trace', w.USHORT), ('type', w.USHORT), ('attrs', w.ULONG), ('reserved', w.ULONG)]
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.GetCurrentProcess.restype = w.HANDLE
    k.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
    k.GetFileType.argtypes = [w.HANDLE]
    k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    k.CloseHandle.argtypes = [w.HANDLE]
    size = 1024 * 1024
    while size <= 256 * 1024 * 1024:
        buf = c.create_string_buffer(size)
        needed = w.ULONG()
        status = n.NtQuerySystemInformation(64, buf, size, c.byref(needed))
        if status == 0:
            break
        if status != -1073741820:
            raise OSError('Handle enumeration unavailable: ' + hex(status & 0xffffffff))
        size = max(size * 2, needed.value + 4096)
    else:
        raise OSError('Handle table exceeds diagnostic limit')
    count = U.from_buffer(buf).value
    entries = (Entry * count).from_buffer(buf, 2 * c.sizeof(U))
    own = k.GetCurrentProcess()
    opened = {}
    names = processes()
    result = []
    pathbuf = c.create_unicode_buffer(32768)
    prefix = os.path.normcase(extended(Path(root).resolve())).rstrip('\\')
    try:
        for e in entries:
            if e.pid == os.getpid():
                continue
            if e.pid not in opened:
                opened[e.pid] = k.OpenProcess(0x40, False, e.pid)
            ph = opened[e.pid]
            if not ph:
                continue
            h = w.HANDLE()
            if not k.DuplicateHandle(ph, w.HANDLE(e.handle), own, c.byref(h), 0, False, 2):
                continue
            try:
                if k.GetFileType(h) != 1:
                    continue
                length = k.GetFinalPathNameByHandleW(h, pathbuf, len(pathbuf), 0)
                if not 0 < length < len(pathbuf):
                    continue
                p = os.path.normcase(pathbuf.value)
                if p == prefix or p.startswith(prefix + '\\'):
                    result.append({'pid': e.pid, 'process': names.get(e.pid, 'unknown'),
                                   'path': pathbuf.value, 'access': hex(e.access)})
            finally:
                k.CloseHandle(h)
    finally:
        for h in opened.values():
            if h:
                k.CloseHandle(h)
    return {'handles': result, 'inaccessible_processes': sum(not h for h in opened.values()),
            'note': 'Best effort: an open handle is evidence, not proof of a blocking lock.'}


def diagnose(root, timeout=20):
    command = [sys.executable]
    if not getattr(sys, 'frozen', False):
        command += ['-m', 'codex_relocate']
    command += ['_handles', str(root)]
    try:
        p = subprocess.run(command, capture_output=True, timeout=timeout,
                           creationflags=0x08000000, encoding='utf-8')
        if p.returncode:
            return {'handles': [], 'incomplete': True, 'error': p.stderr[-500:]}
        return json.loads(p.stdout)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        return {'handles': [], 'incomplete': True, 'error': type(exc).__name__}
