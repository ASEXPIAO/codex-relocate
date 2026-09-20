"""One small window, English by default, with a Chinese switch."""
import json
import ctypes as c
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .discovery import candidates, scan
from .engine import Engine
from .windows import diagnose

TEXT = {
    'tagline': ('Move the data. Keep the path.', '数据搬家，原路径照常用。'),
    'source': ('Source folder', '源文件夹'),
    'destination': ('New destination folder', '目标文件夹（必须为新目录）'),
    'browse': ('Browse…', '浏览…'),
    'discover': ('Find Codex folders', '发现 Codex 目录'),
    'scan': ('Check size', '检查占用'),
    'diagnose': ('Diagnose handles', '诊断占用'),
    'move': ('Verify & move', '校验并迁移'),
    'jobs': ('Recent migrations', '迁移记录'),
    'resume': ('Resume', '继续'),
    'release': ('Release space', '释放空间'),
    'rollback': ('Undo switch', '撤销切换'),
    'note': ('Close apps writing to the folder. The original is kept until you release it.',
             '请先关闭会写入该目录的程序；点击“释放空间”前会保留原件。'),
    'ready': ('Ready · local only · no background service', '就绪 · 全程本地 · 无后台服务'),
    'confirm_move': ('Copy and verify this folder, then redirect the old path?\nThe original is retained.\n\n',
                     '复制并校验此目录，然后让原路径指向目标？\n旧副本会保留。\n\n'),
    'confirm_release': ('Re-verify both copies and permanently remove the retained original?\n'
                        'After release, Undo switch is no longer available.\n\n',
                        '重新校验两份数据后永久删除保留的旧副本？\n释放后无法直接撤销切换。\n\n'),
    'confirm_rollback': ('Restore the original path from the retained original?\nThe destination copy stays.\n\n',
                         '用保留的旧副本恢复原路径？\n目标盘副本仍会保留。\n\n'),
    'busy_close': ('An operation is running. Keep this window open until it finishes.',
                   '正在处理，请保持此窗口打开直至操作结束。'),
    'choose_job': ('Select a migration first.', '请先选择一条迁移记录。'),
    'diagnostic_note': ('Open handles are evidence, not proof of a blocking lock. Close the named app normally; '
                        'this tool never kills processes. A blank list is not a guarantee.',
                        '打开的句柄不一定阻止迁移。请正常关闭对应程序；工具不会强杀进程。空列表也不保证没有占用。'),
}


def human_bytes(size):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(size) < 1024 or unit == 'TiB':
            return '{:.1f} {}'.format(size, unit)
        size /= 1024


