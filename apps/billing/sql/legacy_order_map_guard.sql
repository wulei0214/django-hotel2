-- PMS2 旧订单-新预订关系映射守卫（迁移方案 §1.5.2 数据库层 / R5-04-B）
-- 目标表 billing_legacyorderreservationmap（Django 默认表名）。
-- 保护：确认事实创建后一律不可 UPDATE / 不可 DELETE（改映射只能追加 supersedes 新行）。
-- 安装：python manage.py install_legacy_map_guards
-- 校验：python manage.py check_legacy_map_guards（CI 强制）
-- 注意：SQLite 测试环境无触发器，模型/QuerySet 层守卫是该环境唯一防线。

DROP TRIGGER IF EXISTS trg_billing_legacyordermap_no_update;
DROP TRIGGER IF EXISTS trg_billing_legacyordermap_no_delete;

DELIMITER $$

CREATE TRIGGER trg_billing_legacyordermap_no_update
BEFORE UPDATE ON billing_legacyorderreservationmap
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 legacy order-reservation map is immutable';
END$$

CREATE TRIGGER trg_billing_legacyordermap_no_delete
BEFORE DELETE ON billing_legacyorderreservationmap
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = 'PMS2 legacy order-reservation map cannot be deleted';
END$$

DELIMITER ;

SELECT TRIGGER_NAME, EVENT_OBJECT_TABLE, EVENT_MANIPULATION, ACTION_TIMING
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = DATABASE()
  AND TRIGGER_NAME IN (
    'trg_billing_legacyordermap_no_update',
    'trg_billing_legacyordermap_no_delete'
  )
ORDER BY TRIGGER_NAME;
