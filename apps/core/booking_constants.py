"""跨模块共享的预订/支付时限常量（RSV-07）。

此前「15 分钟支付期限」分别硬编码在 reservations.services 与
billing.payment_intents 两处，改一处即造成订单过期与支付意图窗口漂移：
库存先释放、意图仍有效（或反之）。所有入口统一读取本常量。
"""
PENDING_RESERVATION_TTL_MINUTES = 15
