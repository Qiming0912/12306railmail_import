# 12306 邮件整理核心

Python 3.9+，Windows / Linux，运行仅需标准库。GUI 与可执行文件打包尚未包含。

## 直接运行

解压后进入包含 `railmail` 子目录和本 README 的目录。Windows 通常使用 `python`，Linux 使用 `python3`：

```bash
python -m railmail --db tickets.sqlite import 邮件.zip
python -m railmail --db tickets.sqlite import /path/to/emails other.zip
python -m railmail --db tickets.sqlite query --passenger 戴启明
python -m railmail --db tickets.sqlite export -o tickets.csv
python -m railmail --db tickets.sqlite export --date-from 2015-01-01 --date-to 2015-12-31 -o 2015.csv
python -m railmail --db tickets.sqlite issues
python -m railmail --db tickets.sqlite notifications
python -m railmail --db tickets.sqlite history 1
python -m railmail --db tickets.sqlite reparse
python -m unittest discover -s tests -v
```

路径包含空格时用引号括起来。CSV 默认不覆盖，显式使用 `--force` 可覆盖。
运行命令不需要 pip 安装；可选使用 `pip install .` 安装命令行入口。

## Python API

```python
from railmail import RailMail

with RailMail('tickets.sqlite') as app:
    result = app.import_sources(['邮件.zip'], progress=lambda summary: print(summary))
    rows = app.query_tickets({'passenger': '戴启明'})
    count = app.export_csv('tickets.csv')
    # app.update_ticket(ticket_id, {'seat': '07车04B号'}, reason='核对原文')
    # app.reparse_emails([email_id])
```

每个工作线程创建自己的 RailMail 实例，不跨线程共享 SQLite 连接。
导入返回读取数、入库数、重复数、来源读取失败数及解析问题数。进度回调每处理一个来源调用一次。
回调应快速返回且不抛异常。单封邮件事务提交，不会因后续解析失败回滚已成功导入的邮件。
读取错误保存在 email_source.error，解析问题保存在 parse_issue。

## 数据规则

- `email` 保存原始 EML 字节、正文、邮件头。SHA-256 对原始字节查重；不同文件名不影响。重新导出改变邮件字节时可能保存为不同邮件，但不增加相同业务键的主表行。
- `ticket_event` 唯一键为 **kind + passenger + order_no**，三字段都非空；SQL 查询可直接使用这三个字段。
- `ticket_version` 保存历次解析快照与来源邮件。最新主表按邮件 Date 转 UTC 后比较，后导入的旧邮件不能覆盖新版本。
- 同业务键、同邮件时间却有不同票务内容，保留先入库版本并产生 `same_time_conflict` 问题；请人工核对。
- 主表是该业务键的最新状态，不是原始邮件逐条明细。历史完整保留；原始邮件、邮件来源和业务快照可关联追溯。
- 人工修正保留修改前后值与原因，主表标记 manual=1；后续导入及重新解析不会覆盖人工修正。
- 业务键暂不支持直接编辑，避免错误合并；人工修正支持日期、时间、车次、区间、座位、票价等字段。
- 无乘车人或订单号的记录不进入票务主表；原始邮件及问题仍保留。候补退单、停运、账户通知等进入 notification。
- 日期缺少年份时按正文业务办理年份、其次邮件年份补全，产生待核对问题；跨年行程可能需要人工修正。
- 邮件时间缺失时不使用本机时间代替；产生问题，不能可靠自动比较先后。
- 不推断票种和检票口，不将购票等同于实际乘车。
- 解析失败的邮件保留原始字节。重新解析会将旧快照置为非活动，并保留旧快照；原有问题置为已处理，再生成当前问题。
- 金额以十进制字符串保存，用 Decimal 核对；不采用浮点数计算。
- 数据库 schema version=1，未知版本拒绝打开，避免误修改未来数据库。

## 关键 SQL

```sql
SELECT * FROM ticket_event WHERE kind='购票' AND passenger='戴启明' AND order_no='EC04731598';
SELECT * FROM ticket_version WHERE kind='改签' AND passenger='戴启明' AND order_no='EC04731598' ORDER BY business_time;
SELECT * FROM parse_issue WHERE resolved=0;
```

详细票务字段存储在 payload JSON 中。Python 查询接口不依赖 SQLite JSON 扩展。
CSV 导出主表，按乘车日期、发车时间排序，无日期的放末尾。通知不混入车票 CSV，可通过 API 单独查看。

## 范围与限制

支持 EML、ZIP 和文件夹递归读取，不解压 ZIP 到文件系统。单封邮件上限默认 20 MiB。
暂不支持 PST、MSG、MBOX、加密 ZIP、嵌套压缩包和自动关联停运到具体车票。
只针对已见的新旧中文 12306 通知格式；未知格式进入待核对，而不是猜测字段。
未提供数据库自动备份与 GUI。数据库包含乘车信息与原邮件，请按本地业务数据管理。
