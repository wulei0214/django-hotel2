-- =============================================================================
-- 迭代-2 财务旧数据生产只读盘点脚本（GOV-02 裁定 5 交付物 / 裁定 3 前置证据）
-- -----------------------------------------------------------------------------
-- 文件性质：只读盘点。全部语句为 SELECT；不含任何 INSERT/UPDATE/DELETE/DDL、
--           不建表、不迁移、不 REVOKE、不 GRANT、不切读路径、不物理删除。
-- 基线来源：设计六份文档之《迭代-2_历史账务数据迁移与对账方案》§2 命令清单
--           +《迭代-2_财务与账本旧数据现状盘点》§6 + R4-01/R6-01/R6-02 修订查询
-- 设计基线：fe579479ede9baa000e020bd5dd17ae3fe852de9（六份统一代码事实基线）
-- 第六轮复审：R6-01~R6-04 设计层通过（REV6-AR-20261116 / Issue #7 评论 6915360118）
-- 用途：供具备生产库只读权限的运维/财务人员执行，结果回填《现状盘点》§6；
--       裁定 3（DEFAULT 归属）须以本脚本第 7 组产出的归属证据逐记录核验。
-- 敏感信息：本脚本仅 COUNT / SUM / MIN / MAX / 分类，不返回任何 PII 字段值
--           （不含手机号、证件号、顾客姓名明文）；GROUP_CONCAT 仅返回物业 code。
-- 执行纪律：只读 + 不迁移 + 不建表 + 不 REVOKE + 不暴露敏感信息（裁定 5 纪律）。
--           START TRANSACTION READ ONLY 提供技术兜底：事务内任何写操作将被拒绝。
-- 本地实测适配（2026-11-16）：设计文档原 SQL 的房号 JOIN
--   nr.number = CAST(r.房间号 AS CHAR) 在 MySQL 8 下报 1267 Illegal mix of collations
--   （旧表 room.房间号 为整数字段，CAST 后继承 general_ci；新表 inventory_room.number
--    为 0900_ai_ci）。本脚本两侧显式 COLLATE utf8mb4_unicode_ci 消除歧义，语义不变。
--   此为只读盘点脚本的可执行性适配，不改变设计判定口径、不触及任何裁定。
--   2026-11-17 追加适配：部分旧表为 utf8mb3，CAST 后直接挂 utf8mb4 排序规则
--   会报 1253；改用 CONVERT(... USING utf8mb4) 转码后再挂，语义不变。
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. 执行记录（由执行人现场填写，脚本不预置结果）
-- -----------------------------------------------------------------------------
-- 执行人：____________  执行时间：____________  目标库/schema：____________
-- 实例/版本：____________  只读账号：____________  结果回填位置：现状盘点 §6
-- 执行前置：与负责人确认只读账号；禁止使用应用写账号；禁止在执行库做任何变更前操作。

-- 强制只读事务（MySQL 8.0）：事务内出现任何 DML/DDL 直接报错，作为纪律兜底。
START TRANSACTION READ ONLY;

-- -----------------------------------------------------------------------------
-- 0.1 前置校验：确认表与关键列存在（列名/表名以生产 introspection 为准，
--     与设计基线不一致时先停下核对，不得凭本脚本推定 schema）
-- -----------------------------------------------------------------------------
SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND (TABLE_NAME, COLUMN_NAME) IN
       (('finance_record','金额'), ('finance_record','日期'), ('finance_record','类型'),
        ('folio','金额'), ('folio','日期'), ('folio','类型'), ('folio','order_id'),
        ('accommodation','状态'), ('accommodation','客房编号'),
        ('night_audit','营业日期'), ('room','房间号'),
        ('inventory_room','number'), ('inventory_room','property_id'),
        ('core_property','code'), ('billing_ledgerentry','entry_type'),
        ('billing_ledgerentry','amount'), ('billing_ledgerentry','business_date'))
 ORDER BY TABLE_NAME, COLUMN_NAME;
