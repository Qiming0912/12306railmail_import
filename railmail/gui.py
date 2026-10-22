"""Tk/ttk desktop interface. Run with: python -m railmail.gui."""
import argparse
import json
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .service import RailMail

TICKET_COLUMNS = [
    ('kind', '类型', 70), ('passenger', '乘车人', 85),
    ('travel_date', '乘车日期', 105), ('departure_time', '时间', 65),
    ('train', '车次', 75), ('origin', '出发站', 90),
    ('destination', '到达站', 90), ('fare', '票价（元）', 85),
    ('seat', '车厢座位', 120), ('order_no', '订单号', 135),
    ('state', '状态', 90),
]
EDIT_FIELDS = [
    ('travel_date', '乘车日期（YYYY-MM-DD）'),
    ('departure_time', '发车时间（HH:MM）'),
    ('train', '车次'), ('origin', '出发站'), ('destination', '到达站'),
    ('seat', '车厢座位'), ('seat_class', '席别'), ('ticket_kind', '票种'),
    ('fare', '票价（元）'), ('refund_fee', '退票费（元）'),
    ('refund_amount', '应退票款（元）'), ('gate', '检票口'), ('notes', '备注'),
]
ISSUE_NAMES = {
    'empty_body': '空邮件', 'inferred_year': '年份补全',
    'invalid_date': '日期无效', 'ticket_count': '票数不符',
    'partial_parse': '可能漏票', 'total_amount': '总票款不符',
    'missing_field': '字段缺失', 'refund_amount': '退款不符',
    'invalid_amount': '金额格式无效', 'unparsed': '未识别车票',
    'missing_business_time': '邮件时间不明',
    'same_time_conflict': '同时间内容冲突', 'decode_error': '解码失败',
}


def validated_filters(values):
    """GUI query input validation, also usable without a display."""
    filters = {key: value.strip() for key, value in values.items() if value.strip()}
    for field in ('date_from', 'date_to'):
        if field in filters:
            dt = datetime.strptime(filters[field], '%Y-%m-%d')
            if dt.strftime('%Y-%m-%d') != filters[field]:
                raise ValueError('日期请使用 YYYY-MM-DD 格式')
    if filters.get('date_from') and filters.get('date_to'):
        if filters['date_from'] > filters['date_to']:
            raise ValueError('起始日期不能晚于结束日期')
    return filters


def validate_changes(original, values):
    changes = {key: value.strip() for key, value in values.items()
               if value.strip() != original.get(key, '')}
    for field, fmt in (('travel_date', '%Y-%m-%d'), ('departure_time', '%H:%M')):
        if field in changes and changes[field]:
            dt = datetime.strptime(changes[field], fmt)
            if dt.strftime(fmt) != changes[field]:
                raise ValueError('日期或时间格式无效')
    from decimal import Decimal, InvalidOperation
    for field in ('fare', 'refund_fee', 'refund_amount'):
        if changes.get(field):
            try:
                number = Decimal(changes[field])
                if not number.is_finite() or number < 0:
                    raise ValueError('金额必须是非负数')
            except InvalidOperation as exc:
                raise ValueError('金额格式无效') from exc
    return changes


class EditDialog(tk.Toplevel):
    def __init__(self, parent, record):
        super().__init__(parent)
        self.title('修正票务记录')
        self.transient(parent)
        self.resizable(True, False)
        self.result = None
        self.record = record
        self.variables = {}
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='%s · %s · %s' % (
            record['kind'], record['passenger'], record['order_no']),
            font=('TkDefaultFont', 11, 'bold')).grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 6))
        ttk.Label(frame, text='业务标识只读。保存后自动导入和重新解析将保留人工修正。').grid(
            row=1, column=0, columnspan=4, sticky='w', pady=(0, 12))
        for i, (key, label) in enumerate(EDIT_FIELDS):
            row, pair = divmod(i, 2)
            col = pair * 2
            ttk.Label(frame, text=label).grid(row=row+2, column=col, sticky='w', padx=(0, 8), pady=5)
            var = tk.StringVar(value=record.get(key, ''))
            self.variables[key] = var
            ttk.Entry(frame, textvariable=var, width=24).grid(
                row=row+2, column=col+1, sticky='ew', padx=(0, 12), pady=5)
        reason_row = 2 + (len(EDIT_FIELDS) + 1) // 2
        ttk.Label(frame, text='修正原因（必填）').grid(row=reason_row, column=0, sticky='w', pady=10)
        self.reason = tk.StringVar()
        ttk.Entry(frame, textvariable=self.reason).grid(row=reason_row, column=1, columnspan=3, sticky='ew', padx=(0, 12))
        buttons = ttk.Frame(frame)
        buttons.grid(row=reason_row+1, column=0, columnspan=4, sticky='e', pady=(12, 0))
        ttk.Button(buttons, text='取消', command=self.destroy).pack(side='left', padx=6)
        ttk.Button(buttons, text='保存修正', command=self.save).pack(side='left')
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        self.bind('<Escape>', lambda event: self.destroy())
        self.grab_set()

    def save(self):
        try:
            if not self.reason.get().strip():
                raise ValueError('请填写修正原因')
            changes = validate_changes(self.record, {k: v.get() for k, v in self.variables.items()})
            if not changes:
                raise ValueError('没有修改任何字段')
            self.result = changes, self.reason.get().strip()
            self.destroy()
        except ValueError as exc:
            messagebox.showerror('无法保存', str(exc), parent=self)


