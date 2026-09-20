# Safety and recovery

Codex Relocate 0.1 is a Windows beta. Keep an independent backup of irreplaceable work. This tool provides verification and conservative failure handling, not a backup service or an atomic filesystem snapshot.

## Transaction

1. **Plan:** reject system locations, existing destinations, path overlap, unsupported volumes, and paths that contain the tool, its working directory, or its journal. Known ChatGPT/Codex data roots require app processes to be closed. The sandbox service is not mistaken for an app process.
2. **Copy:** hash a no-follow source inventory; copy into a new directory. Regular files use Windows CopyFile (including named streams). Directories receive protected copies of the source DACL, so they do not inherit broader destination-parent access rules. Owner/audit metadata is not replicated. Interrupted owned partial copies can be replaced from the still-verified source.
3. **Verify:** compare source stability, all file/stream hashes, empty directories, link type and target. An unexpected entry or read failure stops the job.
4. **Switch:** journal the intent, rename the original to a unique sibling, and create a junction at the original path. Verify the junction and perform a small write-through test. If junction creation fails, restore the original name when possible.
5. **Retain:** do not automatically delete the original. The UI makes the next operation explicit.
6. **Release:** verify the destination against the recorded content and the retained original before deleting only the old tree. Never traverse links during cleanup. Record measured drive free space. Once release starts, resume release; rollback is no longer allowed.

One operation holds an exclusive lock within the chosen journal directory. Do not run separate instances with different `--state-dir` values against the same folders.

## Interrupted jobs

Keep the journal directory and both data locations. Open the utility independently, select the job, and choose **Resume**. If interrupted during release, choose **Release space** again.

| State | Next action |
| --- | --- |
| `planned`, `copying` | Resume; source must still match the original snapshot |
| `switch_pending` | Resume; inspects whether the original has already been renamed |
| `migrated` | Release space or Undo switch, before modifying either copy |
| `releasing` | Release space again; already removed entries are accounted for |
| `rollback_pending` | Resume to finish restoring the original name |
| `released` | Use the destination; the old path is a junction |
| `rolled_back` | Use the original; the destination copy is retained |

A process/power failure between creating an entry and recording its identity may leave an unrecognized empty destination or partial file. The tool refuses to guess ownership. Preserve the journal and inspect the entry; do not delete original data just to make a retry pass. Power-loss durability depends on the filesystem and storage device; the journal flushes its files, but is not a distributed/ACID transaction.

If source files changed during copy, resume refuses to combine versions. Keep both copies and start a fresh job into a different empty destination. If destination files changed after switch, automatic cleanup/undo also refuses. Resolve versions manually with a backup before removing the retained tree.

## Limits

- Stop all writers before migration/release. Process-name checks are a convenience, not a complete writer detector. There is no VSS snapshot or exclusive lock over the entire tree. Concurrent changes between checks remain possible; this is not designed for hostile, multi-user mutable directories.
- Open-handle diagnostics use best-effort Windows handle enumeration in a 20-second child process. Undocumented API availability may vary; inaccessible processes and timeouts are reported. Readable handles do not necessarily block a rename, and absent handles do not prove safety.
- Fixed local NTFS drives only; unsupported reparse tags, cloud placeholders, directory named streams and EFS encryption are rejected. Junctions and file/directory symlinks are preserved; creating symlinks can require Developer Mode or appropriate privileges. The utility does not elevate itself.
- No source/destination link ancestors. Already-relocated entries can be inspected; additional migration requires selecting the physical directory after considering existing references.
- Existing destination trees are not merged. Hard-linked files are copied as independent files. File contents and named streams are verified; directory timestamps, audit rules, ownership, physical allocation, deduplication and compression layout are not promised to match.
- Installation paths, WindowsApps, whole user profiles and drive roots are outside scope. Discovery is deliberately limited to known data locations, not an exhaustive map of every version's cache directories.
- No offline repair of internal application databases. A same-machine junction preserves paths; it does not fix pre-existing application corruption or support moving to another computer.
- Free-space change is measured over the operation. Other programs and same-volume copies can affect or even reverse the number; it is not presented as an exact allocated-byte count.

## Privacy

Runtime code has no network client. Local manifests contain paths, content hashes and link targets. They can reveal project names: do not attach them publicly without redaction. Do not upload `.codex`, authentication files, chat databases or personal documents to issues.

Technical references: [Microsoft: hard links and junctions](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions), [Microsoft: reparse points](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points), [Python: shutil](https://docs.python.org/3/library/shutil.html).

## 中文说明

先关闭写入程序，再迁移。工具不使用系统快照，不能保证并发写入下的一致性。搬迁成功后先保留原件；点击“释放空间”才删除已校验的旧副本。释放开始后只能继续清理，不能撤销切换。

中断后保留日志和两份目录，从迁移记录继续；清理中断则再次点击“释放空间”。如果程序发现源数据、目标数据或目录身份有变化，会拒绝自动处理，请先备份并人工核对版本。

目录句柄是排查线索，不代表该进程一定阻止改名。不会强杀进程，不会自动提权，不会停止系统服务。云盘占位文件、加密文件、系统目录及其他不支持的结构会明确拒绝。

本地记录包含路径和哈希，公开反馈时请脱敏；不要上传聊天数据库、登录信息或私人文档。
