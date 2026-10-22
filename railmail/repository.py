"""SQLite persistence. Business key: (kind, passenger, order_no)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS import_batch(
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS email(
 id INTEGER PRIMARY KEY, raw_hash TEXT NOT NULL UNIQUE, raw BLOB NOT NULL,
 subject TEXT NOT NULL, sent_at TEXT NOT NULL, raw_date TEXT NOT NULL,
 body TEXT NOT NULL, message_id TEXT NOT NULL, kind TEXT, parser_version TEXT);
CREATE TABLE IF NOT EXISTS email_source(
 id INTEGER PRIMARY KEY, email_id INTEGER REFERENCES email(id),
 batch_id INTEGER NOT NULL REFERENCES import_batch(id), source TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS ticket_event(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, passenger TEXT NOT NULL,
 order_no TEXT NOT NULL, payload TEXT NOT NULL, business_time TEXT NOT NULL,
 email_id INTEGER REFERENCES email(id), manual INTEGER NOT NULL DEFAULT 0,
 UNIQUE(kind,passenger,order_no),
 CHECK(length(kind)>0 AND length(passenger)>0 AND length(order_no)>0));
CREATE TABLE IF NOT EXISTS ticket_version(
 id INTEGER PRIMARY KEY, email_id INTEGER NOT NULL REFERENCES email(id),
 ordinal INTEGER NOT NULL, kind TEXT NOT NULL, passenger TEXT NOT NULL,
 order_no TEXT NOT NULL, payload TEXT NOT NULL, business_time TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, parser_version TEXT NOT NULL,
 recorded_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS version_key ON ticket_version(kind,passenger,order_no,active);
CREATE TABLE IF NOT EXISTS parse_issue(
 id INTEGER PRIMARY KEY, email_id INTEGER REFERENCES email(id),
 code TEXT NOT NULL, detail TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS notification(
 email_id INTEGER PRIMARY KEY REFERENCES email(id), kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS edit_history(
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES ticket_event(id),
 before_payload TEXT NOT NULL, after_payload TEXT NOT NULL,
 reason TEXT NOT NULL, edited_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS email_time ON email(sent_at);
PRAGMA user_version=1;
'''


class Repository:
    def __init__(self, db_path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA busy_timeout=5000')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.connection.close()
            raise ValueError('Unsupported database schema version: %s' % version)
        self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def issue(self, email_id, code, detail):
        self.connection.execute('INSERT INTO parse_issue(email_id,code,detail) VALUES(?,?,?)',
                                (email_id, code, detail))

    def store_parse(self, email_id, mail, result, parser_version):
        c = self.connection
        keys = {tuple(row) for row in c.execute(
            'SELECT kind,passenger,order_no FROM ticket_version WHERE email_id=? AND active=1', (email_id,))}
        c.execute('UPDATE ticket_version SET active=0 WHERE email_id=?', (email_id,))
        c.execute('UPDATE parse_issue SET resolved=1 WHERE email_id=?', (email_id,))
        c.execute('DELETE FROM notification WHERE email_id=?', (email_id,))
        c.execute('UPDATE email SET kind=?,parser_version=? WHERE id=?',
                  (result['kind'], parser_version, email_id))
        for code, detail in result['issues']:
            self.issue(email_id, code, detail)
        if not result['tickets']:
            c.execute('INSERT INTO notification VALUES(?,?,?)',
                      (email_id, result['kind'], mail['body']))
        for ordinal, ticket in enumerate(result['tickets']):
            if not all(ticket[k] for k in ('kind', 'passenger', 'order_no')):
                # Cannot construct the agreed unique key. Raw email and issue remain available.
                continue
            key = tuple(ticket[k] for k in ('kind', 'passenger', 'order_no'))
            keys.add(key)
            c.execute('''INSERT INTO ticket_version(email_id,ordinal,kind,passenger,order_no,
                payload,business_time,parser_version,recorded_at) VALUES(?,?,?,?,?,?,?,?,?)''',
                      (email_id, ordinal, *key, dumps(ticket), mail['sent_at'], parser_version, now()))
        for key in keys:
            self.refresh(key)

    def refresh(self, key):
        c = self.connection
        existing = c.execute('SELECT * FROM ticket_event WHERE kind=? AND passenger=? AND order_no=?', key).fetchone()
        if existing and existing['manual']:
            return
        versions = c.execute('''SELECT * FROM ticket_version
          WHERE kind=? AND passenger=? AND order_no=? AND active=1
          ORDER BY business_time DESC, id ASC''', key).fetchall()
        if not versions:
            if existing:
                # Preserve IDs referenced by manual edit history; this path only removes auto rows.
                c.execute('DELETE FROM ticket_event WHERE id=?', (existing['id'],))
            return
        newest = versions[0]
        if not newest['business_time']:
            self.issue(newest['email_id'], 'missing_business_time', '邮件时间缺失，不能可靠比较业务先后')
        peers = [v for v in versions if v['business_time'] == newest['business_time']]
        def business(v):
            return {k: value for k, value in json.loads(v['payload']).items()
                    if k not in ('notes', 'raw_ticket')}
        if any(business(v) != business(newest) for v in peers):
            self.issue(newest['email_id'], 'same_time_conflict', '同业务键、同时间存在不同内容，请人工核对；保留先入库版本')
        c.execute('''INSERT INTO ticket_event(kind,passenger,order_no,payload,business_time,email_id)
          VALUES(?,?,?,?,?,?) ON CONFLICT(kind,passenger,order_no) DO UPDATE SET
          payload=excluded.payload,business_time=excluded.business_time,email_id=excluded.email_id''',
                  (*key, newest['payload'], newest['business_time'], newest['email_id']))

    def query(self, filters=None):
        filters = filters or {}
        allowed = {'kind', 'passenger', 'order_no', 'train', 'date_from', 'date_to'}
        if set(filters) - allowed:
            raise ValueError('Unknown filters: %s' % (set(filters) - allowed))
        # Filter decoded records to avoid requiring SQLite's optional JSON extension.
        results = []
        for row in self.connection.execute('SELECT * FROM ticket_event'):
            item = json.loads(row['payload'])
            if any(item.get(k) != v for k, v in filters.items()
                   if k not in ('date_from', 'date_to') and v is not None):
                continue
            if filters.get('date_from') and item['travel_date'] < filters['date_from']:
                continue
            if filters.get('date_to') and (not item['travel_date'] or item['travel_date'] > filters['date_to']):
                continue
            item.update(id=row['id'], business_time=row['business_time'],
                        email_id=row['email_id'], manual=bool(row['manual']))
            results.append(item)
        return sorted(results, key=lambda r: (r['travel_date'] or '9999', r['departure_time'], r['train'], r['passenger'], r['kind']))

    def update_ticket(self, ticket_id, changes, reason):
        from .parser import FIELDS
        if not reason.strip():
            raise ValueError('Correction reason is required')
        editable = set(FIELDS) - {'raw_ticket'}
        if set(changes) - editable:
            raise ValueError('Business keys and raw text are not editable')
        if not all(isinstance(v, str) for v in changes.values()):
            raise ValueError('Correction values must be strings')
        if 'passenger' in changes:
            raise ValueError('Business key passenger cannot be changed')
        c = self.connection
        with c:
            row = c.execute('SELECT * FROM ticket_event WHERE id=?', (ticket_id,)).fetchone()
            if not row:
                raise KeyError(ticket_id)
            updated = json.loads(row['payload'])
            updated.update(changes)
            if updated.get('travel_date'):
                datetime.strptime(updated['travel_date'], '%Y-%m-%d')
            if updated.get('departure_time'):
                datetime.strptime(updated['departure_time'], '%H:%M')
            from decimal import Decimal
            for k in ('fare', 'refund_fee', 'refund_amount'):
                if updated.get(k) and (not Decimal(updated[k]).is_finite() or Decimal(updated[k]) < 0):
                    raise ValueError('Invalid amount: ' + k)
            c.execute('INSERT INTO edit_history VALUES(NULL,?,?,?,?,?)',
                      (ticket_id, row['payload'], dumps(updated), reason, now()))
            c.execute('UPDATE ticket_event SET payload=?,manual=1 WHERE id=?', (dumps(updated), ticket_id))