-- 期望：16 行全部返回。缺列/缺表 → 停止，回填"前置校验未通过"并记录差异。

-- =============================================================================
-- 裁定 5 八项统计（结果逐组回填《现状盘点》§6）
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. 行数（四张旧表全量规模）
-- -----------------------------------------------------------------------------
SELECT 'finance_record' AS tbl, COUNT(*) AS row_cnt FROM `finance_record`
UNION ALL SELECT 'folio',        COUNT(*) FROM `folio`
UNION ALL SELECT 'accommodation',COUNT(*) FROM `accommodation`
UNION ALL SELECT 'night_audit',  COUNT(*) FROM `night_audit`;

-- -----------------------------------------------------------------------------
-- 2. 金额分布（按旧类型聚合）
-- 注：CAST 对解析失败字符串返回 0 并产生 warning，故同时给出"全量行数"与
--     "仅合法金额行的 SUM"，以第 6 组解析失败数为口径校正，避免金额被低估。
-- -----------------------------------------------------------------------------
SELECT `类型`,
       COUNT(*) AS row_cnt_all,
       SUM(CASE WHEN `金额` REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$'
                THEN CAST(`金额` AS DECIMAL(12,2)) ELSE 0 END) AS amount_sum_valid_only
  FROM `folio`
 GROUP BY `类型`;

SELECT `类型`,
       COUNT(*) AS row_cnt_all,
       SUM(CASE WHEN `金额` REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$'
                THEN CAST(`金额` AS DECIMAL(12,2)) ELSE 0 END) AS amount_sum_valid_only
  FROM `finance_record`
 GROUP BY `类型`;

-- -----------------------------------------------------------------------------
-- 3. 日期范围（字符串日期列，MIN/MAX 为字典序，合法 YYYY-MM-DD 下等价时间序）
-- -----------------------------------------------------------------------------
SELECT 'finance_record' AS tbl, MIN(`日期`) AS min_date, MAX(`日期`) AS max_date
  FROM `finance_record`
UNION ALL SELECT 'folio',       MIN(`日期`),  MAX(`日期`)  FROM `folio`
UNION ALL SELECT 'night_audit', MIN(`营业日期`), MAX(`营业日期`) FROM `night_audit`;

-- -----------------------------------------------------------------------------
-- 4. 状态分布（Accommodation 订单状态；收款/押金列做规模参考）
-- -----------------------------------------------------------------------------
SELECT `状态`,
       COUNT(*) AS row_cnt,
       SUM(CASE WHEN `收款金额` REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$'
                THEN CAST(`收款金额` AS DECIMAL(12,2)) ELSE 0 END) AS received_sum,
       SUM(CASE WHEN `押金` REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$'
                THEN CAST(`押金` AS DECIMAL(12,2)) ELSE 0 END) AS deposit_sum
  FROM `accommodation`
 GROUP BY `状态`;

-- -----------------------------------------------------------------------------
-- 5. 孤儿数（旧凭证关联订单不存在）
-- -----------------------------------------------------------------------------
SELECT 'folio_orphan' AS metric, COUNT(*) AS cnt
  FROM `folio` f LEFT JOIN `accommodation` a ON f.`order_id` = a.`id`
 WHERE f.`order_id` IS NOT NULL AND a.`id` IS NULL;

