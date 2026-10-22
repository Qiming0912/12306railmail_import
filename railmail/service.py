"""Public API shared by CLI and future GUI."""
import csv
import json
from .parser import decode_email, parse_email, clean_body, PARSER_VERSION
from .reader import iter_sources
from .repository import Repository, dumps, now

CSV_COLUMNS = [('id','记录ID'),('kind','票务类型'),('passenger','乘车人'),
 ('order_no','订单号'),('travel_date','乘车日期'),('departure_time','发车时间'),
 ('train','车次'),('origin','出发站'),('destination','到达站'),('seat','车厢座位'),
 ('seat_class','席别'),('ticket_kind','票种'),('fare','票价（元）'),
 ('refund_fee','退票费（元）'),('refund_amount','应退票款（元）'),('gate','检票口'),
 ('business_time','邮件时间（UTC）'),('email_id','来源邮件ID'),('manual','人工修正'),('notes','备注')]


class RailMail:
    def __init__(self, db_path):
        self.repository = Repository(db_path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.repository.close()

    def import_sources(self, paths, progress=None):
        """Progress callback receives a summary copy after each source; no UI calls."""
        c = self.repository.connection
        stats = dict(seen=0, imported=0, duplicate=0, errors=0, issues=0)
        with c:
            batch_id = c.execute('INSERT INTO import_batch(started_at) VALUES(?)', (now(),)).lastrowid
        for source, raw, error in iter_sources(paths):
            stats['seen'] += 1
            if error:
                with c:
                    c.execute('INSERT INTO email_source VALUES(NULL,NULL,?,?,?)', (batch_id, source, error))
                stats['errors'] += 1
            else:
                # One transaction per message. A failure never leaves a partial email or ticket.
                try:
                    mail = decode_email(raw)
                    result = parse_email(mail)
                except Exception as exc:
                    # Keep the source fingerprint and error, not the original EML bytes.
                    import hashlib
                    mail = dict(raw_hash=hashlib.sha256(raw).hexdigest(), subject='',
                                sent_at='', raw_date='', body='', message_id='')
                    result = dict(kind='解析失败', tickets=[], issues=[('decode_error', str(exc))])
                with c:
                    existing = c.execute('SELECT id FROM email WHERE raw_hash=?', (mail['raw_hash'],)).fetchone()
                    if existing:
                        email_id = existing[0]
                        stats['duplicate'] += 1
                    else:
                        email_id = c.execute('''INSERT INTO email(raw_hash,raw,subject,sent_at,raw_date,body,message_id)
                          VALUES(?,?,?,?,?,?,?)''', (mail['raw_hash'], b'', mail['subject'], mail['sent_at'],
                                                     mail['raw_date'], mail['body'], mail['message_id'])).lastrowid
                        self.repository.store_parse(email_id, mail, result, PARSER_VERSION)
                        stats['imported'] += 1
                        stats['issues'] += len(result['issues'])
                    c.execute('INSERT INTO email_source VALUES(NULL,?,?,?,NULL)', (email_id, batch_id, source))
            if progress:
                progress(dict(stats))
        with c:
            c.execute('UPDATE import_batch SET finished_at=?,summary=? WHERE id=?', (now(), dumps(stats), batch_id))
        return dict(batch_id=batch_id, **stats)

    def query_tickets(self, filters=None):
        return self.repository.query(filters)

    def update_ticket(self, ticket_id, changes, reason):
        return self.repository.update_ticket(ticket_id, changes, reason)

    def get_email(self, email_id):
        row = self.repository.connection.execute('SELECT * FROM email WHERE id=?', (email_id,)).fetchone()
        if not row:
            raise KeyError(email_id)
        return dict(row)

    def list_issues(self, unresolved_only=True):
        sql = 'SELECT * FROM parse_issue' + (' WHERE resolved=0' if unresolved_only else '') + ' ORDER BY id'
        return [dict(r) for r in self.repository.connection.execute(sql)]

    def resolve_issue(self, issue_id):
        c = self.repository.connection
        with c:
            if not c.execute('UPDATE parse_issue SET resolved=1 WHERE id=?', (issue_id,)).rowcount:
                raise KeyError(issue_id)

    def list_notifications(self):
        return [dict(r) for r in self.repository.connection.execute('SELECT * FROM notification ORDER BY email_id')]

    def ticket_history(self, ticket_id):
        c = self.repository.connection
        key = c.execute('SELECT kind,passenger,order_no FROM ticket_event WHERE id=?', (ticket_id,)).fetchone()
        if not key:
            raise KeyError(ticket_id)
        return [dict(r) for r in c.execute('''SELECT * FROM ticket_version WHERE
          kind=? AND passenger=? AND order_no=? ORDER BY business_time,id''', tuple(key))]

    def reparse_emails(self, email_ids=None):
        c = self.repository.connection
        ids = list(email_ids) if email_ids is not None else [r[0] for r in c.execute('SELECT id FROM email ORDER BY id')]
        for email_id in ids:
            with c:
                stored = self.get_email(email_id)
                # Legacy databases may still contain full EML. Decode it once, then
                # discard those bytes; subsequent parses use the stored clean text.
                mail = decode_email(stored['raw']) if stored['raw'] else dict(stored)
                mail['body'] = clean_body(mail['body'])
                c.execute('UPDATE email SET raw=?,subject=?,sent_at=?,raw_date=?,body=?,message_id=? WHERE id=?',
                          (b'', mail['subject'], mail['sent_at'], mail['raw_date'], mail['body'], mail['message_id'], email_id))
                self.repository.store_parse(email_id, mail, parse_email(mail), PARSER_VERSION)
        return len(ids)

    def compact_database(self):
        """Clean legacy messages and release unused SQLite pages. No outer transaction."""
        from pathlib import Path
        c = self.repository.connection
        path = Path(c.execute('PRAGMA database_list').fetchone()[2])
        before = path.stat().st_size
        count = self.reparse_emails()
        c.execute('VACUUM')
        return {'emails': count, 'bytes_before': before, 'bytes_after': path.stat().st_size}

    def export_csv(self, output_path, filters=None, overwrite=False):
        from pathlib import Path
        path = Path(output_path)
        if path.resolve() == Path(self.repository.connection.execute('PRAGMA database_list').fetchone()[2]).resolve():
            raise ValueError('CSV output cannot overwrite the database')
        path.parent.mkdir(parents=True, exist_ok=True)
        records = self.query_tickets(filters)
        with path.open('w' if overwrite else 'x', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([label for _, label in CSV_COLUMNS])
            for record in records:
                writer.writerow([record.get(key, '') for key, _ in CSV_COLUMNS])
        return len(records)
