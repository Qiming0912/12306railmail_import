# 12306 邮件整理核心

Python 3.9+，Windows / Linux，核心仅需标准库。提供 Tkinter/ttk 桌面 GUI，以及 Windows x64 EXE 的构建脚本。

## Windows EXE

Windows x64 免安装版为 `RailMail.exe`，无需安装 Python。解压发布包后双击运行，先新建或打开数据库，再导入邮件。
SQLite 数据库由用户选择保存位置，独立于程序文件。更新 EXE 不会更改已有数据库；请不要将数据库放在程序的临时解压目录。
当前版本尚未记住上次打开的数据库，下次启动需要重新选择。没有安装器、自动更新或代码签名。

在 Windows 上自行构建（建议 Python 3.12 x64，包含 Tkinter）：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1
# 先生成文件夹版本做排查
powershell -ExecutionPolicy Bypass -File tools/build_windows.ps1 -OneDir
```

构建脚本会在项目内创建独立 `.build-venv`，构建依赖固定在 `requirements-build.txt`。
默认输出 `dist/RailMail.exe`；`-OneDir` 输出 `dist/RailMail/RailMail.exe`，文件夹版需携带整个目录。

可对构建后的程序进行自检，生成指定位置的 JSON 报告：

```powershell
& .\dist\RailMail.exe --self-test .\build\self-test.json
```

自检使用临时生成的虚构邮件，检查窗口组件、SQLite、后台导入、重复导入、筛选和 CSV 导出，不读取用户数据库。

## 启动桌面界面

在仓库根目录运行：

```bash
python -m railmail.gui
# 或启动时直接打开数据库
python -m railmail.gui --db tickets.sqlite
```

Windows 官方 Python 安装程序通常已包含 Tkinter。Linux 若缺少该组件，需要安装对应发行版的 Tkinter 系统包（例如 Debian/Ubuntu 的 `python3-tk`），之后使用 `python3 -m railmail.gui`。
GUI 不需要 PySide6 或其他第三方 Python 包；安装本项目后也可使用 `railmail-gui` 命令。

界面功能：

- 顶部打开/新建数据库；打开前检查已有数据库的基本表结构，避免误用其他程序的数据文件。
- 票务记录：按乘车人、类型、订单号、车次及日期范围查询，按乘车日期排序；查看详情、来源原文、历史快照和人工修正。
- 修正记录：业务键只读；可修改日期、区间、座位、金额等，修正原因必填，沿用核心的人工保护规则。
- 导入邮件：批量选择 EML/ZIP 或文件夹，后台处理，显示新增、重复及错误统计；查询导入批次和来源读取错误。
- 待核对：查看问题及精简后的邮件正文、跳转关联票务、重新解析单封邮件、标记已处理。修正记录与关闭问题是两个独立步骤。
- 其他通知：查看停运、候补退单、账户和空邮件等通知。
- 导出 CSV：导出当前已查询的筛选结果；修改筛选条件后必须先查询，避免导出范围与可见表格不一致。
- 支持后台重新解析全部邮件。任务运行期间禁止切换数据库或关闭窗口，需等待结束。
- 0.1.1 起不保存完整 EML，正文在“温馨提示”处截断。导入页可“清理旧库并压缩空间”，删除旧库完整 EML 副本并执行 SQLite VACUUM，邮箱和磁盘邮件不受影响。

乘车人、车次、订单号按完整值匹配；日期使用 `YYYY-MM-DD`。界面中的邮件时间和历史时间明确显示为 UTC。
暂不含任务取消、地图、分页、多库合并、批量修正、业务键修改及人工保护解除。

## 直接运行

解压后进入包含 `railmail` 子目录和本 README 的目录。Windows 通常使用 `python`，Linux 使用 `python3`：

```bash
python -m railmail --db tickets.sqlite import 邮件.zip
python -m railmail --db tickets.sqlite import /path/to/emails other.zip
python -m railmail --db tickets.sqlite query --passenger 张三
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
    rows = app.query_tickets({'passenger': '张三'})
    count = app.export_csv('tickets.csv')
    # app.update_ticket(ticket_id, {'seat': '07车04B号'}, reason='核对原文')
    # app.reparse_emails([email_id])