class RailMailWindow(ttk.Frame):
    def __init__(self, root, db_path=None):
        super().__init__(root)
        self.root = root
        self.pack(fill='both', expand=True)
        root.title('车票邮件整理')
        root.geometry('1180x800')
        root.minsize(920, 680)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.db_path = None
        self.events = queue.Queue()
        self.worker = None
        self.busy = False
        self.after_id = None
        self.rows = {}
        self.issues = {}
        self.notices = {}
        self.edit_buttons = []
        self.task_buttons = []
        self.filters = {}
        self.applied_filters = {}
        self.pending_sources = []
        self.db_label = tk.StringVar(value='尚未打开数据库')
        self.status = tk.StringVar(value='请选择或新建数据库')
        self._build()
        self.after_id = root.after(100, self.poll)
        if db_path:
            self.open_database(db_path)

    def _button(self, parent, text, command, **pack):
        button = ttk.Button(parent, text=text, command=command)
        button.pack(**pack)
        return button

    @staticmethod
    def _table(parent, columns, height=8):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings',
                            selectmode='browse', height=height)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=55, stretch=False)
        y = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        x = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.grid(row=0, column=0, sticky='nsew')
        y.grid(row=0, column=1, sticky='ns')
        x.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    @staticmethod
    def _text(parent):
        frame = ttk.Frame(parent, padding=8)
        frame.pack(fill='both', expand=True)
        text = tk.Text(frame, wrap='word', height=6, state='disabled', relief='flat', padx=6, pady=6)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        return text

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', value)
        widget.configure(state='disabled')

    def _build(self):
        top = ttk.Frame(self, padding=(14, 10))
        top.pack(fill='x')
        ttk.Label(top, text='当前数据库：').pack(side='left')
        ttk.Label(top, textvariable=self.db_label).pack(side='left', fill='x', expand=True)
        self.task_buttons += [self._button(top, '新建数据库', self.new_database, side='right', padx=4),
                              self._button(top, '打开数据库', self.choose_database, side='right', padx=4)]
        ttk.Separator(self).pack(fill='x')
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        nav = ttk.Frame(body, padding=12)
        nav.pack(side='left', fill='y')
        self.content = ttk.Frame(body, padding=12)
        self.content.pack(side='left', fill='both', expand=True)
        self.pages = {}
        self.page_buttons = {}
        for key, label in [('records', '票务记录'), ('import', '导入邮件'), ('issues', '待核对'), ('notices', '其他通知')]:
            self.page_buttons[key] = self._button(nav, label, lambda k=key: self.show_page(k), fill='x', pady=5)
            self.pages[key] = ttk.Frame(self.content)
        ttk.Label(nav, text='本地 SQLite\n数据保存在本机', justify='left').pack(side='bottom', pady=12)
        self._records_page()
        self._import_page()
        self._issues_page()
        self._notices_page()
        ttk.Separator(self).pack(fill='x')
        ttk.Label(self, textvariable=self.status, padding=(14, 7), anchor='w').pack(fill='x')
        self.show_page('records')

    def show_page(self, page):
        for frame in self.pages.values():
            frame.pack_forget()
        self.pages[page].pack(fill='both', expand=True)
        for key, button in self.page_buttons.items():
            button.state(['pressed'] if key == page else ['!pressed'])

    def _records_page(self):
        parent = self.pages['records']
        head = ttk.Frame(parent)
        head.pack(fill='x', pady=(0, 10))
        ttk.Label(head, text='票务记录', font=('TkDefaultFont', 15, 'bold')).pack(side='left')
        self.task_buttons.append(self._button(head, '导出当前筛选 CSV', self.export_csv, side='right'))
        form = ttk.LabelFrame(parent, text='筛选条件', padding=8)
        form.pack(fill='x')
        for index, (key, label) in enumerate([
            ('passenger', '乘车人'), ('kind', '票务类型'), ('train', '车次'),
            ('order_no', '订单号'), ('date_from', '起始日期'), ('date_to', '结束日期')]):
            r, col = divmod(index, 3)
            ttk.Label(form, text=label).grid(row=r, column=col*2, sticky='w', padx=(0, 6), pady=4)
            var = tk.StringVar()
            self.filters[key] = var
            if key == 'kind':
                entry = ttk.Combobox(form, textvariable=var, state='readonly', width=17,
                                     values=['', '购票', '改签', '退票', '候补兑现'])
            else:
                entry = ttk.Entry(form, textvariable=var, width=19)
                entry.bind('<Return>', lambda event: self.apply_filters())
            entry.grid(row=r, column=col*2+1, sticky='ew', padx=(0, 10), pady=4)
            form.columnconfigure(col*2+1, weight=1)
        actions = ttk.Frame(parent)
        actions.pack(fill='x', pady=8)
        ttk.Label(actions, text='日期：YYYY-MM-DD；乘车人、订单号、车次按完整值匹配').pack(side='left')
        self._button(actions, '重置', self.reset_filters, side='right', padx=5)
        self._button(actions, '查询', self.apply_filters, side='right')
        self.record_count = tk.StringVar(value='0 条记录')
        ttk.Label(parent, textvariable=self.record_count).pack(anchor='w', pady=(0, 6))
        split = ttk.Panedwindow(parent, orient='vertical')
        split.pack(fill='both', expand=True)
        upper, lower = ttk.Frame(split), ttk.Frame(split)
        split.add(upper, weight=3)
        split.add(lower, weight=2)
        self.ticket_tree = self._table(upper, TICKET_COLUMNS)
        self.ticket_tree.bind('<<TreeviewSelect>>', self.select_ticket)
        self.ticket_tree.bind('<Double-1>', lambda event: self.edit_selected())
        tabs = ttk.Notebook(lower)
        tabs.pack(fill='both', expand=True, pady=(10, 0))
        texts = []
        for label in ['票务详情', '邮件正文', '历史版本 / 人工修正']:
            tab = ttk.Frame(tabs)
            tabs.add(tab, text=label)
            texts.append(self._text(tab))
        self.details, self.source_text, self.history_text = texts
        self.edit_buttons.append(self._button(lower, '修正选中记录', self.edit_selected, anchor='e', pady=(5, 0)))

    def _import_page(self):
        parent = self.pages['import']
        ttk.Label(parent, text='导入邮件', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w', pady=(0, 8))
        ttk.Label(parent, text='支持 EML、ZIP 及文件夹递归读取。只保存必要邮件头和精简正文，不保存完整 EML。', wraplength=780).pack(anchor='w')
        actions = ttk.Frame(parent)
        actions.pack(fill='x', pady=12)
        for label, command in [('选择 EML / ZIP', self.add_files), ('选择文件夹', self.add_folder), ('移除选中', self.remove_source), ('清空列表', self.clear_sources)]:
            self.task_buttons.append(self._button(actions, label, command, side='left', padx=(0, 8)))
        self.source_list = tk.Listbox(parent, height=5, selectmode='extended')
        self.source_list.pack(fill='x')
        self.task_buttons.append(self._button(parent, '开始导入', self.start_import, anchor='e', pady=10))
        self.progress = ttk.Progressbar(parent, mode='indeterminate')
        self.progress.pack(fill='x')
        self.import_status = tk.StringVar(value='尚未导入')
        ttk.Label(parent, textvariable=self.import_status, wraplength=780).pack(anchor='w', pady=8)
        ttk.Label(parent, text='导入批次与来源错误', font=('TkDefaultFont', 11, 'bold')).pack(anchor='w', pady=(12, 6))
        self.batch_tree = self._table(parent, [('id','批次',60), ('started_at','开始时间（UTC）',220),
            ('finished_at','结束时间（UTC）',220), ('summary','处理统计',550)], height=5)
        self.import_errors = self._text(parent)
        tools = ttk.Frame(parent)
        tools.pack(fill='x', pady=(8, 0))
        self.task_buttons.append(self._button(tools, '清理旧库并压缩空间', self.compact, side='left'))
        self.task_buttons.append(self._button(tools, '重新解析数据库全部邮件', self.reparse, side='right'))

    def _issues_page(self):
        parent = self.pages['issues']
        ttk.Label(parent, text='待核对', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w', pady=(0, 8))
        ttk.Label(parent, text='选中问题查看精简正文。标记已处理只关闭问题，不会自动修改票务数据。', wraplength=780).pack(anchor='w', pady=(0, 8))
        self.issue_tree = self._table(parent, [('code','问题',150), ('email_id','邮件ID',70), ('detail','说明',650)], height=6)
        self.issue_tree.bind('<<TreeviewSelect>>', self.select_issue)
        self.issue_text = self._text(parent)
        actions = ttk.Frame(parent)
        actions.pack(fill='x', pady=8)
        self.edit_buttons.append(self._button(actions, '查看关联票务', self.related_tickets, side='left'))
        self.task_buttons.append(self._button(actions, '重新解析此邮件', self.reparse_selected, side='left', padx=8))
        self.edit_buttons.append(self._button(actions, '标记已处理', self.resolve_selected, side='right'))

    def _notices_page(self):
        parent = self.pages['notices']
        ttk.Label(parent, text='其他通知', font=('TkDefaultFont', 15, 'bold')).pack(anchor='w', pady=(0, 8))
        ttk.Label(parent, text='停运、候补退单、账户通知及未形成完整车票的邮件。').pack(anchor='w', pady=(0, 8))
        self.notice_tree = self._table(parent, [('kind','类型',100), ('subject','邮件主题',360), ('sent_at','邮件时间（UTC）',230)], height=7)
        self.notice_tree.bind('<<TreeviewSelect>>', self.select_notice)
        self.notice_text = self._text(parent)

    def choose_database(self):
        path = filedialog.askopenfilename(parent=self.root, title='打开 SQLite 数据库',
            filetypes=[('SQLite 数据库', '*.sqlite *.sqlite3 *.db'), ('所有文件', '*')])
        if path:
            self.open_database(path)

    def new_database(self):
        path = filedialog.asksaveasfilename(parent=self.root, title='新建 SQLite 数据库',
            defaultextension='.sqlite', initialfile='tickets.sqlite',
            filetypes=[('SQLite 数据库', '*.sqlite')], confirmoverwrite=False)
        if path:
            if Path(path).exists():
                messagebox.showerror('文件已存在', '请选择新的文件名；已有数据库请使用“打开数据库”。', parent=self.root)
                return
            self.open_database(path)

    def open_database(self, path):
        if self.busy:
            return
        previous = self.db_path
        try:
            candidate = str(Path(path).resolve())
            # Inspect existing nonempty files before the core creates tables.
            if Path(candidate).is_file() and Path(candidate).stat().st_size:
                import sqlite3
                conn = sqlite3.connect(candidate)
                try:
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if tables and not {'email', 'ticket_event', 'ticket_version'}.issubset(tables):
                        raise ValueError('该文件不是 RailMail 数据库，请选择其他文件')
                finally:
                    conn.close()
            with RailMail(candidate):
                pass
            self.db_path = candidate
            self.applied_filters = {}
            for var in self.filters.values():
                var.set('')
            self.refresh_all()
            self.db_label.set(candidate)
            self.status.set('数据库已打开：' + candidate)
        except Exception as exc:
            self.db_path = previous
            self.error(exc)

    def require_db(self):
        if not self.db_path:
            messagebox.showinfo('请选择数据库', '请先打开或新建数据库。', parent=self.root)
            return False
        return True

    def error(self, exc):
        self.status.set('操作失败：' + str(exc))
        messagebox.showerror('操作失败', str(exc), parent=self.root)

    @staticmethod
    def _replace_rows(tree, rows):
        tree.delete(*tree.get_children())
        for iid, values in rows:
            tree.insert('', 'end', iid=str(iid), values=values)

    def apply_filters(self):
        if not self.require_db():
            return
        try:
            filters = validated_filters({k: v.get() for k, v in self.filters.items()})
            self.applied_filters = filters
            self.refresh_tickets()
        except Exception as exc:
            self.error(exc)

    def reset_filters(self):
        for var in self.filters.values():
            var.set('')
        if self.db_path:
            self.applied_filters = {}
            self.refresh_tickets()

    def refresh_tickets(self):
        old = self.ticket_tree.selection()
        with RailMail(self.db_path) as app:
            records = app.query_tickets(self.applied_filters)
        self.rows = {str(r['id']): r for r in records}
        self._replace_rows(self.ticket_tree, [(r['id'], [
            ('人工修正' if r['manual'] else '邮件记录') if k == 'state' else r.get(k, '')
            for k, _, _ in TICKET_COLUMNS]) for r in records])
        self.record_count.set('%d 条记录 · 当前筛选 · 按乘车日期排序' % len(records))
        if records:
            iid = old[0] if old and old[0] in self.rows else str(records[0]['id'])
            self.ticket_tree.selection_set(iid)
            self.select_ticket()
        else:
            for text in (self.details, self.source_text, self.history_text):
                self._set_text(text, '')

    def refresh_all(self):
        self.refresh_tickets()
        with RailMail(self.db_path) as app:
            self.issues = {str(r['id']): r for r in app.list_issues()}
            self._replace_rows(self.issue_tree, [(key, [ISSUE_NAMES.get(r['code'], r['code']), r['email_id'], r['detail']])
                                                  for key, r in self.issues.items()])
            self.page_buttons['issues'].configure(text='待核对（%d）' % len(self.issues))
            self.notices = {}
            for notice in app.list_notifications():
                mail = app.get_email(notice['email_id'])
                self.notices[str(notice['email_id'])] = dict(notice, subject=mail['subject'], sent_at=mail['sent_at'])
            self._replace_rows(self.notice_tree, [(k, [r['kind'], r['subject'], r['sent_at']]) for k, r in self.notices.items()])
            batches = app.repository.connection.execute('SELECT * FROM import_batch ORDER BY id DESC').fetchall()
            self._replace_rows(self.batch_tree, [(r['id'], [r['id'],r['started_at'],r['finished_at'] or '未完成',r['summary'] or '']) for r in batches])
            errors = app.repository.connection.execute('SELECT batch_id,source,error FROM email_source WHERE error IS NOT NULL ORDER BY id DESC').fetchall()
            self._set_text(self.import_errors, '\n'.join('批次%s  %s\n%s' % tuple(r) for r in errors) or '没有来源读取错误')
        self._set_text(self.issue_text, '请选择待核对问题')
        self._set_text(self.notice_text, '请选择通知查看原文')

    def _mail_text(self, app, email_id):
        mail = app.get_email(email_id)
        sources = app.repository.connection.execute('SELECT DISTINCT source FROM email_source WHERE email_id=?', (email_id,)).fetchall()
        return '邮件ID：%s\n主题：%s\n邮件原始时间：%s\n来源：\n%s\n\n%s' % (
            email_id, mail['subject'], mail['raw_date'], '\n'.join(r[0] for r in sources), mail['body'] or '（邮件正文为空）')

    def select_ticket(self, event=None):
        selected = self.ticket_tree.selection()
        if not selected or selected[0] not in self.rows:
            return
        record = self.rows[selected[0]]
        try:
            labels = dict((k, label) for k, label, _ in TICKET_COLUMNS)
            labels.update(seat_class='席别', ticket_kind='票种', refund_fee='退票费', refund_amount='应退款',
                          gate='检票口', notes='备注', business_time='业务时间（UTC）')
            self._set_text(self.details, '\n'.join('%s：%s' % (label, record.get(k, '')) for k, label in labels.items() if k != 'state'))
            with RailMail(self.db_path) as app:
                self._set_text(self.source_text, self._mail_text(app, record['email_id']))
                versions = app.ticket_history(record['id'])
                lines = []
                for version in versions:
                    data = json.loads(version['payload'])
                    lines.append('%s · 邮件%s · %s\n%s %s %s → %s · %s\n%s' % (
                        version['business_time'] or '时间未知',version['email_id'],
                        '有效解析' if version['active'] else '已被重新解析替代',
                        data['travel_date'],data['train'],data['origin'],data['destination'],data['seat'],data.get('notes','')))
                edits = app.repository.connection.execute('SELECT * FROM edit_history WHERE ticket_id=? ORDER BY id', (record['id'],)).fetchall()
                for edit in edits:
                    before, after = json.loads(edit['before_payload']), json.loads(edit['after_payload'])
                    changed = '\n'.join('%s：%s → %s' % (key,before.get(key,''),value) for key,value in after.items() if value != before.get(key))
                    lines.append('人工修正 %s\n原因：%s\n%s' % (edit['edited_at'],edit['reason'],changed))
                self._set_text(self.history_text, '\n\n'.join(lines))
        except Exception as exc:
            self.error(exc)

    def edit_selected(self):
        if self.busy or not self.require_db():
            return
        ids = self.ticket_tree.selection()
        if not ids:
            return
        dialog = EditDialog(self.root, self.rows[ids[0]])
        self.root.wait_window(dialog)
        if dialog.result:
            try:
                with RailMail(self.db_path) as app:
                    app.update_ticket(int(ids[0]), *dialog.result)
                self.refresh_tickets()
                self.status.set('人工修正已保存；如有相关问题，请核对后标记已处理')
            except Exception as exc:
                self.error(exc)

    def select_issue(self, event=None):
        ids = self.issue_tree.selection()
        if ids and ids[0] in self.issues:
            try:
                with RailMail(self.db_path) as app:
                    issue = self.issues[ids[0]]
                    self._set_text(self.issue_text, issue['detail']+'\n\n'+self._mail_text(app, issue['email_id']))
            except Exception as exc:
                self.error(exc)

    def select_notice(self, event=None):
        ids = self.notice_tree.selection()
        if ids and ids[0] in self.notices:
            try:
                with RailMail(self.db_path) as app:
                    self._set_text(self.notice_text, self._mail_text(app, int(ids[0])))
            except Exception as exc:
                self.error(exc)

    def related_tickets(self):
        ids = self.issue_tree.selection()
        if not ids:
            return
        try:
            issue = self.issues[ids[0]]
            with RailMail(self.db_path) as app:
                keys = [tuple(r) for r in app.repository.connection.execute(
                    'SELECT DISTINCT kind,passenger,order_no FROM ticket_version WHERE email_id=?', (issue['email_id'],))]
                records = app.query_tickets()
            matches = [r for r in records if (r['kind'],r['passenger'],r['order_no']) in keys]
            if not matches:
                messagebox.showinfo('没有关联票务', '此邮件未形成完整票务记录，可先核对原文或重新解析。', parent=self.root)
                return
            self.reset_filters()
            self.show_page('records')
            self.ticket_tree.selection_set(str(matches[0]['id']))
            self.ticket_tree.see(str(matches[0]['id']))
            self.select_ticket()
            self.status.set('找到 %d 条关联票务，已定位第一条' % len(matches))
        except Exception as exc:
            self.error(exc)

    def resolve_selected(self):
        if self.busy:
            return
        ids = self.issue_tree.selection()
        if not ids:
            return
        if messagebox.askyesno('标记已处理', '确认此问题已核对？这不会修改车票信息。', parent=self.root):
            try:
                with RailMail(self.db_path) as app:
                    app.resolve_issue(int(ids[0]))
                self.refresh_all()
            except Exception as exc:
                self.error(exc)

    def add_files(self):
        paths = filedialog.askopenfilenames(parent=self.root, title='选择邮件或压缩包',
            filetypes=[('邮件与压缩包','*.eml *.zip'), ('所有文件','*')])
        self.add_sources(paths)

    def add_folder(self):
        path = filedialog.askdirectory(parent=self.root, title='选择邮件文件夹')
        if path:
            self.add_sources([path])

    def add_sources(self, paths):
        for path in paths:
            value = str(Path(path).resolve())
            if value not in self.pending_sources:
                self.pending_sources.append(value)
                self.source_list.insert('end', value)

    def remove_source(self):
        for index in reversed(self.source_list.curselection()):
            del self.pending_sources[index]
            self.source_list.delete(index)

    def clear_sources(self):
        self.pending_sources.clear()
        self.source_list.delete(0,'end')

    def run_task(self, label, function):
        if self.busy or not self.require_db():
            return
        self.busy = True
        for button in self.task_buttons + self.edit_buttons:
            button.state(['disabled'])
        self.progress.start(12)
        self.status.set(label+'…')
        db_path = self.db_path
        def work():
            try:
                with RailMail(db_path) as app:
                    result = function(app)
                self.events.put(('done', label, result))
            except Exception as exc:
                self.events.put(('error', label, str(exc)))
        self.worker = threading.Thread(target=work, name='railmail-task', daemon=False)
        self.worker.start()

    def start_import(self):
        if not self.pending_sources:
            messagebox.showinfo('没有来源', '请先选择邮件、ZIP 或文件夹。', parent=self.root)
            return
        paths = list(self.pending_sources)
        # Throttle worker progress to keep the GUI queue bounded for large imports.
        last = [0]
        def task(app):
            def progress(stats):
                if stats['seen'] - last[0] >= 10:
                    last[0] = stats['seen']
                    self.events.put(('progress', '', stats))
            return app.import_sources(paths, progress)
        self.run_task('导入邮件', task)

    def reparse(self):
        if not self.require_db():
            return
        if messagebox.askyesno('重新解析', '重新解析已存正文？旧库完整 EML 将清理，人工修正会保留，待核对问题将重新生成。', parent=self.root):
            self.run_task('重新解析', lambda app: {'reparsed':app.reparse_emails()})

    def compact(self):
        if not self.require_db():
            return
        if messagebox.askyesno('清理旧库并压缩', '将删除数据库内的完整 EML，仅保留温馨提示前的正文和必要邮件头，并压缩空间。\n\n此操作不会删除邮箱或磁盘上的邮件。请确认原始邮件已另行保留。', parent=self.root):
            self.run_task('清理数据库', lambda app: {'compacted':app.compact_database()})

    def reparse_selected(self):
        ids = self.issue_tree.selection()
        if ids:
            email_id = self.issues[ids[0]]['email_id']
            self.run_task('重新解析', lambda app: {'reparsed':app.reparse_emails([email_id])})

    def export_csv(self):
        if not self.require_db():
            return
        # Export uses exactly the visible query, not unsubmitted form edits.
        try:
            filters_in_form = validated_filters({k:v.get() for k,v in self.filters.items()})
        except ValueError as exc:
            self.error(exc)
            return
        if self.applied_filters != filters_in_form:
            messagebox.showinfo('请先查询', '筛选条件已修改，请先点击“查询”，再导出。', parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title='导出当前筛选结果',
            defaultextension='.csv', initialfile='tickets.csv', filetypes=[('CSV 文件','*.csv')])
        if path:
            filters = dict(self.applied_filters)
            self.run_task('导出 CSV', lambda app: {'exported':app.export_csv(path, filters, overwrite=True), 'path':path})

    def poll(self):
        try:
            for _ in range(50):
                kind, label, result = self.events.get_nowait()
                if kind == 'progress':
                    self.import_status.set('已读取 {seen}，新增 {imported}，重复 {duplicate}，读取失败 {errors}'.format(**result))
                else:
                    self.busy = False
                    self.progress.stop()
                    for button in self.task_buttons + self.edit_buttons:
                        button.state(['!disabled'])
                    if kind == 'error':
                        self.error(result)
                    else:
                        try:
                            self.refresh_all()
                            if 'seen' in result:
                                summary = '读取 {seen}；新增 {imported}；重复 {duplicate}；读取失败 {errors}；解析问题 {issues}'.format(**result)
                                self.import_status.set(summary)
                                self.status.set(label+'完成 · '+summary)
                            elif 'exported' in result:
                                self.status.set('已导出 %s 条记录到 %s' % (result['exported'],result['path']))
                            elif 'compacted' in result:
                                sizes = result['compacted']
                                self.status.set('清理完成：%s 封邮件；数据库 %.2f MB → %.2f MB' % (
                                    sizes['emails'], sizes['bytes_before']/1048576, sizes['bytes_after']/1048576))
                            else:
                                self.status.set('重新解析完成：%s 封邮件' % result['reparsed'])
                        except Exception as exc:
                            self.error(exc)
        except queue.Empty:
            pass
        self.after_id = self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            messagebox.showinfo('正在处理', '请等待当前任务结束后再关闭，已完成的邮件已提交保存。', parent=self.root)
            return
        if self.after_id:
            self.root.after_cancel(self.after_id)
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description='12306 邮件整理桌面界面')
    parser.add_argument('--db', help='启动时打开的 SQLite 数据库')
    args = parser.parse_args(argv)
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    RailMailWindow(root, args.db)
    root.mainloop()


if __name__ == '__main__':
    main()
