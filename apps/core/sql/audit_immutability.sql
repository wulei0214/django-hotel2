-- PMS2 MySQL 8 financial fact guards: append-only audit/ledger/cash movements + open-shift insert guard
-- 先在目标 PMS 数据库中执行；不要把此脚本放进 Django migration（测试/迁移账号与运行账号权限不同）。
-- 此脚本保护只追加表：core_auditevent、billing_ledgerentry、billing_cashiershiftmovement；
-- 并阻止对已关班/非本人班次直接 INSERT 现金变动。
-- Payment / Refund / CashierShift / ApprovalRecord 是有状态机的业务事实，不能用本脚本禁止 UPDATE。

DROP TRIGGER IF EXISTS trg_core_auditevent_no_update;
DROP TRIGGER IF EXISTS trg_core_auditevent_no_delete;
DROP TRIGGER IF EXISTS trg_billing_ledgerentry_no_update;
DROP TRIGGER IF EXISTS trg_billing_ledgerentry_no_delete;
DROP TRIGGER IF EXISTS trg_billing_shiftmovement_no_update;
DROP TRIGGER IF EXISTS trg_billing_shiftmovement_no_delete;
DROP TRIGGER IF EXISTS trg_billing_shiftmovement_open_shift_insert;

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

CREATE TRIGGER trg_billing_shiftmovement_open_shift_insert
BEFORE INSERT ON billing_cashiershiftmovement
FOR EACH ROW
BEGIN
    DECLARE v_shift_status VARCHAR(16) DEFAULT NULL;
    DECLARE v_shift_cashier BIGINT DEFAULT NULL;
    SELECT status, cashier_id
      INTO v_shift_status, v_shift_cashier
      FROM billing_cashiershift
      WHERE id = NEW.shift_id
      FOR UPDATE;
    IF v_shift_status IS NULL
       OR v_shift_status <> 'OPEN'
       OR v_shift_cashier <> NEW.created_by_id THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'PMS2 cash movement requires its own open cashier shift';
    END IF;
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
    'trg_billing_shiftmovement_no_delete',
    'trg_billing_shiftmovement_open_shift_insert'
  )
ORDER BY EVENT_OBJECT_TABLE, EVENT_MANIPULATION;