```

每个工作线程创建自己的 RailMail 实例，不跨线程共享 SQLite 连接。
导入返回读取数、入库数、重复数、来源读取失败数及解析问题数。进度回调每处理一个来源调用一次。
回调应快速返回且不抛异常。单封邮件事务提交，不会因后续解析失败回滚已成功导入的邮件。
读取错误保存在 email_source.error，解析问题保存在 parse_issue。

## 数据规则

- `email` 只保存必要邮件头、精简正文和原始文件的 SHA-256 指纹，不保存完整 EML。为兼容旧数据库，`raw` 列保留但写入空字节。正文在第一个“温馨提示”标题前截断，包含分隔空格的标题也能识别；没有此标题则保留全部正文。不同文件名不影响按指纹查重。
- `ticket_event` 唯一键为 **kind + passenger + order_no**，三字段都非空；SQL 查询可直接使用这三个字段。
- `ticket_version` 保存历次解析快照与来源邮件。最新主表按邮件 Date 转 UTC 后比较，后导入的旧邮件不能覆盖新版本。
- 同业务键、同邮件时间却有不同票务内容，保留先入库版本并产生 `same_time_conflict` 问题；请人工核对。
- 主表是该业务键的最新状态，不是邮件逐条明细。票务历史保留；精简邮件、邮件来源和业务快照可关联追溯。完整原邮件由用户在邮箱或磁盘中保留。
- 人工修正保留修改前后值与原因，主表标记 manual=1；后续导入及重新解析不会覆盖人工修正。
- 业务键暂不支持直接编辑，避免错误合并；人工修正支持日期、时间、车次、区间、座位、票价等字段。
- 无乘车人或订单号的记录不进入票务主表；精简正文、来源及问题仍保留。候补退单、停运、账户通知等进入 notification。
- 日期缺少年份时按正文业务办理年份、其次邮件年份补全，产生待核对问题；跨年行程可能需要人工修正。
- 邮件时间缺失时不使用本机时间代替；产生问题，不能可靠自动比较先后。
- 不推断票种和检票口，不将购票等同于实际乘车。
- 重新解析基于已存精简正文，旧快照置为非活动但保留，旧问题置为已处理并重新生成。旧库中的完整 EML 会在重新解析后清除。无法解码的邮件只保存指纹及错误，需从邮箱重新取得原始文件；本版本不支持从数据库恢复原始 EML。
- 金额以十进制字符串保存，用 Decimal 核对；不采用浮点数计算。
- 数据库 schema version=1，未知版本拒绝打开，避免误修改未来数据库。

## 关键 SQL

```sql
SELECT * FROM ticket_event WHERE kind='购票' AND passenger='张三' AND order_no='E000000001';
SELECT * FROM ticket_version WHERE kind='改签' AND passenger='张三' AND order_no='E000000001' ORDER BY business_time;
SELECT * FROM parse_issue WHERE resolved=0;
```

旧库清理和压缩也可以使用命令行：

```bash
python -m railmail --db tickets.sqlite compact
```

清理会重新生成待核对问题并保留人工修正。仅截短字段或清空 BLOB 不一定减小文件，`compact` 会再执行 `VACUUM` 释放空闲页。清理前请自行备份数据库；不再保存的原邮件内容不能从库中恢复。

详细票务字段存储在 payload JSON 中。Python 查询接口不依赖 SQLite JSON 扩展。
CSV 导出主表，按乘车日期、发车时间排序，无日期的放末尾。通知不混入车票 CSV，可通过 API 单独查看。

## 范围与限制

支持 EML、ZIP 和文件夹递归读取，不解压 ZIP 到文件系统。单封邮件上限默认 20 MiB。
暂不支持 PST、MSG、MBOX、加密 ZIP、嵌套压缩包和自动关联停运到具体车票。
只针对已见的新旧中文 12306 通知格式；未知格式进入待核对，而不是猜测字段。
未提供数据库自动备份。数据库包含乘车信息与原邮件，请按本地业务数据管理。
