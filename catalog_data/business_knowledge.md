# 订单数据库业务口径

- 表是订单跟踪样例。没有金额、单价、数量、成本、库存、支付时间、送达时间等字段；不能计算销售额、客单价、利润、库存或真实配送耗时。发票编号不是发票金额，文本 details 不视为结构化金额或数量。
- 订单数按 Orders.order_id；客户数按 Customers.customer_id；订单明细数按 Order_Items.order_item_id；发货批次数按 Shipments.shipment_id。明细记录数不等于商品购买数量。
- Orders 一行一订单，Order_Items 一行一明细，Shipments 一行一发货批次。一个订单可有多个明细和发货批次，多表直接 JOIN 后 COUNT(*) 会重复统计订单；订单金额等指标也不能在一对多连接后直接 SUM。
- 当前 SQLite 样例订单状态 Shipped=已发货、Pending=待处理、Returned=已退回、Delivered=已送达、Processed=已处理、Canceled=已取消。明细状态 Finish=已完成、Shipped=已发货、Pending=待处理、Packed=已打包、Canceled=已取消。没有支付状态；不能把明细取消当整单取消。其他数据源以运行时实际取值为准。用户要求“已完成订单”时需澄清是否指已送达订单还是全部明细完成。
- 关联路径：Customers.customer_id = Orders.customer_id；Orders.order_id = Order_Items.order_id；Products.product_id = Order_Items.product_id；Orders.order_id = Shipments.order_id；Invoices.invoice_number = Shipments.invoice_number；Shipment_Items.shipment_id = Shipments.shipment_id，Shipment_Items.order_item_id = Order_Items.order_item_id。
- Invoices 与 Orders 没有直接订单外键，须经过 Shipments；关联时先按发票、订单目标粒度去重。Shipment_Items 是桥接记录，不可默认其组合键在数据中唯一。
- 日期问题严格区分下单 date_order_placed、开票 invoice_date、发货 shipment_date。时间区间使用左闭右开；“本月/今年”按当前业务时间，“数据最新月份”才按数据库最大日期。
- “没有订单的客户”保留所有客户，使用 NOT EXISTS 或 LEFT JOIN 判断；“所有明细均完成”用不存在未完成明细，明确是否包括无明细订单和 NULL 状态。业务定义有歧义时询问用户，不能擅自补条件。
