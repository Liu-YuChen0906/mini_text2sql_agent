-- 50 个经典问题的 SQLite 参考查询；不是模型实际输出。
-- 第24题 :customer_id、第48题 :tracking_number 必须绑定参数；不要直接拼接用户输入。
-- 统计口径与数据异常说明见 50个问题可查询性评估.md。

-- 1. 数据库里一共有多少位客户？
-- Customers 一行一客户。
SELECT COUNT(*) AS customer_count FROM Customers;

-- 2. 数据库里一共有多少笔订单？
-- Orders 一行一订单。
SELECT COUNT(*) AS order_count FROM Orders;

-- 3. 数据库里一共有多少种商品？
-- 需区分 product_id 对应的商品记录数，与 DISTINCT product_name 对应的名称/类别数；参考 SQL 按 product_id。
SELECT COUNT(*) AS product_count FROM Products;

-- 4. 各个州分别有多少位客户？
-- 按 state 分组，NULL 单列为未知。
SELECT state, COUNT(*) AS customer_count FROM Customers GROUP BY state ORDER BY customer_count DESC, state;

-- 5. 客户数量最多的十个州是哪些？
-- 按客户数取十州；同数时按州名排序，不额外纳入并列。
SELECT state, COUNT(*) AS customer_count FROM Customers GROUP BY state ORDER BY customer_count DESC, state LIMIT 10;

-- 6. 名称中包含“phone”的商品有哪些？
-- 按 product_name 包含匹配；SQLite LIKE 默认 ASCII 不区分大小写。
SELECT product_id, product_name FROM Products WHERE product_name LIKE '%phone%' ORDER BY product_id;

-- 7. 哪些商品的名称以“music-”开头？
-- 前缀 music-；空结果也可能正确。
SELECT product_id, product_name FROM Products WHERE product_name LIKE 'music-%' ORDER BY product_id;

-- 8. 最近创建的二十笔订单分别是什么时候下的？
-- 用 date_order_placed 排序，不按 order_id 推断时间；同刻用 ID 稳定排序。
SELECT order_id, date_order_placed FROM Orders ORDER BY date_order_placed DESC, order_id DESC LIMIT 20;

-- 9. 当前处于 Pending 状态的订单有多少笔？
-- 实际值为 Pending，大小写必须准确。
SELECT COUNT(*) AS order_count FROM Orders WHERE order_status='Pending';

-- 10. 各种订单状态分别有多少笔？
-- 统计 Orders.order_status。
SELECT order_status, COUNT(*) AS order_count FROM Orders GROUP BY order_status ORDER BY order_status;

-- 11. 各种订单明细状态分别有多少条？
-- 统计 Order_Items.order_item_status。
SELECT order_item_status, COUNT(*) AS item_count FROM Order_Items GROUP BY order_item_status ORDER BY order_item_status;

-- 12. 每位客户各下过多少笔订单？
-- 按 customer_id 分组，LEFT JOIN 保留零订单客户，不能 COUNT(*)。
SELECT c.customer_id, c.customer_name, COUNT(o.order_id) AS order_count FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name ORDER BY c.customer_id;

-- 13. 下单次数最多的十位客户是谁？
-- 按订单数取十位，按客户 ID 打破并列。
SELECT c.customer_id,c.customer_name,COUNT(o.order_id) AS order_count FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name ORDER BY order_count DESC,c.customer_id LIMIT 10;

-- 14. 有多少位客户从未下过订单？
-- NOT EXISTS 查询无订单客户。
SELECT COUNT(*) AS customer_count FROM Customers c WHERE NOT EXISTS (SELECT 1 FROM Orders o WHERE o.customer_id=c.customer_id);

-- 15. 每笔订单包含多少条商品明细？
-- LEFT JOIN 保留无明细订单，COUNT(order_item_id)。
SELECT o.order_id,COUNT(oi.order_item_id) AS item_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id ORDER BY o.order_id;

-- 16. 平均每笔订单包含多少条商品明细？
-- 分母是全部订单，含零明细订单；先逐单计数，再 AVG。
SELECT AVG(item_count * 1.0) AS avg_item_count FROM (SELECT o.order_id,COUNT(oi.order_item_id) AS item_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id);

-- 17. 哪些订单包含的商品明细最多？
-- 返回全部并列最多订单。
WITH counts AS (SELECT o.order_id,COUNT(oi.order_item_id) AS item_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id) SELECT order_id,item_count FROM counts WHERE item_count=(SELECT MAX(item_count) FROM counts) ORDER BY order_id;

