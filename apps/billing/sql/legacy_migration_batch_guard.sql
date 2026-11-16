-- PMS2 历史迁移批次守卫（迁移方案 §5.2 数据库层）
-- 目标表 billing_legacymigrationbatch（Django 默认表名）。
-- 保护：CLOSED 终态不可改写/不可删除；状态流转白名单；ACTIVE 后 scope 与执行人冻结。
-- 安装：python manage.py install_legacy_batch_guards
-- 校验：python manage.py check_legacy_batch_guards（CI 强制）
-- 注意：SQLite 测试环境无触发器，模型/QuerySet 层守卫是该环境唯一防线。

DROP TRIGGER IF EXISTS trg_billing_legacybatch_no_delete;
DROP TRIGGER IF EXISTS trg_billing_legacybatch_guard_update;

DELIMITER $$

CREATE TRIGGER trg_billing_legacybatch_no_delete
BEFORE DELETE ON billing_legacymigrationbatch
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 migration batches cannot be deleted';
END$$

CREATE TRIGGER trg_billing_legacybatch_guard_update
BEFORE UPDATE ON billing_legacymigrationbatch
FOR EACH ROW
BEGIN
    IF OLD.status = 'CLOSED' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'PMS2 closed migration batch is terminal';
    END IF;
    IF NEW.status <> OLD.status THEN
        IF NOT ((OLD.status = 'PENDING'
                 AND NEW.status IN ('ACTIVE', 'CLOSED', 'REJECTED'))
                OR (OLD.status = 'ACTIVE' AND NEW.status = 'CLOSED')) THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'PMS2 illegal migration batch status transition';
        END IF;
    END IF;
    IF OLD.status = 'ACTIVE' AND (
        NOT (NEW.scope_property_id <=> OLD.scope_property_id)
        OR NOT (NEW.executed_by_id <=> OLD.executed_by_id)
        OR NOT (CAST(NEW.scope_source_models AS CHAR)
                <=> CAST(OLD.scope_source_models AS CHAR))
        OR NOT (CAST(NEW.scope_record_range AS CHAR)
                <=> CAST(OLD.scope_record_range AS CHAR))
    ) THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'PMS2 active migration batch scope is frozen';
    END IF;
END$$

DELIMITER ;

SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE()
  AND TRIGGER_NAME IN (
    'trg_billing_legacybatch_no_delete',
    'trg_billing_legacybatch_guard_update'
  )
ORDER BY TRIGGER_NAME;
