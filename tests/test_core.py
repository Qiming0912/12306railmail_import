import csv
import tempfile
import unittest
import zipfile
from email.message import EmailMessage
from pathlib import Path
from railmail import RailMail
from railmail.parser import decode_email, parse_email


def message(date='Wed, 01 Apr 2015 12:00:00 +0800', seat='07车04B号',
            subject='网上购票系统-用户改签通知', short=False):
    m = EmailMessage()
    m['Subject'] = subject
    m['Date'] = date
    m['Message-ID'] = '<same-id-is-not-a-dedup-key@example.test>'
    m.set_content('您于2015年04月01日成功改签车票1张，新车票票款共计72.00元，订单号码EC04731598。'
                  ' 1.测试乘客,%s04月08日10:40开,唐山—北京南,D6792次列车,%s,二等座,票价72.00元。'
                  ' 为了确保列车运行秩序。温馨提示：' % ('' if short else '2015年', seat))
    return m.as_bytes()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='railmail-test-', dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.app = RailMail(self.root / 'data.sqlite')

    def tearDown(self):
        self.app.__exit__()
        self.temp.cleanup()

    def put(self, name, raw):
        p = self.root / name
        p.write_bytes(raw)
        return p

    def test_duplicate_sources_and_order_independence(self):
        newer = self.put('新.eml', message('Thu, 02 Apr 2015 12:00:00 +0800', '07车05C号'))
        old = self.put('旧.eml', message())
        result = self.app.import_sources([newer, old])
        self.assertEqual(result['imported'], 2)
        current = self.app.query_tickets()
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['seat'], '07车05C号')
        self.assertEqual(len(self.app.ticket_history(current[0]['id'])), 2)
        renamed = self.put('renamed.eml', newer.read_bytes())
        self.assertEqual(self.app.import_sources([renamed])['duplicate'], 1)
        self.assertEqual(len(self.app.ticket_history(current[0]['id'])), 2)

    def test_manual_edit_survives_import_and_reparse(self):
        p = self.put('one.eml', message())
        self.app.import_sources([p])
        row = self.app.query_tickets()[0]
        self.app.update_ticket(row['id'], {'seat': '人工核对座位'}, '根据原始车票核对')
        self.app.reparse_emails()
        self.app.import_sources([self.put('new.eml', message('Thu, 02 Apr 2015 12:00:00 +0800'))])
        self.assertEqual(self.app.query_tickets()[0]['seat'], '人工核对座位')
        self.assertEqual(self.app.repository.connection.execute('SELECT count(*) FROM edit_history').fetchone()[0], 1)

    def test_legacy_year_and_zip(self):
        archive = self.root / '邮件.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../旧邮件.eml', message(short=True))
            z.writestr('空.eml', b'')
        self.assertEqual(self.app.import_sources([archive])['imported'], 2)
        self.assertFalse((self.root.parent / '旧邮件.eml').exists())
        self.assertEqual(self.app.query_tickets()[0]['travel_date'], '2015-04-08')
        self.assertIn('inferred_year', [i['code'] for i in self.app.list_issues()])
        self.assertEqual(len(self.app.list_notifications()), 1)

    def test_csv_filters_and_protection(self):
        self.app.import_sources([self.put('one.eml', message())])
        out = self.root / 'out.csv'
        self.assertEqual(self.app.export_csv(out, {'passenger':'测试乘客'}), 1)
        self.assertTrue(out.read_bytes().startswith(b'\xef\xbb\xbf'))
        with out.open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual(list(csv.DictReader(f))[0]['乘车人'], '测试乘客')
        with self.assertRaises(FileExistsError):self.app.export_csv(out)
        with self.assertRaises(ValueError):self.app.export_csv(self.root / 'data.sqlite', overwrite=True)
        with self.assertRaises(ValueError):self.app.query_tickets({'bad_sql':'x'})

    def test_html_fallback_and_gbk(self):
        m = EmailMessage(); m['Subject']='网上购票系统-用户支付通知'
        m.set_content('')
        body=decode_email(message())['body']
        m.add_alternative('<p>'+body.replace('唐山','<b>唐山</b>')+'</p>', subtype='html')
        self.assertEqual(len(parse_email(decode_email(m.as_bytes()))['tickets']), 1)
        raw=('Subject: test\r\nContent-Type: text/plain; charset=gb2312\r\n\r\n').encode()+body.encode('gb18030')
        self.assertEqual(len(parse_email(decode_email(raw))['tickets']), 1)

    def test_same_time_conflict_and_business_types(self):
        self.app.import_sources([self.put('a.eml',message()),self.put('b.eml',message(seat='08车01号'))])
        self.assertIn('same_time_conflict',[i['code'] for i in self.app.list_issues()])
        self.app.import_sources([self.put('c.eml',message(subject='网上购票系统-用户支付通知'))])
        self.assertEqual(len(self.app.query_tickets()), 2)

    def test_amount_and_count_validation(self):
        mail = decode_email(message())
        mail['body'] = mail['body'].replace('1张', '2张').replace('共计72.00', '共计144.00')
        codes = [code for code, detail in parse_email(mail)['issues']]
        self.assertIn('ticket_count', codes)
        self.assertIn('total_amount', codes)

    def test_missing_key_does_not_create_ticket(self):
        mail = message().replace(b'EC04731598', b'')
        self.app.import_sources([self.put('broken.eml', mail)])
        # MIME transfer encoding may encode the body; construct directly for this case.
        m = EmailMessage();m['Subject']='网上购票系统-用户支付通知'
        m.set_content('1.测试乘客,2015年04月08日10:40开,唐山—北京南,D6792次列车,07车04B号,二等座,票价72.00元。')
        self.app.import_sources([self.put('no-order.eml', m.as_bytes())])
        self.assertFalse(any(not r['order_no'] for r in self.app.query_tickets()))
        self.assertIn('missing_field', [i['code'] for i in self.app.list_issues()])

    def test_source_error_and_reparse_refresh(self):
        p = self.put('one.eml', message())
        stats = self.app.import_sources([self.root / 'not-found.eml', p])
        self.assertEqual(stats['errors'], 1)
        self.assertEqual(stats['imported'], 1)
        before = self.app.query_tickets()
        self.assertEqual(self.app.reparse_emails(), 1)
        self.assertEqual(self.app.query_tickets(), before)
        c = self.app.repository.connection
        self.assertEqual(c.execute('SELECT count(*) FROM ticket_version WHERE active=1').fetchone()[0],1)
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')


if __name__ == '__main__':
    unittest.main()
