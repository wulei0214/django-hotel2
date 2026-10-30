# 界面与页面说明

## 当前页面状态

| 页面 | 路径 | 技术 | 状态 | 说明 |
|---|---|---|---|---|
| 顾客端新预订页 | `/reserve/` | Django Template + 原生 JavaScript | PMS 2.0 Phase 2 已新增 | 调用 API v1，展示日期、房型、逐晚价格与预订流程 |
| 顾客端旧预订页 | `/booking/` | 历史 Django 模板 | 暂时保留 | 仅作兼容和回归对照，不作为新页面 |
| 员工登录/后台 | `/login/`、`/home/`、`/admin/` | Django 模板 / Django Admin + SimpleUI | 历史页面并存 | 员工端全面切换新领域模型尚未完成 |
| 前台入住管理 | `/checkin/` | 历史 Django 页面 | 待接入 PMS 2.0 新模型 | 不能假设已使用 Reservation / Stay / Folio 新服务 |

## 新顾客端预订页

当前页面访问 `/reserve/`，由 `apps/api/pages.py` 渲染 `templates/public/booking.html`。前端使用原生 JavaScript 请求：

- `GET /api/v1/availability`：按日期和房间数量查询房型可售情况及逐晚价格。
- `POST /api/v1/reservations`：提交入住人、联系电话、房型、日期、房间数和人数。
- `GET /api/v1/reservations/{booking_no}`：用预订编号和联系电话查询订单。
- `POST /api/v1/reservations/{booking_no}/cancel`：取消待支付订单。
- `POST /api/v1/payments/intents` 与回调：仅开发/测试环境展示模拟支付。

所有金额、库存和订单状态以后端响应为准；浏览器不能自行决定可售数量或支付成功。

## UI 截图维护规则

1. 页面路由、字段、交互发生变化时，同一 PR 必须更新本文件。
2. 新截图必须从当前代码运行后的真实页面截取，不得用旧页面截图冒充新页面。
3. 截图文件建议放在 `docs/screenshots/`，命名包含页面和版本日期，例如 `reserve-2026-10-30.png`。
4. 旧截图如仍保留，必须明确标注“历史版本/旧页面”，不得放在 README 的“当前界面”区域。
5. 当前仓库已有 `docs/1.png` 至 `docs/9.png` 等历史截图；它们不代表本次新增的 `/reserve/` 页面。
6. 新预订页目前尚未附运行态截图。完成本地数据库初始化并运行页面后，再补真实截图；不使用示意图替代真实界面。

## 页面验收清单

- [ ] 日期默认值按酒店物业时区计算，离店日晚于入住日。
- [ ] 房型列表与逐晚价格来自 API，不在前端硬编码。
- [ ] 查询无房、价格缺失、网络错误均有明确提示。
- [ ] 提交按钮防止重复点击；请求带幂等键。
- [ ] 预订查询要求订单号与联系电话匹配。
- [ ] Mock 支付仅在 dev/test 可见，生产不提供模拟扣款入口。
- [ ] 桌面和窄屏布局均可操作。
- [ ] 最新截图与页面版本一致。
