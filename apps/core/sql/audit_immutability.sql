-- PMS2 MySQL 8 append-only financial/audit facts
-- 先在目标 PMS 数据库中执行；不要把此脚本放进 Django migration（测试/迁移账号与运行账号权限不同）。
-- 此脚本保护只追加表：core_auditevent、billing_ledgerentry、billing_cashiershiftmovement。
-- Payment / Refund / CashierShift / ApprovalRecord 是有状态机的业务事实，不能用本脚本禁止 UPDATE。

DROP TRIGGER IF EXISTS trg_core_auditevent_no_update;
DROP TRIGGER IF EXISTS trg_core_auditevent_no_delete;
DROP TRIGGER IF EXISTS trg_billing_ledgerentry_no_update;
DROP TRIGGER IF EXISTS trg_billing_ledgerentry_no_delete;
DROP TRIGGER IF EXISTS trg_billing_shiftmovement_no_update;
DROP TRIGGER IF EXISTS trg_billing_shiftmovement_no_delete;

DELIMITER $$

CREATE TRIGGER trg_core_auditevent_no_update
BEFORE UPDATE ON core_auditevent
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 audit events are append-only';
END$$

CREATE TRIGGER trg_core_auditevent_no_delete
BEFORE DELETE ON core_auditevent
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 audit events cannot be deleted';
END$$

CREATE TRIGGER trg_billing_ledgerentry_no_update
BEFORE UPDATE ON billing_ledgerentry
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 ledger entries are immutable; post a reversal';
END$$

CREATE TRIGGER trg_billing_ledgerentry_no_delete
BEFORE DELETE ON billing_ledgerentry
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 ledger entries cannot be deleted';
END$$

CREATE TRIGGER trg_billing_shiftmovement_no_update
BEFORE UPDATE ON billing_cashiershiftmovement
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 cashier shift movements are append-only';
END$$

CREATE TRIGGER trg_billing_shiftmovement_no_delete
BEFORE DELETE ON billing_cashiershiftmovement
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 cashier shift movements cannot be deleted';
END$$

DELIMITER ;

-- 部署验证
SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE()
  AND TRIGGER_NAME IN (
    'trg_core_auditevent_no_update',
    'trg_core_auditevent_no_delete',
    'trg_billing_ledgerentry_no_update',
    'trg_billing_ledgerentry_no_delete',
    'trg_billing_shiftmovement_no_update',
    'trg_billing_shiftmovement_no_delete'
  )
ORDER BY EVENT_OBJECT_TABLE, EVENT_MANIPULATION;