-- 18. 每种商品出现在多少笔不同的订单中？
-- 按 product_id 分组，COUNT(DISTINCT order_id)，保留零订单商品。
SELECT p.product_id,p.product_name,COUNT(DISTINCT oi.order_id) AS order_count FROM Products p LEFT JOIN Order_Items oi ON oi.product_id=p.product_id GROUP BY p.product_id,p.product_name ORDER BY p.product_id;

-- 19. 被订购次数最多的十种商品是什么？
-- 需明确次数是不同订单数还是明细记录数；参考 SQL 按不同订单数，不能视作商品件数。
SELECT p.product_id,p.product_name,COUNT(DISTINCT oi.order_id) AS order_count FROM Products p LEFT JOIN Order_Items oi ON oi.product_id=p.product_id GROUP BY p.product_id,p.product_name ORDER BY order_count DESC,p.product_id LIMIT 10;

-- 20. 从未出现在任何订单中的商品有哪些？
-- NOT EXISTS；返回商品 ID 和名称。
SELECT p.product_id,p.product_name FROM Products p WHERE NOT EXISTS (SELECT 1 FROM Order_Items oi WHERE oi.product_id=p.product_id) ORDER BY p.product_id;

-- 21. 最近二十笔订单的客户姓名和订单状态是什么？
-- 同第8题按真实下单时间取最近二十单。
SELECT o.order_id,c.customer_name,o.order_status,o.date_order_placed FROM Orders o JOIN Customers c ON c.customer_id=o.customer_id ORDER BY o.date_order_placed DESC,o.order_id DESC LIMIT 20;

-- 22. 各州客户分别下了多少笔订单？
-- 由客户州关联订单，保留零订单的已有州；没有历史地址，统计的是当前客户州。
SELECT c.state,COUNT(o.order_id) AS order_count FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.state ORDER BY order_count DESC,c.state;

-- 23. 哪些客户下过处于 Returned 状态的订单？
-- 用 EXISTS 每位客户只出现一次。
SELECT c.customer_id,c.customer_name FROM Customers c WHERE EXISTS (SELECT 1 FROM Orders o WHERE o.customer_id=c.customer_id AND o.order_status='Returned') ORDER BY c.customer_id;

-- 24. 某位客户的全部订单分别包含哪些商品？
-- 必须提供 customer_id；姓名可能重名。保留该客户没有明细的订单。
SELECT o.order_id,o.date_order_placed,oi.order_item_id,p.product_id,p.product_name FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id LEFT JOIN Products p ON p.product_id=oi.product_id WHERE o.customer_id=:customer_id ORDER BY o.date_order_placed,o.order_id,oi.order_item_id;

-- 25. 每笔订单涉及多少种不同的商品？
-- 商品种类按 product_id 去重，名称相同但 ID 不同仍是不同商品记录。
SELECT o.order_id,COUNT(DISTINCT oi.product_id) AS product_count FROM Orders o LEFT JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_id ORDER BY o.order_id;

-- 26. 同一笔订单中，哪些商品出现了不止一次？
-- 按 (order_id, product_id) 统计明细条数>1，不是数量字段>1。
SELECT oi.order_id,oi.product_id,p.product_name,COUNT(*) AS item_count FROM Order_Items oi JOIN Products p ON p.product_id=oi.product_id GROUP BY oi.order_id,oi.product_id,p.product_name HAVING COUNT(*)>1 ORDER BY oi.order_id,oi.product_id;

-- 27. 哪些订单包含状态为 Canceled 的商品明细？
-- 判断明细 Canceled，不要误用订单状态。
SELECT o.order_id FROM Orders o WHERE EXISTS (SELECT 1 FROM Order_Items oi WHERE oi.order_id=o.order_id AND oi.order_item_status='Canceled') ORDER BY o.order_id;

-- 28. 订单状态为 Delivered、但仍有商品明细处于 Pending 状态的订单有哪些？
-- Orders.Delivered 与 Order_Items.Pending 联合筛选，用 EXISTS 防重复。
SELECT o.order_id FROM Orders o WHERE o.order_status='Delivered' AND EXISTS (SELECT 1 FROM Order_Items oi WHERE oi.order_id=o.order_id AND oi.order_item_status='Pending') ORDER BY o.order_id;

-- 29. 每种订单状态下，各有多少条不同状态的商品明细？
-- 按两个状态字段分组，每条明细算一次；未出现组合默认不补零。
SELECT o.order_status,oi.order_item_status,COUNT(*) AS item_count FROM Orders o JOIN Order_Items oi ON oi.order_id=o.order_id GROUP BY o.order_status,oi.order_item_status ORDER BY o.order_status,oi.order_item_status;

