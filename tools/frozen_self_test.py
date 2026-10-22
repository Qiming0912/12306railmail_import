"""Opt-in frozen application diagnostics using disposable synthetic mail."""
import json
import tempfile
import time
import traceback
from email.message import EmailMessage
from pathlib import Path
import tkinter as tk
from railmail import RailMail
from railmail.gui import RailMailWindow


def run(report_path):
    report = Path(report_path).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    root = window = None
    result = {'ok': False, 'checks': []}
    try:
        with tempfile.TemporaryDirectory(prefix='railmail-check-', dir=report.parent) as folder:
            folder = Path(folder)
            mail = EmailMessage()
            mail['Subject'] = '网上购票系统-用户支付通知'
            mail['Date'] = 'Mon, 06 Apr 2015 12:00:00 +0800'
            mail.set_content('您于2015年04月06日成功购买了2张车票，票款共计144.00元，订单号码E000000001。'
                ' 1.张三,2015年04月08日10:40开,唐山—北京南,D6792次列车,07车04B号,二等座,票价72.00元。'
                ' 2.李四,2015年04月08日10:40开,唐山—北京南,D6792次列车,07车04C号,二等座,票价72.00元。'
                ' 温馨提示：不应保存在数据库中的多余说明。')
            source = folder / 'mail.eml'
            source.write_bytes(mail.as_bytes())
            (folder / 'empty.eml').write_bytes(b'')
            root = tk.Tk()
            root.withdraw()
            window = RailMailWindow(root, folder / 'check.sqlite')
            errors = []
            window.error = lambda error: errors.append(str(error))
            def wait():
                deadline = time.monotonic() + 30
                while window.busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.02)
                assert not window.busy, 'Background operation timed out'
                assert not errors, errors
            window.add_sources([source, folder / 'empty.eml'])
            window.start_import()
            wait()
            assert len(window.rows) == 2
            assert len(window.issues) == 1 and len(window.notices) == 1
            assert '07车' in window.source_text.get('1.0', 'end')
            assert '温馨提示' not in window.source_text.get('1.0', 'end')
            with RailMail(folder / 'check.sqlite') as app:
                assert app.get_email(1)['raw'] == b''
            result['checks'].append('GUI / Tk resources / SQLite / mail decoding / background import')
            window.start_import()
            wait()
            assert len(window.rows) == 2
            assert '重复 2' in window.import_status.get()
            result['checks'].append('Repeated import deduplication')
            window.filters['passenger'].set('张三')
            window.apply_filters()
            assert len(window.rows) == 1
            window.run_task('导出 CSV', lambda app: {
                'exported': app.export_csv(folder / 'result.csv', {'passenger': '张三'}),
                'path': str(folder / 'result.csv')})
            wait()
            assert '张三' in (folder / 'result.csv').read_text(encoding='utf-8-sig')
            result['checks'].append('Filtering / background CSV export')
            window.run_task('清理数据库', lambda app: {'compacted':app.compact_database()})
            wait()
            assert len(window.rows) == 1
            result['checks'].append('Trimmed body / no full EML stored / database compaction')
            with RailMail(folder / 'check.sqlite') as app:
                assert app.repository.connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            window.close()
            window = root = None
        result['ok'] = True
    except Exception:
        result['error'] = traceback.format_exc()
    finally:
        if window is not None:
            if window.worker:
                window.worker.join(timeout=35)
            window.busy = False
            window.close()
        elif root is not None:
            root.destroy()
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if result['ok'] else 1
