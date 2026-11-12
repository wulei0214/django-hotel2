# -*- coding: utf-8 -*-
"""并发事务隔离辅助（WB-085，第 32 轮）。

MySQL 默认 REPEATABLE READ 的一致性快照在事务**第一次读**时建立。
「先拿行锁的并发事务提交后，后拿锁方的普通 SELECT 仍按旧快照判断」
会让锁后业务校验（排房/续住冲突检查、退房余额检查）看到过期数据，
导致两个事务双双放行、产生重叠占用或资账脱节。

行锁只保护"锁到的行"，不保护"检查用的普通读"；这类
「锁后校验」必须在 READ COMMITTED 下执行——每次普通读都看到最新
已提交数据，与 select_for_update 的最新行版本语义一致。

用法：在 ``transaction.atomic()`` **开启前**调用
``use_read_committed()``（对下一个事务生效；若调用方已处于事务中，
此设置对当前事务无效，属尽力而为的降级）。SQLite 无此语义，直接跳过。
"""
from django.db import connection


def use_read_committed():
    """将本连接的下一个事务隔离级别设为 READ COMMITTED（仅 MySQL）。

    若调用方已处于外层事务中（例如 Django TestCase 会把整个测试包进
    atomic，或业务侧自行组合事务），隔离特征无法中途变更（MySQL 1568），
    此时静默跳过——降级沿用外层事务的隔离级别，属文档化的尽力而为。
    """
    if connection.vendor != "mysql" or connection.in_atomic_block:
        return
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
