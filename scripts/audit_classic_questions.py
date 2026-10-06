"""离线执行 50 个经典问题的参考 SQL；不调用模型、不修改业务数据库。"""
from pathlib import Path
import json
import sqlite3
import sys
import argparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 使用主键分组，避免同名客户或商品被合并；默认保留零记录实体。
QUERIES = [
"SELECT COUNT(*) AS customer_count FROM Customers",
"SELECT COUNT(*) AS order_count FROM Orders",
"SELECT COUNT(*) AS product_count FROM Products",
"SELECT state, COUNT(*) AS customer_count FROM Customers GROUP BY state ORDER BY customer_count DESC, state",
"SELECT state, COUNT(*) AS customer_count FROM Customers GROUP BY state ORDER BY customer_count DESC, state LIMIT 10",
"SELECT product_id, product_name FROM Products WHERE product_name LIKE '%phone%' ORDER BY product_id",
"SELECT product_id, product_name FROM Products WHERE product_name LIKE 'music-%' ORDER BY product_id",
"SELECT order_id, date_order_placed FROM Orders ORDER BY date_order_placed DESC, order_id DESC LIMIT 20",
"SELECT COUNT(*) AS order_count FROM Orders WHERE order_status='Pending'",
"SELECT order_status, COUNT(*) AS order_count FROM Orders GROUP BY order_status ORDER BY order_status",
"SELECT order_item_status, COUNT(*) AS item_count FROM Order_Items GROUP BY order_item_status ORDER BY order_item_status",
"SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS order_count FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name ORDER BY c.customer_id",
"SELECT c.customer_id,c.customer_name,COUNT(o.order_id) AS order_count FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name ORDER BY order_count DESC,c.customer_id LIMIT 10",
"SELECT COUNT(*) AS customer_count FROM Customers c WHERE NOT EXISTS (SELECT 1 FROM Orders o WHERE o.customer_id=c.customer_id)",
"SELECT o.order_id,COUNT(oi.order_item_id) AS item_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id ORDER BY o.order_id",
"SELECT AVG(item_count * 1.0) AS avg_item_count FROM (SELECT o.order_id,COUNT(oi.order_item_id) AS item_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id)",
"WITH counts AS (SELECT o.order_id,COUNT(oi.order_item_id) AS item_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id) SELECT order_id,item_count FROM counts WHERE item_count=(SELECT MAX(item_count) FROM counts) ORDER BY order_id",
"SELECT p.product_id,p.product_name,COUNT(DISTINCT oi.order_id) AS order_count FROM Products p LEFT JOIN Order_Items oi ON oi.product_id=p.product_id GROUP BY p.product_id,p.product_name ORDER BY p.product_id",
"SELECT p.product_id,p.product_name,COUNT(DISTINCT oi.order_id) AS order_count FROM Products p LEFT JOIN Order_Items oi ON oi.product_id=p.product_id GROUP BY p.product_id,p.product_name ORDER BY order_count DESC,p.product_id LIMIT 10",
"SELECT p.product_id,p.product_name FROM Products p WHERE NOT EXISTS (SELECT 1 FROM Order_Items oi WHERE oi.product_id=p.product_id) ORDER BY p.product_id",
"SELECT o.order_id,c.customer_name,o.order_status,o.date_order_placed FROM Orders o JOIN Customers c ON c.customer_id=o.customer_id ORDER BY o.date_order_placed DESC,o.order_id DESC LIMIT 20",
"SELECT c.state,COUNT(o.order_id) AS order_count FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.state ORDER BY order_count DESC,c.state",
"SELECT c.customer_id,c.customer_name FROM Customers c WHERE EXISTS (SELECT 1 FROM Orders o WHERE o.customer_id=c.customer_id AND o.order_status='Returned') ORDER BY c.customer_id",
"SELECT o.order_id,o.date_order_placed,oi.order_item_id,p.product_id,p.product_name FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id LEFT JOIN Products p ON p.product_id=oi.product_id WHERE o.customer_id=:customer_id ORDER BY o.date_order_placed,o.order_id,oi.order_item_id",
"SELECT o.order_id,COUNT(DISTINCT oi.product_id) AS product_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id ORDER BY o.order_id",
"SELECT oi.order_id,oi.product_id,p.product_name,COUNT(*) AS item_count FROM Order_Items oi JOIN Products p ON p.product_id=oi.product_id GROUP BY oi.order_id,oi.product_id,p.product_name HAVING COUNT(*)>1 ORDER BY oi.order_id,oi.product_id",
"SELECT o.order_id FROM Orders o WHERE EXISTS (SELECT 1 FROM Order_Items oi WHERE oi.order_id=o.order_id AND oi.order_item_status='Canceled') ORDER BY o.order_id",
"SELECT o.order_id FROM Orders o WHERE o.order_status='Delivered' AND EXISTS (SELECT 1 FROM Order_Items oi WHERE oi.order_id=o.order_id AND oi.order_item_status='Pending') ORDER BY o.order_id",
"SELECT o.order_status,oi.order_item_status,COUNT(*) AS item_count FROM Orders o JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_status,oi.order_item_status ORDER BY o.order_status,oi.order_item_status",
"SELECT p.product_id,p.product_name FROM Products p WHERE EXISTS (SELECT 1 FROM Order_Items oi JOIN Orders o ON o.order_id=oi.order_id WHERE oi.product_id=p.product_id AND o.order_status='Returned') ORDER BY p.product_id",
"SELECT MIN(date_order_placed) AS first_order_time,MAX(date_order_placed) AS last_order_time FROM Orders",
"SELECT strftime('%Y-%m',date_order_placed) AS month,COUNT(*) AS order_count FROM Orders GROUP BY month ORDER BY month",
"SELECT date(date_order_placed) AS day,COUNT(*) AS order_count FROM Orders GROUP BY day ORDER BY day",
"WITH counts AS (SELECT date(date_order_placed) AS day,COUNT(*) AS order_count FROM Orders GROUP BY day) SELECT day,order_count FROM counts WHERE order_count=(SELECT MAX(order_count) FROM counts) ORDER BY day",
"SELECT strftime('%Y-%m',date_order_placed) AS month,order_status,COUNT(*) AS order_count FROM Orders WHERE date_order_placed>='2026-01-01' AND date_order_placed<'2026-04-01' GROUP BY month,order_status ORDER BY month,order_status",
"SELECT c.state,strftime('%Y-%m',o.date_order_placed) AS month,COUNT(*) AS order_count FROM Orders o JOIN Customers c ON c.customer_id=o.customer_id GROUP BY c.state,month ORDER BY month,c.state",
"SELECT strftime('%Y-%m',date_order_placed) AS month,COUNT(DISTINCT customer_id) AS customer_count FROM Orders GROUP BY month ORDER BY month",
"SELECT c.customer_id,c.customer_name,MIN(o.date_order_placed) AS first_order_time,MAX(o.date_order_placed) AS last_order_time FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name ORDER BY c.customer_id",
"SELECT c.customer_id,c.customer_name FROM Customers c JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name HAVING MAX(o.date_order_placed)>MIN(o.date_order_placed) ORDER BY c.customer_id",
"SELECT o.order_id FROM Orders o WHERE EXISTS (SELECT 1 FROM Shipments s JOIN Invoices i ON i.invoice_number=s.invoice_number WHERE s.order_id=o.order_id AND date(i.invoice_date)<>date(o.date_order_placed)) ORDER BY o.order_id",
"SELECT o.order_status,COUNT(s.shipment_id) AS shipment_count FROM Orders o LEFT JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_status ORDER BY o.order_status",
"SELECT o.order_id FROM Orders o WHERE NOT EXISTS (SELECT 1 FROM Shipments s WHERE s.order_id=o.order_id) ORDER BY o.order_id",
"SELECT o.order_id,COUNT(s.shipment_id) AS shipment_count FROM Orders o JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_id HAVING COUNT(s.shipment_id)>1 ORDER BY shipment_count DESC,o.order_id",
"SELECT o.order_id,o.date_order_placed,MIN(s.shipment_date) AS first_shipment_time,ROUND((julianday(MIN(s.shipment_date))-julianday(o.date_order_placed))*24,6) AS hours_to_first_shipment FROM Orders o LEFT JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_id,o.date_order_placed ORDER BY o.order_id",
"WITH firsts AS (SELECT o.order_id,o.date_order_placed,MIN(s.shipment_date) AS first_shipment_time FROM Orders o JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_id,o.date_order_placed) SELECT order_id,date_order_placed,first_shipment_time,ROUND((julianday(first_shipment_time)-julianday(date_order_placed))*24,6) AS hours_to_first_shipment FROM firsts WHERE first_shipment_time IS NOT NULL AND julianday(first_shipment_time)>=julianday(date_order_placed) ORDER BY (julianday(first_shipment_time)-julianday(date_order_placed)) DESC,order_id LIMIT 10",
"SELECT s.shipment_id,COUNT(DISTINCT si.order_item_id) AS item_count FROM Shipments s LEFT JOIN Shipment_Items si ON si.shipment_id=s.shipment_id GROUP BY s.shipment_id ORDER BY s.shipment_id",
"SELECT s.shipment_id FROM Shipments s WHERE NOT EXISTS (SELECT 1 FROM Shipment_Items si WHERE si.shipment_id=s.shipment_id) ORDER BY s.shipment_id",
"SELECT s.shipment_id,s.shipment_tracking_number,s.order_id,c.customer_id,c.customer_name,s.shipment_date,oi.order_item_id,oi.order_id AS item_order_id,p.product_id,p.product_name FROM Shipments s JOIN Orders o ON o.order_id=s.order_id JOIN Customers c ON c.customer_id=o.customer_id LEFT JOIN Shipment_Items si ON si.shipment_id=s.shipment_id LEFT JOIN Order_Items oi ON oi.order_item_id=si.order_item_id LEFT JOIN Products p ON p.product_id=oi.product_id WHERE s.shipment_tracking_number=:tracking_number ORDER BY s.shipment_id,oi.order_item_id",
"SELECT oi.order_item_id,oi.order_id,oi.product_id FROM Order_Items oi LEFT JOIN Shipment_Items si ON si.order_item_id=oi.order_item_id WHERE si.order_item_id IS NULL ORDER BY oi.order_item_id",
"SELECT s.shipment_id,s.order_id AS shipment_order_id,oi.order_item_id,oi.order_id AS item_order_id FROM Shipment_Items si JOIN Shipments s ON s.shipment_id=si.shipment_id JOIN Order_Items oi ON oi.order_item_id=si.order_item_id WHERE oi.order_id<>s.order_id ORDER BY s.shipment_id,oi.order_item_id",
]
NOTES = [
"Customers 一行一客户。", "Orders 一行一订单。", "需区分 product_id 对应的商品记录数，与 DISTINCT product_name 对应的名称/类别数；参考 SQL 按 product_id。",
"按 state 分组，NULL 单列为未知。", "按客户数取十州；同数时按州名排序，不额外纳入并列。", "按 product_name 包含匹配；SQLite LIKE 默认 ASCII 不区分大小写。", "前缀 music-；空结果也可能正确。",
"用 date_order_placed 排序，不按 order_id 推断时间；同刻用 ID 稳定排序。", "实际值为 Pending，大小写必须准确。", "统计 Orders.order_status。", "统计 Order_Items.order_item_status。",
"按 customer_id 分组，LEFT JOIN 保留零订单客户，不能 COUNT(*)。", "按订单数取十位，按客户 ID 打破并列。", "NOT EXISTS 查询无订单客户。", "LEFT JOIN 保留无明细订单，COUNT(order_item_id)。",
"分母是全部订单，含零明细订单；先逐单计数，再 AVG。", "返回全部并列最多订单。", "按 product_id 分组，COUNT(DISTINCT order_id)，保留零订单商品。",
"需明确次数是不同订单数还是明细记录数；参考 SQL 按不同订单数，不能视作商品件数。", "NOT EXISTS；返回商品 ID 和名称。", "同第8题按真实下单时间取最近二十单。",
"由客户州关联订单，保留零订单的已有州；没有历史地址，统计的是当前客户州。", "用 EXISTS 每位客户只出现一次。", "必须提供 customer_id；姓名可能重名。保留该客户没有明细的订单。",
"商品种类按 product_id 去重，名称相同但 ID 不同仍是不同商品记录。", "按 (order_id, product_id) 统计明细条数>1，不是数量字段>1。", "判断明细 Canceled，不要误用订单状态。",
"Orders.Delivered 与 Order_Items.Pending 联合筛选，用 EXISTS 防重复。", "按两个状态字段分组，每条明细算一次；未出现组合默认不补零。", "按订单 Returned 找商品，每个 product_id 一次。",
"下单时间 MIN/MAX。", "按年-月分组，不能只按月份号码合并不同年份。", "按日期分组；没有订单的日期默认不补零。", "返回全部并列最多日期。",
"时间区间 [2026-01-01,2026-04-01)，再按年-月和状态分组。", "按年-月和当前客户州分组；未出现组合默认不补零。", "每月 COUNT(DISTINCT customer_id)。",
"保留零订单客户，其首次/最近时间为 NULL。", "按题目“首次之后”严格要求 MAX(time)>MIN(time)；仅同一时刻两笔单不算之后。若要两笔及以上订单，应另用 COUNT(order_id)>=2。",
"Orders→Shipments→Invoices；任一关联发票日期不同即入选，订单去重；无关联发票或 NULL 日期不能判定。", "计数 Shipments.shipment_id，不能计数订单或重复关联后的明细。",
"NOT EXISTS Shipments，不按 order_status 代替关联关系。", "按 order_id 聚合 Shipments 的记录数>1。",
"可算，但真实数据有早于下单的发货；参考 SQL 保留负时间差，无发货为 NULL，单位小时，首次=MIN(shipment_date)。",
"需明确异常处理；参考 SQL 排除首次发货早于下单及无日期订单，再取最长十单。不要擅自改成首次非负发货。",
"经 Shipment_Items 计数 DISTINCT order_item_id，保留零明细发货；组合关系没有唯一约束。", "NOT EXISTS Shipment_Items。",
"必须提供 tracking_number；走 Shipment_Items 找发货商品，不能取订单所有商品；运单号无唯一约束，返回全部匹配发货及明细。",
"左连接反查询 Shipment_Items；当前无该外键索引，相关 NOT EXISTS 曾超过 Mini 的2秒时限。与订单是否关联发货是不同层级。", "比较 Shipments.order_id 和桥接明细 Order_Items.order_id；不能预先用二者相等作为 JOIN 条件。",
]
CLASSIFICATIONS = {3:"需明确口径",19:"需明确口径",24:"需补参数",44:"需处理异常",45:"需处理异常",48:"需补参数"}


