import argparse
import json
import sys
from .service import RailMail


def main(argv=None):
    parser = argparse.ArgumentParser(description='12306 邮件整理核心（Python 3.9+，标准库）')
    parser.add_argument('--db', required=True, help='SQLite 数据库路径')
    sub = parser.add_subparsers(dest='command', required=True)
    imp = sub.add_parser('import', help='导入 EML / ZIP / 文件夹')
    imp.add_argument('paths', nargs='+')
    for name in ('query', 'export'):
        command = sub.add_parser(name)
        for flag in ('passenger', 'kind', 'order-no', 'train', 'date-from', 'date-to'):
            command.add_argument('--' + flag)
        if name == 'export':
            command.add_argument('-o', '--output', required=True)
            command.add_argument('--force', action='store_true')
    sub.add_parser('issues', help='查看待核对问题')
    sub.add_parser('notifications', help='查看通知与非车票邮件')
    sub.add_parser('compact', help='清理旧库完整EML、裁剪正文并压缩数据库')
    rep = sub.add_parser('reparse', help='重新解析已存正文，保护人工修正')
    rep.add_argument('--email-id', type=int, action='append')
    hist = sub.add_parser('history')
    hist.add_argument('ticket_id', type=int)
    edit = sub.add_parser('update')
    edit.add_argument('ticket_id', type=int)
    edit.add_argument('--changes', required=True, help='JSON 对象，例如 {"seat":"07车04B号"}')
    edit.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    try:
        with RailMail(args.db) as app:
            if args.command == 'import':
                result = app.import_sources(args.paths)
            elif args.command in ('query', 'export'):
                filters = {k: getattr(args, k) for k in ('passenger','kind','order_no','train','date_from','date_to') if getattr(args,k)}
                result = app.query_tickets(filters) if args.command == 'query' else dict(exported=app.export_csv(args.output, filters, args.force))
            elif args.command == 'issues':
                result = app.list_issues()
            elif args.command == 'notifications':
                result = app.list_notifications()
            elif args.command == 'compact':
                result = app.compact_database()
            elif args.command == 'history':
                result = app.ticket_history(args.ticket_id)
            elif args.command == 'update':
                changes = json.loads(args.changes)
                if not isinstance(changes, dict):
                    raise ValueError('--changes must be a JSON object')
                app.update_ticket(args.ticket_id, changes, args.reason)
                result = dict(updated=args.ticket_id)
            else:
                result = dict(reparsed=app.reparse_emails(args.email_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get('errors') else 0
    except (ValueError, KeyError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