-- -----------------------------------------------------------------------------
-- 6. 解析失败数（决定 C4 规模；日期含格式+真实存在+roundtrip 三重校验）
-- -----------------------------------------------------------------------------
SELECT
  SUM(CASE WHEN `金额` NOT REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$' THEN 1 ELSE 0 END)
    AS folio_amount_bad,
  SUM(CASE WHEN `日期` NOT REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            OR STR_TO_DATE(`日期`,'%Y-%m-%d') IS NULL
            OR DATE_FORMAT(STR_TO_DATE(`日期`,'%Y-%m-%d'),'%Y-%m-%d') <> `日期`
           THEN 1 ELSE 0 END) AS folio_date_bad
  FROM `folio`;

SELECT
  SUM(CASE WHEN `金额` NOT REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$' THEN 1 ELSE 0 END)
    AS finance_amount_bad,
  SUM(CASE WHEN `日期` NOT REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            OR STR_TO_DATE(`日期`,'%Y-%m-%d') IS NULL
            OR DATE_FORMAT(STR_TO_DATE(`日期`,'%Y-%m-%d'),'%Y-%m-%d') <> `日期`
           THEN 1 ELSE 0 END) AS finance_date_bad
  FROM `finance_record`;

-- -----------------------------------------------------------------------------
-- 7. 非 DEFAULT 归属证据（R4-01 六态判定，Accommodation 粒度）
--    裁定 3：DEFAULT 仅为候选，须以本结果逐记录人工确认，不得按代码默认值自动赋值。
--    房号相同仅为辅助证据；历史调房/跨物业复用号需人工确认。
-- -----------------------------------------------------------------------------
SELECT a.`id`                            AS accommodation_id,
       r.`房间号`                         AS legacy_room_no,
       COUNT(DISTINCT nr.`property_id`)  AS matched_property_count,
       GROUP_CONCAT(DISTINCT p.`code`)   AS matched_property_codes,
       CASE
         WHEN r.`房间号` IS NULL THEN '旧数据关系异常（订单未关联旧房间）'
         WHEN COUNT(DISTINCT nr.`property_id`) = 0 THEN '证据不足（新侧无匹配房间）'
         WHEN COUNT(DISTINCT nr.`property_id`) = 1
              AND MAX(p.`code`) = 'DEFAULT' THEN '唯一匹配 DEFAULT（候选证据，须人工确认）'
         WHEN COUNT(DISTINCT nr.`property_id`) = 1 THEN '唯一匹配非 DEFAULT（其他物业证据）'
         ELSE '同号多物业冲突（需人工核验）'
       END                               AS attribution_verdict
  FROM `accommodation` a
  LEFT JOIN `room` r            ON r.`房间号` = a.`客房编号`
  LEFT JOIN `inventory_room` nr ON nr.`number` COLLATE utf8mb4_unicode_ci = CONVERT(CAST(r.`房间号` AS CHAR) USING utf8mb4) COLLATE utf8mb4_unicode_ci
  LEFT JOIN `core_property` p   ON p.`id` = nr.`property_id`
 GROUP BY a.`id`, r.`房间号`
 ORDER BY a.`id`;
-- 回填要点：统计 attribution_verdict 六态各自行数；"唯一匹配 DEFAULT" 行数即
--          裁定 3 需人工逐记录确认的候选规模，不得直接作为已归属结果。

-- -----------------------------------------------------------------------------
-- 8. 新旧重复事实候选（R6-01 双轨：① 完整统计无 LIMIT；② 逐对抽样 LIMIT 200）
--    关系映射（legacy_order_reservation_map）属阶段 D 交付、尚未实施；
--    故所有候选订单关联状态一律"未建立（禁止判定 C2/C3）"。
-- -----------------------------------------------------------------------------
-- 8.① 完整统计（无 LIMIT）：逐旧收款候选数分布，完整性核对入口，不遗漏
SELECT f.`id`                        AS legacy_folio_id,
       f.`order_id`                  AS legacy_order_id,
       f.`金额`                       AS legacy_amount_raw,
       f.`日期`                       AS legacy_date,
       p.`code`                      AS matched_property_code,
       COUNT(le.`id`)                AS candidate_count,
       CASE WHEN COUNT(le.`id`) > 1 THEN '多候选（禁止自动判定 C2/C3）'
            WHEN COUNT(le.`id`) = 1 THEN '单候选（仍须人工核验 + 关系映射确认）'
            ELSE '无候选（订单关联未建立）' END AS candidate_match_status,
       '未建立（禁止判定 C2/C3；关系映射属阶段 D 交付）' AS order_association_status
  FROM `folio` f
  JOIN `accommodation` a         ON a.`id` = f.`order_id`
  JOIN `room` r                  ON r.`房间号` = a.`客房编号`
  JOIN `inventory_room` nr       ON nr.`number` COLLATE utf8mb4_unicode_ci = CONVERT(CAST(r.`房间号` AS CHAR) USING utf8mb4) COLLATE utf8mb4_unicode_ci
  JOIN `core_property` p         ON p.`id` = nr.`property_id`
  LEFT JOIN `billing_folio` bf   ON bf.`property_id` = nr.`property_id`
  LEFT JOIN `billing_ledgerentry` le ON le.`folio_id` = bf.`id`
                              AND le.`entry_type` = 'PAYMENT'
                              AND le.`amount` = -CAST(f.`金额` AS DECIMAL(12, 2))
                              AND le.`business_date` = STR_TO_DATE(f.`日期`, '%Y-%m-%d')
 WHERE f.`类型` = '收款'
 GROUP BY f.`id`, f.`order_id`, f.`金额`, f.`日期`, p.`code`
 ORDER BY candidate_count DESC, f.`id`;
-- 解读：candidate_count ≥ 2 = 同凭证存在多个同金额同日期新候选——禁止自动判定，
--       必须人工核验 + 关系映射（阶段 D）确认后才可标 C2/C3。

-- 8.② 逐对明细（LIMIT 200，人工抽样；完整性以 8.① 为准）
SELECT f.`id`                        AS legacy_folio_id,
       f.`order_id`                  AS legacy_order_id,
       f.`金额`                       AS legacy_amount_raw,
       f.`日期`                       AS legacy_date,
       p.`code`                      AS matched_property_code,
       bf.`id`                       AS candidate_new_folio_id,
       bf.`reservation_id`           AS candidate_new_reservation_id,
       le.`id`                       AS candidate_new_entry_id,
       le.`amount`                   AS candidate_new_amount,
       le.`business_date`            AS candidate_new_business_date,
       CASE WHEN COUNT(*) OVER (PARTITION BY f.`id`) > 1
            THEN '多候选（禁止自动判定 C2/C3）'
            ELSE '单候选（仍须人工核验）' END AS candidate_match_status,
       '未建立（禁止判定 C2/C3；关系映射属阶段 D 交付）' AS order_association_status
  FROM `folio` f
  JOIN `accommodation` a         ON a.`id` = f.`order_id`
  JOIN `room` r                  ON r.`房间号` = a.`客房编号`
  JOIN `inventory_room` nr       ON nr.`number` COLLATE utf8mb4_unicode_ci = CONVERT(CAST(r.`房间号` AS CHAR) USING utf8mb4) COLLATE utf8mb4_unicode_ci
  JOIN `core_property` p         ON p.`id` = nr.`property_id`
  JOIN `billing_folio` bf        ON bf.`property_id` = nr.`property_id`
  JOIN `billing_ledgerentry` le  ON le.`folio_id` = bf.`id`
                              AND le.`entry_type` = 'PAYMENT'
                              AND le.`amount` = -CAST(f.`金额` AS DECIMAL(12, 2))
                              AND le.`business_date` = STR_TO_DATE(f.`日期`, '%Y-%m-%d')
 WHERE f.`类型` = '收款'
 GROUP BY f.`id`, f.`order_id`, f.`金额`, f.`日期`, p.`code`,
          bf.`id`, bf.`reservation_id`, le.`id`, le.`amount`, le.`business_date`
 ORDER BY f.`id`, bf.`id`, le.`id`
 LIMIT 200;

-- 8.③ 新旧重复预检（C2 判定输入）：新账本 legacy 前缀凭证应为 0（未迁移）
SELECT COUNT(*) AS legacy_prefixed_entry_cnt_should_be_zero
  FROM `billing_ledgerentry`
 WHERE `reference` LIKE 'legacy:%';

-- 8.④ FinanceRecord 收入逐条分类（R6-02：全记录可见，LEFT JOIN + 逐条 classification）
-- 分类口径：金额解析失败/日期解析失败(含不存在与 roundtrip)/订单ID格式非法/订单不存在
--           → C4；无关联订单 → C5 候选（资金性质待人工判定）；旧订单存在 → 待关系映射核验。
SELECT fr.`id`                          AS legacy_finance_id,
       fr.`关联订单`                     AS legacy_order_ref_raw,
       fr.`金额`                         AS legacy_amount_raw,
       fr.`日期`                         AS legacy_date_raw,
       CASE
         WHEN fr.`金额` NOT REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$' THEN '金额解析失败（C4）'
         WHEN fr.`日期` NOT REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN '日期格式非法（C4）'
         WHEN STR_TO_DATE(fr.`日期`, '%Y-%m-%d') IS NULL THEN '日期不存在（C4）'
         WHEN DATE_FORMAT(STR_TO_DATE(fr.`日期`, '%Y-%m-%d'), '%Y-%m-%d') <> fr.`日期`
              THEN '日期 roundtrip 不一致（C4）'
         WHEN fr.`关联订单` IS NULL OR TRIM(fr.`关联订单`) = '' THEN '无关联订单（C5 候选，资金性质待人工判定）'
         WHEN TRIM(fr.`关联订单`) NOT REGEXP '^[0-9]+$' THEN '订单ID格式非法（C4）'
         WHEN NOT EXISTS (SELECT 1 FROM `accommodation` ax
                           WHERE ax.`id` = CAST(TRIM(fr.`关联订单`) AS UNSIGNED)) THEN '订单不存在（C4）'
         ELSE '旧订单存在，待新预订关联核验'
       END                              AS classification,
       p.`code`                         AS matched_property_code,
       le.`id`                          AS candidate_new_entry_id,
       le.`amount`                      AS candidate_new_amount,
       le.`business_date`               AS candidate_new_business_date
  FROM `finance_record` fr
  LEFT JOIN `accommodation` a
         ON fr.`日期` REGEXP '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
        AND STR_TO_DATE(fr.`日期`, '%Y-%m-%d') IS NOT NULL
        AND DATE_FORMAT(STR_TO_DATE(fr.`日期`, '%Y-%m-%d'), '%Y-%m-%d') = fr.`日期`
        AND fr.`金额` REGEXP '^-?[0-9]+(\\.[0-9]{1,2})?$'
        AND TRIM(fr.`关联订单`) REGEXP '^[0-9]+$'
        AND a.`id` = CAST(TRIM(fr.`关联订单`) AS UNSIGNED)
  LEFT JOIN `room` r                ON r.`房间号` = a.`客房编号`
  LEFT JOIN `inventory_room` nr     ON nr.`number` COLLATE utf8mb4_unicode_ci = CONVERT(CAST(r.`房间号` AS CHAR) USING utf8mb4) COLLATE utf8mb4_unicode_ci
  LEFT JOIN `core_property` p       ON p.`id` = nr.`property_id`
  LEFT JOIN `billing_ledgerentry` le
         ON le.`entry_type` = 'PAYMENT'
        AND le.`property_id` = nr.`property_id`
        AND le.`amount` = -CAST(fr.`金额` AS DECIMAL(12, 2))
        AND le.`business_date` = STR_TO_DATE(fr.`日期`, '%Y-%m-%d')
 WHERE fr.`类型` = '收入'
 ORDER BY fr.`id`;

-- =============================================================================
-- 收尾：只读事务提交（无数据变更，仅结束只读事务）
-- =============================================================================
COMMIT;

-- 执行后动作（人工，不在本脚本内）：
-- 1) 将第 1~8 组结果回填《迭代-2_财务与账本旧数据现状盘点》§6，记录执行人/时间/库。
-- 2) 第 7 组"唯一匹配 DEFAULT"逐记录交人工确认，作为裁定 3 生效的前置证据。
-- 3) 本脚本执行结果不构成阶段 D 实施授权；签署与批准仍由负责人另行完成。