class App:
    def __init__(self, root, state_dir=None):
        self.root = root
        self.language = tk.StringVar(value='English')
        self.source = tk.StringVar()
        self.destination = tk.StringVar()
        self.status = tk.StringVar()
        self.events = queue.Queue()
        self.busy = False
        self.engine = Engine(state_dir, lambda msg: self.events.put(('progress', msg)))
        self.translated = []
        self.actions = []
        root.title('Codex Relocate')
        root.geometry('820x720')
        root.minsize(760, 700)
        root.configure(bg='#f6f7fb')
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#f6f7fb', foreground='#182234')
        style.configure('TButton', padding=(12, 7), background='#e8edf6', borderwidth=0)
        style.map('TButton', background=[('active', '#d8e3f5')])
        style.configure('Accent.TButton', background='#2459c4', foreground='white')
        style.map('Accent.TButton', background=[('active', '#1749ae'), ('disabled', '#bac7df')])
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 24))
        style.configure('Muted.TLabel', foreground='#627088')
        shell = ttk.Frame(root, padding=26)
        shell.pack(fill='both', expand=True)
        top = ttk.Frame(shell)
        top.pack(fill='x')
        ttk.Label(top, text='Codex Relocate', style='Title.TLabel').pack(side='left')
        picker = ttk.Combobox(top, textvariable=self.language, values=['English', '中文'], state='readonly', width=9)
        picker.pack(side='right')
        picker.bind('<<ComboboxSelected>>', lambda event: self.translate())
        self.label(shell, 'tagline', style='Muted.TLabel').pack(anchor='w', pady=(3, 20))
        for key, var, command in [('source', self.source, self.choose_source),
                                   ('destination', self.destination, self.choose_destination)]:
            self.label(shell, key).pack(anchor='w', pady=(8, 5))
            row = ttk.Frame(shell)
            row.pack(fill='x')
            ttk.Entry(row, textvariable=var).pack(side='left', fill='x', expand=True, ipady=5)
            self.button(row, 'browse', command).pack(side='right', padx=(8, 0))
        row = ttk.Frame(shell)
        row.pack(fill='x', pady=(16, 8))
        for key, cmd in [('discover', self.find), ('scan', self.check_size), ('diagnose', self.check_handles)]:
            self.button(row, key, cmd).pack(side='left', padx=(0, 6))
        self.button(row, 'move', self.move, style='Accent.TButton').pack(side='right')
        self.label(shell, 'note', style='Muted.TLabel', wraplength=740).pack(anchor='w', pady=(2, 18))
        self.label(shell, 'jobs').pack(anchor='w', pady=(0, 6))
        self.job_picker = ttk.Combobox(shell, state='readonly')
        self.job_picker.pack(fill='x', ipady=3)
        row = ttk.Frame(shell)
        row.pack(fill='x', pady=8)
        for key in ('resume', 'release', 'rollback'):
            self.button(row, key, lambda k=key: self.job_action(k)).pack(side='left', padx=(0, 7))
        self.output = tk.Text(shell, height=9, font=('Consolas', 10), bg='white', fg='#26334b',
                              relief='flat', padx=12, pady=10, wrap='word', state='disabled')
        self.output.pack(fill='both', expand=True, pady=(4, 10))
        self.bar = ttk.Progressbar(shell, mode='indeterminate')
        self.bar.pack(fill='x')
        footer = ttk.Frame(shell)
        footer.pack(fill='x', pady=(8, 0))
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel').pack(side='left')
        ttk.Label(footer, text='v' + __version__ + ' · beta', style='Muted.TLabel').pack(side='right')
        self.translate()
        self.refresh_jobs()
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.poll_id = root.after(100, self.poll)

    def t(self, key):
        return TEXT[key][1 if self.language.get() == '中文' else 0]

    def label(self, parent, key, **kwargs):
        widget = ttk.Label(parent, **kwargs)
        self.translated.append((widget, key))
        return widget

    def button(self, parent, key, command, **kwargs):
        widget = ttk.Button(parent, command=command, **kwargs)
        self.translated.append((widget, key))
        self.actions.append(widget)
        return widget

    def translate(self):
        for widget, key in self.translated:
            widget.configure(text=self.t(key))
        if not self.busy:
            self.status.set(self.t('ready'))

    def choose_source(self):
        p = filedialog.askdirectory()
        if p:
            self.source.set(p)

    def choose_destination(self):
        parent = filedialog.askdirectory(title=self.t('destination'))
        if parent:
            self.destination.set(str(Path(parent) / (Path(self.source.get()).name or 'CodexData')))

    def log(self, text):
        self.output.configure(state='normal')
        self.output.insert('end', text + '\n')
        self.output.see('end')
        self.output.configure(state='disabled')

    def run(self, function):
        if self.busy:
            return
        self.busy = True
        self.status.set('Working…' if self.language.get() == 'English' else '正在处理…')
        for button in self.actions:
            button.configure(state='disabled')
        self.bar.start(12)
        def work():
            try:
                self.events.put(('done', function()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=work, daemon=False).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'progress':
                    self.status.set(value[:100])
                else:
                    self.busy = False
                    self.bar.stop()
                    for button in self.actions:
                        button.configure(state='normal')
                    self.status.set(self.t('ready'))
                    if kind == 'error':
                        self.log('Error: ' + value)
                        messagebox.showerror('Codex Relocate', value)
                    else:
                        if isinstance(value, dict):
                            value = {k: v for k, v in value.items() if k != 'manifest'}
                        self.log(self.describe(value))
                    self.refresh_jobs()
        except queue.Empty:
            pass
        self.poll_id = self.root.after(100, self.poll)

    def find(self):
        found = candidates()
        if found:
            self.source.set(next((p['path'] for p in found if not p['already_linked']), found[0]['path']))
        self.log('\n'.join('{}{}\n  {}'.format(p['category'], ' · linked' if p['already_linked'] else '',
                                                p['physical_path']) for p in found) or 'No known folders found.')

    def describe(self, value):
        zh = self.language.get() == '中文'
        if not isinstance(value, dict):
            return str(value)
        if 'phase' in value:
            phase = {'migrated': '已切换，原件保留', 'released': '已释放旧副本', 'rolled_back': '已恢复原路径',
                     'copying': '复制中', 'switch_pending': '等待切换', 'releasing': '清理待继续'}.get(value['phase'], value['phase'])
            lines = [phase if zh else value['phase'].replace('_', ' ').capitalize(),
                     value['source'] + '\n→ ' + value['destination']]
            size = value.get('summary', {})
            lines.append(('已校验：' if zh else 'Verified: ') + '{} files · {} links · {}'.format(
                size.get('files', 0), size.get('links', 0), human_bytes(size.get('bytes', 0))))
            if 'free_after' in value:
                lines.append(('当前可用空间：' if zh else 'Free now: ') + human_bytes(value['free_after']))
                lines.append(('可用空间变化：' if zh else 'Observed free-space change: ') + human_bytes(value['observed_free_change']))
            elif value['phase'] == 'migrated':
                lines.append('下一步：释放空间，或撤销切换。' if zh else 'Next: Release space, or Undo switch.')
            return '\n'.join(lines)
        if 'handles' in value:
            lines = ['{} · PID {}\n  {}'.format(h['process'], h['pid'], h['path']) for h in value['handles']]
            if not lines:
                lines.append('未发现可见占用。' if zh else 'No visible matching handles.')
            if value.get('incomplete'):
                lines.append(('诊断不完整：' if zh else 'Incomplete diagnostic: ') + value.get('error', ''))
            if value.get('inaccessible_processes'):
                lines.append(('无法检查的进程数：' if zh else 'Inaccessible processes: ') + str(value['inaccessible_processes']))
            return '\n'.join(lines)
        if value.get('already_linked'):
            return ('已是目录链接，实际位置：' if zh else 'Already linked. Actual location: ') + value['physical_path']
        if 'bytes' in value:
            return '{} files · {} links · {}'.format(value['files'], value['links'], human_bytes(value['bytes']))
        return json.dumps(value, ensure_ascii=False, indent=2)

    def check_size(self):
        path = self.source.get()
        self.run(lambda: scan(path))

    def check_handles(self):
        path = self.source.get()
        self.log(self.t('diagnostic_note'))
        self.run(lambda: diagnose(path))

    def move(self):
        source, destination = self.source.get(), self.destination.get()
        if not source or not destination:
            messagebox.showerror('Codex Relocate', self.t('source') + ' / ' + self.t('destination'))
            return
        if messagebox.askyesno('Codex Relocate', self.t('confirm_move') + source + '\n→ ' + destination):
            self.run(lambda: self.engine.migrate(source, destination))

    def refresh_jobs(self):
        try:
            self.job_list = self.engine.jobs()
            self.job_picker['values'] = ['{} · {} · {}'.format(j['phase'], Path(j['source']).name, j['id'][:8])
                                         for j in self.job_list]
            if self.job_list:
                self.job_picker.current(0)
        except Exception as exc:
            self.log(str(exc))

    def job_action(self, action):
        index = self.job_picker.current()
        if index < 0:
            messagebox.showinfo('Codex Relocate', self.t('choose_job'))
            return
        job = self.job_list[index]
        if action != 'resume' and not messagebox.askyesno('Codex Relocate', self.t('confirm_' + action) +
                                                        job['source'] + '\n→ ' + job['destination']):
            return
        self.run(lambda: getattr(self.engine, action)(job['id']))

    def close(self):
        if self.busy:
            messagebox.showinfo('Codex Relocate', self.t('busy_close'))
        else:
            self.root.after_cancel(self.poll_id)
            self.root.destroy()


def launch(state_dir=None):
    # Hide only a console created for this executable, never the caller's terminal.
    import sys
    if getattr(sys, 'frozen', False):
        k = c.WinDLL('kernel32')
        ids = (c.c_uint32 * 2)()
        if k.GetConsoleProcessList(ids, 2) == 1:
            k.GetConsoleWindow.restype = c.c_void_p
            u = c.WinDLL('user32')
            u.ShowWindow.argtypes = [c.c_void_p, c.c_int]
            u.ShowWindow(k.GetConsoleWindow(), 0)
    root = tk.Tk()
    App(root, state_dir)
    root.mainloop()
