"""Pure email decoding and ticket parsing; no database or UI dependencies."""
import hashlib
import html
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

PARSER_VERSION = '2'
FIELDS = ('passenger', 'travel_date', 'departure_time', 'train', 'origin',
          'destination', 'seat', 'seat_class', 'ticket_kind', 'fare',
          'refund_fee', 'refund_amount', 'gate', 'notes', 'raw_ticket')
TICKET = re.compile(
    r'(?:\d+[.、]\s*)?(?P<passenger>[^\s，,。:：]+)[，,]\s*'
    r'(?:(?P<year>\d{4})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s*'
    r'(?P<departure_time>\d{1,2}:\d{2})\s*开[，,]\s*'
    r'(?P<origin>[^，,]+?)\s*[-—–－―]\s*(?P<destination>[^，,]+?)[，,]\s*'
    r'(?P<train>[A-Z]?\d+)次列车[，,]\s*'
    r'(?P<tail>.*?)(?=\s+\d+[.、]|为了确保|按购票|温馨提示|感谢您|$)')


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in ('br', 'p', 'div', 'tr', 'td', 'li', 'table'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        elif tag in ('p', 'div', 'tr', 'td', 'li', 'table'):
            self.parts.append(' ')

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def _find(pattern, text):
    match = re.search(pattern, text)
    return match.group(1) if match else ''


def clean_body(text):
    """Keep useful message text only; do not persist the boilerplate footer."""
    text = re.sub(r'\s+', ' ', html.unescape(text)).strip()
    return re.split(r'温\s*馨\s*提\s*示', text, maxsplit=1)[0].rstrip()


def _text(part):
    raw = part.get_payload(decode=True) or b''
    for encoding in (part.get_content_charset(), 'utf-8', 'gb18030', 'big5'):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeError, LookupError):
            continue
    return raw.decode('utf-8', errors='replace')


def decode_email(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    candidates = [p for p in message.walk()
                  if p.get_content_type() in ('text/plain', 'text/html')
                  and p.get_content_disposition() != 'attachment']
    candidates.sort(key=lambda p: p.get_content_type() != 'text/plain')
    body = ''
    for part in candidates:
        value = _text(part)
        if part.get_content_type() == 'text/html':
            extractor = _HTMLText()
            extractor.feed(value)
            value = ''.join(extractor.parts)
        value = re.sub(r'\s+', ' ', html.unescape(value)).strip()
        if value:
            body = clean_body(value)
            break
    stamp = ''
    try:
        dt = parsedate_to_datetime(str(message.get('Date', '')))
        # No local-machine timezone dependency; unzoned dates remain unresolved.
        if dt.tzinfo:
            stamp = dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError):
        pass
    subject = str(message.get('Subject', ''))
    return dict(raw_hash=hashlib.sha256(raw).hexdigest(), subject=subject,
                message_id=str(message.get('Message-ID', '')), sent_at=stamp,
                raw_date=str(message.get('Date', '')), body=body)


def parse_email(mail):
    """Return {kind, tickets, issues}; amounts are exact decimal strings."""
    body, subject = mail['body'], mail['subject']
    kinds = [('候补兑现', '兑现成功'), ('候补退单', '候补订单退单'),
             ('购票', '支付通知'), ('改签', '改签通知'),
             ('退票', '退票通知'), ('停运', '停运通知')]
    kind = next((k for k, marker in kinds if marker in subject), '其他')
    issues, tickets = [], []
    if not body:
        return dict(kind='空邮件', tickets=[], issues=[('empty_body', '邮件正文为空')])
    main = body.split('温馨提示')[0]
    order = _find(r'订单号码\s*([A-Z0-9]+)', main)
    business_year = _find(r'您于(\d{4})年', main)
    if not business_year:
        try:
            business_year = str(parsedate_to_datetime(mail['raw_date']).year)
        except (ValueError, TypeError, OverflowError):
            pass
    for match in TICKET.finditer(main):
        g = match.groupdict()
        tail = g['tail']
        parts = re.split('[，,]', tail)
        notes = []
        year = g['year'] or business_year
        travel_date = ''
        try:
            travel_date = datetime(int(year), int(g['month']), int(g['day'])).date().isoformat()
            departure_time = datetime.strptime(g['departure_time'], '%H:%M').strftime('%H:%M')
        except (ValueError, TypeError):
            departure_time = g['departure_time']
            issues.append(('invalid_date', match.group(0)))
        if not g['year']:
            notes.append('原文无年份，按业务办理/邮件年份%s补全，跨年行程需核对' % year)
            issues.append(('inferred_year', notes[-1]))
        kind_text = _find(r'(成人票|儿童票|学生票|残军票)', tail)
        if not kind_text:
            notes.append('原邮件未注明票种')
        ticket = dict(kind=kind, order_no=order,
                      passenger=re.sub(r'^\d+[.、]', '', g['passenger']),
                      travel_date=travel_date, departure_time=departure_time,
                      train=g['train'], origin=g['origin'].strip(),
                      destination=g['destination'].strip(), seat=parts[0].strip(),
                      seat_class=parts[1].strip() if len(parts) > 1 else '',
                      ticket_kind=kind_text, fare=_find(r'票价\s*([\d.]+)元', tail),
                      refund_fee=_find(r'退票费\s*([\d.]+)元', tail),
                      refund_amount=_find(r'应退票款\s*([\d.]+)元', tail),
                      gate=_find(r'检票口([^，,。\s]+)', tail),
                      notes='；'.join(notes), raw_ticket=match.group(0))
        tickets.append(ticket)
    expected = _find(r'(?:购买了|兑现了|改签车票)\s*(\d+)张', main)
    if expected and int(expected) != len(tickets):
        issues.append(('ticket_count', '声明%s张，解析%d张' % (expected, len(tickets))))
    dated_count = len(re.findall(r'\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2}\s*开', main))
    if dated_count != len(tickets):
        issues.append(('partial_parse', '带日期车票%d条，解析%d条' % (dated_count, len(tickets))))
    total = _find(r'票款共计\s*([\d.]+)元', main)
    try:
        if total and tickets and sum(Decimal(t['fare'] or '0') for t in tickets) != Decimal(total):
            issues.append(('total_amount', '邮件总票款与明细不一致'))
        for t in tickets:
            for field in ('passenger', 'order_no', 'travel_date', 'train', 'origin', 'destination', 'fare'):
                if not t[field]:
                    issues.append(('missing_field', '%s: %s' % (t['passenger'], field)))
            if all(t[k] for k in ('fare', 'refund_fee', 'refund_amount')):
                if Decimal(t['fare']) - Decimal(t['refund_fee']) != Decimal(t['refund_amount']):
                    issues.append(('refund_amount', t['passenger'] + ': 退款不符'))
    except InvalidOperation:
        issues.append(('invalid_amount', '金额格式无效'))
    if not tickets and kind in ('购票', '改签', '退票', '候补兑现'):
        issues.append(('unparsed', '票务邮件未识别到车票'))
    if tickets and not mail['sent_at']:
        issues.append(('missing_business_time', 'Date 缺失、无时区或无效，业务时间无法可靠排序'))
    return dict(kind=kind, tickets=tickets, issues=issues)
