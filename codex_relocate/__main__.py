import argparse
import json
import sys

from . import __version__


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr:
        sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description='Codex Relocate — verified local Windows folder migration')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--state-dir', help='Override local job journal directory')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('gui')
    scan_parser = sub.add_parser('scan', help='Read-only discovery or size scan')
    scan_parser.add_argument('source', nargs='?')
    for name in ('diagnose', '_handles'):
        p = sub.add_parser(name, help='Read-only open-handle evidence')
        p.add_argument('source')
    p = sub.add_parser('move', help='Copy, SHA-256 verify, switch; keep original')
    p.add_argument('source')
    p.add_argument('destination')
    for name in ('resume', 'release', 'rollback'):
        p = sub.add_parser(name)
        p.add_argument('job_id')
        if name in ('release', 'rollback'):
            p.add_argument('--yes', action='store_true', required=True, help='Confirm the named operation')
    sub.add_parser('jobs')
    args = parser.parse_args()
    if args.command in (None, 'gui'):
        from .gui import launch
        launch(args.state_dir)
        return 0
    from . import windows
    from .discovery import candidates, scan
    from .engine import Engine
    try:
        if args.command == '_handles':
            result = windows.directory_handles(args.source)
        elif args.command == 'diagnose':
            result = windows.diagnose(args.source)
        elif args.command == 'scan':
            result = scan(args.source) if args.source else candidates()
        else:
            engine = Engine(args.state_dir, lambda msg: print(msg, file=sys.stderr, flush=True))
            if args.command == 'move':
                result = engine.migrate(args.source, args.destination)
            elif args.command == 'jobs':
                result = engine.jobs()
            else:
                result = getattr(engine, args.command)(args.job_id)
            if isinstance(result, dict):
                result = {k: v for k, v in result.items() if k != 'manifest'}
            elif isinstance(result, list):
                result = [{k: v for k, v in j.items() if k != 'manifest'} for j in result]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