-- 30. 哪些商品曾被处于 Returned 状态的订单订购？
-- 按订单 Returned 找商品，每个 product_id 一次。
SELECT p.product_id,p.product_name FROM Products p WHERE EXISTS (SELECT 1 FROM Order_Items oi JOIN Orders o ON o.order_id=oi.order_id WHERE oi.product_id=p.product_id AND o.order_status='Returned') ORDER BY p.product_id;

-- 31. 数据中最早和最晚的下单时间分别是什么？
-- 下单时间 MIN/MAX。
SELECT MIN(date_order_placed) AS first_order_time,MAX(date_order_placed) AS last_order_time FROM Orders;

-- 32. 每个月分别新增了多少笔订单？
-- 按年-月分组，不能只按月份号码合并不同年份。
SELECT strftime('%Y-%m',date_order_placed) AS month,COUNT(*) AS order_count FROM Orders GROUP BY month ORDER BY month;

-- 33. 每天的订单数量如何变化？
-- 按日期分组；没有订单的日期默认不补零。
SELECT date(date_order_placed) AS day,COUNT(*) AS order_count FROM Orders GROUP BY day ORDER BY day;

-- 34. 哪一天的下单数量最多？
-- 返回全部并列最多日期。
WITH counts AS (SELECT date(date_order_placed) AS day,COUNT(*) AS order_count FROM Orders GROUP BY day) SELECT day,order_count FROM counts WHERE order_count=(SELECT MAX(order_count) FROM counts) ORDER BY day;

-- 35. 2026 年第一季度各个月的订单状态分布如何？
-- 时间区间 [2026-01-01,2026-04-01)，再按年-月和状态分组。
SELECT strftime('%Y-%m',date_order_placed) AS month,order_status,COUNT(*) AS order_count FROM Orders WHERE date_order_placed>='2026-01-01' AND date_order_placed<'2026-04-01' GROUP BY month,order_status ORDER BY month,order_status;

-- 36. 各州客户在每个月分别下了多少笔订单？
-- 按年-月和当前客户州分组；未出现组合默认不补零。
SELECT c.state,strftime('%Y-%m',o.date_order_placed) AS month,COUNT(*) AS order_count FROM Orders o JOIN Customers c ON c.customer_id=o.customer_id GROUP BY c.state,month ORDER BY month,c.state;

-- 37. 每个月有多少位不同客户下过订单？
-- 每月 COUNT(DISTINCT customer_id)。
SELECT strftime('%Y-%m',date_order_placed) AS month,COUNT(DISTINCT customer_id) AS customer_count FROM Orders GROUP BY month ORDER BY month;

-- 38. 每位客户首次和最近一次下单分别是什么时候？
-- 保留零订单客户，其首次/最近时间为 NULL。
SELECT c.customer_id,c.customer_name,MIN(o.date_order_placed) AS first_order_time,MAX(o.date_order_placed) AS last_order_time FROM Customers c LEFT JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name ORDER BY c.customer_id;

-- 39. 哪些客户在首次下单后又再次下单？
-- 按题目“首次之后”严格要求 MAX(time)>MIN(time)；仅同一时刻两笔单不算之后。若要两笔及以上订单，应另用 COUNT(order_id)>=2。
SELECT c.customer_id,c.customer_name FROM Customers c JOIN Orders o ON o.customer_id=c.customer_id GROUP BY c.customer_id,c.customer_name HAVING MAX(o.date_order_placed)>MIN(o.date_order_placed) ORDER BY c.customer_id;

-- 40. 哪些订单的发票日期与下单日期不是同一天？
-- Orders→Shipments→Invoices；任一关联发票日期不同即入选，订单去重；无关联发票或 NULL 日期不能判定。
SELECT o.order_id FROM Orders o WHERE EXISTS (SELECT 1 FROM Shipments s JOIN Invoices i ON i.invoice_number=s.invoice_number WHERE s.order_id=o.order_id AND date(i.invoice_date)<>date(o.date_order_placed)) ORDER BY o.order_id;

-- 41. 每种订单状态分别对应多少笔发货记录？
-- 计数 Shipments.shipment_id，不能计数订单或重复关联后的明细。
SELECT o.order_status,COUNT(s.shipment_id) AS shipment_count FROM Orders o LEFT JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_status ORDER BY o.order_status;

-- 42. 哪些订单尚未关联发货记录？
-- NOT EXISTS Shipments，不按 order_status 代替关联关系。
SELECT o.order_id FROM Orders o WHERE NOT EXISTS (SELECT 1 FROM Shipments s WHERE s.order_id=o.order_id) ORDER BY o.order_id;