def main(check_adapter=False):
    questions = [line.split('. ',1)[1] for line in (ROOT/'docs/订单数据库50个经典问题.md').read_text().splitlines() if line.split('. ',1)[0].isdigit()]
    assert len(questions) == len(QUERIES) == len(NOTES) == 50
    with sqlite3.connect((ROOT/'data/tracking_orders.sqlite').resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        customer_id=db.execute('SELECT customer_id FROM Orders ORDER BY order_id LIMIT 1').fetchone()[0]
        tracking=db.execute('SELECT shipment_tracking_number FROM Shipments WHERE shipment_tracking_number IS NOT NULL ORDER BY shipment_id LIMIT 1').fetchone()[0]
        params={'customer_id':customer_id,'tracking_number':tracking}
        results=[]; all_rows=[]
        for i,(question,sql,note) in enumerate(zip(questions,QUERIES,NOTES),1):
            cursor=db.execute(sql,params);rows=cursor.fetchall();all_rows.append(rows)
            results.append({'id':i,'question':question,'assessment':CLASSIFICATIONS.get(i,'可直接查询'),'note':note,'row_count':len(rows),'columns':[col[0] for col in cursor.description]})
        # Cross-check independent totals, so successful execution alone is not the only evidence.
        orders=all_rows[1][0][0]; customers=all_rows[0][0][0]
        items=db.execute('SELECT COUNT(*) FROM Order_Items').fetchone()[0]
        assert sum(row[1] for row in all_rows[9]) == orders
        assert sum(row[2] for row in all_rows[11]) == orders and len(all_rows[11])==customers
        assert sum(row[1] for row in all_rows[14]) == items
        assert abs(all_rows[15][0][0] - items/orders)<1e-10
        assert sum(row[1] for row in all_rows[31]) == orders
        assert sum(row[1] for row in all_rows[32]) == orders
        negative=db.execute('SELECT COUNT(*) FROM Shipments s JOIN Orders o ON o.order_id=s.order_id WHERE julianday(s.shipment_date)<julianday(o.date_order_placed)').fetchone()[0]
        negative_orders=sum(row[3] is not None and row[3]<0 for row in all_rows[43])
        assert all(row[3]>=0 for row in all_rows[44])
        product_names=db.execute('SELECT COUNT(DISTINCT product_name) FROM Products').fetchone()[0]
        violations=len(db.execute('PRAGMA foreign_key_check').fetchall())
        duplicates=db.execute('SELECT COUNT(*) FROM (SELECT shipment_id,order_item_id FROM Shipment_Items GROUP BY 1,2 HAVING COUNT(*)>1)').fetchone()[0]
        date_range=db.execute('SELECT MIN(date_order_placed),MAX(date_order_placed) FROM Orders').fetchone()
        adapter_results = None
        if check_adapter:
            from mini.query.sqlite_adapter import SQLiteAdapter
            adapter = SQLiteAdapter(ROOT/'data/tracking_orders.sqlite')
            literals = {':customer_id':db.execute('SELECT quote(?)',(customer_id,)).fetchone()[0],
                        ':tracking_number':db.execute('SELECT quote(?)',(tracking,)).fetchone()[0]}
            adapter_results = []
            for number, query in enumerate(QUERIES,1):
                for placeholder,literal in literals.items():
                    query = query.replace(placeholder,literal)
                result = adapter.execute_readonly(query).result
                adapter_results.append({'id':number,'status':result['status'],'error':result['error']})
            assert all(item['status']=='success' for item in adapter_results), adapter_results
    sql_lines=['-- 50 个经典问题的 SQLite 参考查询；不是模型实际输出。', '-- 第24题 :customer_id、第48题 :tracking_number 必须绑定参数；不要直接拼接用户输入。', '-- 统计口径与数据异常说明见 50个问题可查询性评估.md。','']
    for i,(question,sql,note) in enumerate(zip(questions,QUERIES,NOTES),1):
        sql_lines.extend([f'-- {i}. {question}',f'-- {note}',sql+';',''])
    (ROOT/'docs/订单数据库50个问题参考.sql').write_text('\n'.join(sql_lines))
    headers=[
        '# 50 个问题的可查询性评估',
        '',
        '依据：当前 `data/tracking_orders.sqlite` 的真实建表定义、外键、状态值及数据（2026-10-01）。所有检查只读执行，不调用模型。',
        '',
        '**结论：50 题均可由现有表和字段表达，没有缺字段而无法计算的题。44 题可按文档说明直接查询；第3、19题需明确统计口径，第24、48题需补具体参数，第44、45题需确定异常时间差的处理规则。**',
        '',
        '这说明数据库支持这些问题，不代表当前模型一定能正确生成全部 SQL。下面是人工制定口径后的参考 SQL 执行验证，没有测试真实模型的50题准确率。',
        '',
        f'所有 50 条参考查询均执行成功；第24题绑定了一个已有订单的客户 ID，第48题绑定了一个已有运单号。当前客户 {customers} 位、订单 {orders} 笔、商品 ID {all_rows[2][0][0]} 个，但不同商品名称仅 {product_names} 个。订单明细 {items} 条。下单时间范围为 {date_range[0]} 至 {date_range[1]}。',
        '',
        f'数据注意事项：{negative} 笔发货记录早于下单，其中 {negative_orders} 笔订单的首次发货时间差为负；外键违规 {violations} 条；当前 Shipment_Items 重复组合 {duplicates} 组，但表没有唯一约束。',
        '',
        ('Mini 实际 SQLite 只读执行器：50 条参考查询均通过，沿用默认2秒超时和100行展示限制。' if check_adapter else '本次仅直接 SQLite 执行，未复测 Mini 执行器时限；使用 --check-adapter 可检查。'),
        '',
        '性能说明：第49题原先用相关 NOT EXISTS，在当前未为 Shipment_Items.order_item_id 建索引的数据上触发2秒超时。参考 SQL 已改为等价 LEFT JOIN + IS NULL，保持结果不变且通过时限；本次没有修改数据库或添加索引。',
        '',
        '参考 SQL：[订单数据库50个问题参考.sql](订单数据库50个问题参考.sql)。重新验证：在 mini 根目录运行 `.venv/bin/python scripts/audit_classic_questions.py --check-adapter`。该脚本会重新生成本报告、参考 SQL 和验证 JSON，不修改数据库。',
        '',
        '## 逐题结论',
        '',
        '“返回行数”是当前样例数据执行参考 SQL 的行数。COUNT 查询返回1行，不等于计数结果为1；0行表示没有匹配记录，不自动代表 SQL 错误。Top N 默认只取 N 条，若想包含边界并列，需要另行指定。',
        '',
        '| 题号 | 问题 | 判断 | 口径与关键条件 | 返回行数 |',
        '| --- | --- | --- | --- | --- |',
    ]
    for result in results:
        headers.append(f"| {result['id']} | {result['question']} | {result['assessment']} | {result['note']} | {result['row_count']} |")
    headers.extend(['','## 建议明确写法','',
        '- 第3题：分别问“数据库有多少条商品记录（按 product_id）？”或“有多少个不同商品名称/类别（按 product_name）？”。',
        '- 第19题：“出现在不同订单中次数最多的十个商品 ID 是哪些？同一订单中重复明细只算一次。”若统计明细次数，明确改为每条 Order_Items 算一次；二者均不是实际件数。',
        '- 第24题：“customer_id 为指定值的客户，全部订单分别包含哪些商品？”输入客户姓名时，先解决同名客户。',
        '- 第44题：“按小时计算每单下单到首次发货的时间差；未发货为 NULL，负值标记为数据异常，不改写原时间。”',
        '- 第45题：“排除首次发货早于下单及首次发货日期缺失的订单，输出下单到首次发货耗时最长的十单，单位小时。”',
        '- 第48题：“查询运单号为指定值的所有发货记录及其客户、订单、关联商品、发货日期；按 Shipment_Items 的实际关联取商品。”',
        '',
        '## 容易写错但结构支持的题','',
        '- 第12、15、16、18、22、38、41、46题注意零记录实体和 LEFT JOIN；计数子表主键，不要把 COUNT(*) 的占位行算成1。',
        '- 第18、19、25题注意不同订单/商品去重；商品名称会重复，按名称分组可能把不同 ID 合并。',
        '- 第17、34题返回全部并列第一。第39题严格按“首次之后”比较时间；“至少两笔订单”是另一口径。',
        '- 第32、33、36题默认只显示有记录的日期或组合；如需连续时间轴补零，应明确日历范围。',
        '- 第40题经 Shipments 连接发票；没有关联发票不能断言同日或异日，返回订单时必须去重。',
        '- 第42、47、49题分别检查订单、发货、订单明细三个层级，不能互换。',
        '- 第44题保留无发货订单；第45题避免将“首次发货”偷换为“首次有效/非负发货”。',
        '- 第48题不能把订单的全部商品当作该批次发货的商品。第50题是关联一致性检查，用相等条件关联会把要找的异常过滤掉。',
        '',
        '## 验证边界','',
        '脚本执行参考 SQL，并交叉验证订单状态合计、客户订单合计、逐单明细合计、平均明细数、月/日订单合计与基础表总数。通过这些检查不构成每题业务正确性的数学证明。第3、19、44、45题的参考答案依赖这里明确记录的口径，用户选择其他口径时须调整 SQL。',
    ])
    (ROOT/'docs/50个问题可查询性评估.md').write_text('\n'.join(headers)+'\n')
    (ROOT/'docs/50个问题验证结果.json').write_text(json.dumps({'reference_queries_passed':50,'model_tested':False,'mini_adapter_results':adapter_results,'negative_shipment_records':negative,'negative_first_shipment_orders':negative_orders,'cases':results},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'executed':50,'classifications':{label:sum(x['assessment']==label for x in results) for label in ('可直接查询','需明确口径','需补参数','需处理异常')},'zero_row_questions':[x['id'] for x in results if not x['row_count']],'negative_shipments':negative,'negative_first_shipment_orders':negative_orders,'product_ids':all_rows[2][0][0],'product_names':product_names},ensure_ascii=False))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-adapter',action='store_true',help='同时通过 Mini 执行器验证只读限制、100行限制和2秒超时')
    main(parser.parse_args().check_adapter)
