"""GUI tests use temporary synthetic data, never the user's database."""
import tempfile
import time
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

try:
    import tkinter as tk
    from railmail.gui import RailMailWindow, validated_filters, validate_changes
except ImportError:
    tk = None


@unittest.skipIf(tk is None, 'tkinter is not installed')
class InputTests(unittest.TestCase):
    def test_date_range(self):
        self.assertEqual(validated_filters({'passenger':' 测试乘客 ', 'date_from':'2025-01-01'}),
                         {'passenger':'测试乘客','date_from':'2025-01-01'})
        for values in ({'date_from':'2025-02-30'}, {'date_from':'2025-2-1'},
                       {'date_from':'2026-01-01','date_to':'2025-01-01'}):
            with self.assertRaises(ValueError):validated_filters(values)

    def test_edit_validation(self):
        self.assertEqual(validate_changes({'seat':'01车01A号'}, {'seat':'01车02A号'}), {'seat':'01车02A号'})
        for value in ('NaN','Infinity','-1','abc'):
            with self.assertRaises(ValueError):validate_changes({}, {'fare':value})


@unittest.skipIf(tk is None, 'tkinter is not installed')
class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk(); probe.withdraw(); probe.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest('No usable Tk display: ' + str(exc))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='railmail-gui-test-',dir=Path.cwd())
        self.path = Path(self.temp.name)
        self.root = tk.Tk();self.root.withdraw()
        self.window = RailMailWindow(self.root, self.path / 'tickets.sqlite')
        self.errors = []
        self.window.error = lambda exc: self.errors.append(str(exc))

    def tearDown(self):
        if self.window.worker:self.window.worker.join(timeout=10)
        self.window.busy = False
        self.window.close()
        self.temp.cleanup()

    def wait(self):
        deadline=time.monotonic()+10
        while self.window.busy and time.monotonic()<deadline:
            self.root.update()
            time.sleep(.02)
        self.assertFalse(self.window.busy)
        self.assertEqual(self.errors, [])

    def test_import_filter_source_history_export(self):
        m=EmailMessage();m['Subject']='网上购票系统-用户支付通知';m['Date']='Wed, 01 Apr 2015 12:00:00 +0800'
        m.set_content('您于2015年04月01日成功购买了1张车票，票款共计72.00元，订单号码E000000001。'
                      ' 1.测试乘客,2015年04月08日10:40开,唐山—北京南,D6792次列车,07车04B号,二等座,票价72.00元。')
        mail=self.path/'test.eml';mail.write_bytes(m.as_bytes())
        self.window.add_sources([mail]);self.window.start_import();self.wait()
        self.assertEqual(len(self.window.rows),1)
        self.assertIn('测试乘客',self.window.source_text.get('1.0','end'))
        self.assertIn('07车04B号',self.window.history_text.get('1.0','end'))
        self.window.filters['passenger'].set('其他人');self.window.apply_filters()
        self.assertEqual(len(self.window.rows),0)
        self.window.reset_filters()
        out=self.path/'export.csv'
        with patch('railmail.gui.filedialog.asksaveasfilename', return_value=str(out)):
            self.window.export_csv()
        self.wait()
        self.assertIn('测试乘客',out.read_text(encoding='utf-8-sig'))
        self.window.start_import();self.wait()
        self.assertEqual(len(self.window.rows),1)
        self.assertIn('重复 1',self.window.import_status.get())

    def test_issue_notification_and_reparse(self):
        empty=self.path/'empty.eml';empty.write_bytes(b'')
        self.window.add_sources([empty]);self.window.start_import();self.wait()
        self.assertEqual(len(self.window.issues),1)
        self.assertEqual(len(self.window.notices),1)
        iid=next(iter(self.window.issues))
        self.window.issue_tree.selection_set(iid);self.window.select_issue()
        self.assertIn('正文为空',self.window.issue_text.get('1.0','end'))
        with patch('railmail.gui.messagebox.askyesno',return_value=True):self.window.resolve_selected()
        self.assertEqual(len(self.window.issues),0)
        self.window.run_task('重新解析',lambda app:{'reparsed':app.reparse_emails()});self.wait()
        self.assertEqual(len(self.window.issues),1)


if __name__=='__main__':unittest.main()