-- 43. 哪些订单对应了不止一笔发货记录？
-- 按 order_id 聚合 Shipments 的记录数>1。
SELECT o.order_id,COUNT(s.shipment_id) AS shipment_count FROM Orders o JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_id HAVING COUNT(s.shipment_id)>1 ORDER BY shipment_count DESC,o.order_id;

-- 44. 每笔订单从下单到首次发货间隔了多久？
-- 可算，但真实数据有早于下单的发货；参考 SQL 保留负时间差，无发货为 NULL，单位小时，首次=MIN(shipment_date)。
SELECT o.order_id,o.date_order_placed,MIN(s.shipment_date) AS first_shipment_time,ROUND((julianday(MIN(s.shipment_date))-julianday(o.date_order_placed))*24,6) AS hours_to_first_shipment FROM Orders o LEFT JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_id,o.date_order_placed ORDER BY o.order_id;

-- 45. 下单到首次发货间隔最长的十笔订单是哪些？
-- 需明确异常处理；参考 SQL 排除首次发货早于下单及无日期订单，再取最长十单。不要擅自改成首次非负发货。
WITH firsts AS (SELECT o.order_id,o.date_order_placed,MIN(s.shipment_date) AS first_shipment_time FROM Orders o JOIN Shipments s ON s.order_id=o.order_id GROUP BY o.order_id,o.date_order_placed) SELECT order_id,date_order_placed,first_shipment_time,ROUND((julianday(first_shipment_time)-julianday(date_order_placed))*24,6) AS hours_to_first_shipment FROM firsts WHERE first_shipment_time IS NOT NULL AND julianday(first_shipment_time)>=julianday(date_order_placed) ORDER BY (julianday(first_shipment_time)-julianday(date_order_placed)) DESC,order_id LIMIT 10;

-- 46. 每笔发货记录包含多少条订单明细？
-- 经 Shipment_Items 计数 DISTINCT order_item_id，保留零明细发货；组合关系没有唯一约束。
SELECT s.shipment_id,COUNT(DISTINCT si.order_item_id) AS item_count FROM Shipments s LEFT JOIN Shipment_Items si ON si.shipment_id=s.shipment_id GROUP BY s.shipment_id ORDER BY s.shipment_id;

-- 47. 哪些发货记录没有关联任何订单明细？
-- NOT EXISTS Shipment_Items。
SELECT s.shipment_id FROM Shipments s WHERE NOT EXISTS (SELECT 1 FROM Shipment_Items si WHERE si.shipment_id=s.shipment_id) ORDER BY s.shipment_id;

-- 48. 某个运单号对应的客户、订单、商品和发货日期是什么？
-- 必须提供 tracking_number；走 Shipment_Items 找发货商品，不能取订单所有商品；运单号无唯一约束，返回全部匹配发货及明细。
SELECT s.shipment_id,s.shipment_tracking_number,s.order_id,c.customer_id,c.customer_name,s.shipment_date,oi.order_item_id,oi.order_id AS item_order_id,p.product_id,p.product_name FROM Shipments s JOIN Orders o ON o.order_id=s.order_id JOIN Customers c ON c.customer_id=o.customer_id LEFT JOIN Shipment_Items si ON si.shipment_id=s.shipment_id LEFT JOIN Order_Items oi ON oi.order_item_id=si.order_item_id LEFT JOIN Products p ON p.product_id=oi.product_id WHERE s.shipment_tracking_number=:tracking_number ORDER BY s.shipment_id,oi.order_item_id;

-- 49. 哪些订单明细尚未关联任何发货记录？
-- 左连接反查询 Shipment_Items；当前无该外键索引，相关 NOT EXISTS 曾超过 Mini 的2秒时限。与订单是否关联发货是不同层级。
SELECT oi.order_item_id,oi.order_id,oi.product_id FROM Order_Items oi LEFT JOIN Shipment_Items si ON si.order_item_id=oi.order_item_id WHERE si.order_item_id IS NULL ORDER BY oi.order_item_id;

-- 50. 哪些发货记录关联的订单明细不属于该发货记录对应的订单？
-- 比较 Shipments.order_id 和桥接明细 Order_Items.order_id；不能预先用二者相等作为 JOIN 条件。
SELECT s.shipment_id,s.order_id AS shipment_order_id,oi.order_item_id,oi.order_id AS item_order_id FROM Shipment_Items si JOIN Shipments s ON s.shipment_id=si.shipment_id JOIN Order_Items oi ON oi.order_item_id=si.order_item_id WHERE oi.order_id<>s.order_id ORDER BY s.shipment_id,oi.order_item_id;
