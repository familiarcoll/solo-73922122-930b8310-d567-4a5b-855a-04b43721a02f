# 工资支付单 · 银行分拆到账回执对账

把银行分拆到账回执归集到工资支付单，生成可入仓的实付依据。
FastAPI + SQLite 单容器应用，含浏览器操作页面与 REST API。

## 业务规则

- 同月最多导入 **8 张支付单、16 条回执**；单据/回执均含唯一编号、员工号、正整数金额（单位：分）。
- 每张支付单可由 **同一员工 1～3 条** 回执组成，回执金额之和必须等于支付单应付额；每条回执全局只能使用一次。
- 建议算法先求「完整匹配支付单数量最多」的最优解（最大基数匹配，记忆化 DFS），
  再用禁止法求所有最优方案的**配对交集**：交集内的标记为「确定」，其余有合法组合的标「待核」，无合法组合的单列展示。
- 网页可勾选确定/待核组合后整组确认，并下载已确认实付依据 CSV（UTF-8 BOM，Excel 可直接打开）。
- 确认后的支付单与回执立即退出匹配池；补录回执后建议按当前开放数据重新计算，其他员工的补录不改变既有确认。
- 确认接口在 `BEGIN IMMEDIATE` 事务内拿锁后重新校验：双窗口并发确认不会重复占用回执，**整组成功或全不改**；失败与重启后已确认依据持久保留。
- **过期建议不可入库**：建议携带其所属已提交状态的签名依据令牌。同员工补录（即使金额仍相等、回执未被占用）后，旧页面的整组确认会被拒绝（409 `stale_suggestion`）并提示刷新；其他员工的补录不阻止本组确认。刷新后建议重建，待核组合需人工重新选择，页面不会自动重提旧选择。补录与确认并发时，确认在写锁内逐员工核对版本，过期请求同样被拒，整组不写入。

## 过期建议（stale suggestion）机制

1. 每个员工有一行 `employee_revisions` 版本号，该员工的支付单/回执每发生一次补录、导入、确认或清空，就 +1（只增不减）。
2. `GET /api/state` 在同一个已提交读快照内计算建议并签发 `suggestion_basis` 令牌：令牌内含各员工版本，经 HMAC-SHA256 签名，页面无法伪造。签名密钥由 `RECON_TOKEN_SECRET` 指定，未指定时在库内生成并持久化，重启后旧令牌仍可正常校验。
3. `POST /api/confirm` 必须携带 `basis`。服务端在 `BEGIN IMMEDIATE` 写锁内重新读取版本，只比对本组涉及员工的版本：
   - 任一员工版本不一致 → 409 `{"code":"stale_suggestion"}`，整组不写入；
   - 仅其他员工变化 → 不影响本组；
   - 校验通过后再执行原有的金额/员工/1～3 条/回执独占校验，全部通过才整组落库。
4. 页面不自动轮询，旧页面得以保留补录前的建议用于复现拦截；过期后确认按钮禁用，必须先刷新建议再人工选择。

## 快速启动（Docker Compose）

```bash
docker compose up --build
# 浏览器入口
open http://localhost:8000/
```

SQLite 数据文件保存在命名卷 `recon-data`（容器内 `/data/recon.db`），容器重启数据不丢。

## 本地开发启动

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 浏览器打开 http://localhost:8000/
```

环境变量 `RECON_DB_PATH` 可覆盖数据库路径（默认 `data/recon.db`）；
`RECON_TOKEN_SECRET` 可固定建议依据令牌签名密钥（多副本部署时统一设置）。

## 页面操作

1. 「载入演示数据（2026-10）」一键导入 5 张支付单 + 11 条回执：
   - 3 个确定配对（含 2 条、1 条、3 条回执三种形态）；
   - E004 存在两种等价 4000 组合 → 全部列为「待核」，不进确定区；
   - E005 无回执（待补录），另有一条 E999 孤立回执。
2. 「演示：补录 E005 回执」可观察建议自动重算，PO20261005 进入确定区，其他员工结论不变。
3. 勾选配对 → 「确认所选配对」；到「已确认实付依据」区下载 CSV。
4. 也可用 `data/sample_orders.csv`、`data/sample_receipts.csv` 走 CSV 导入流程。
5. **过期建议演示**（复现「旧网页把已失去唯一性的建议入库」问题）：
   1. 点「载入过期建议演示（E710）」：PO7100 以 RC7100 为确定配对；
   2. 点「另一窗口：同员工补录 100 分」（模拟另一个浏览器窗口补录 E710 第二条 100 分回执），
      本页**不刷新**，仍显示补录前的确定配对；
   3. 点「确认所选配对」：服务端返回 409 `stale_suggestion` 并提示刷新，整组未写入
      （PO7100/PO8100 仍 open，RC7100/RC7101 仍 open）；
   4. 点「刷新建议」：PO7100 变为待核，两种 100 分组合均不预勾选，人工选择后可确认；
   5. 再用「另一窗口：其他员工补录 100 分」验证：本页只需勾选 PO7100（E710）即可确认成功，
      其他员工的变化不阻止本组；E810 自身的建议依据已变，若要确认它需先刷新。

## REST API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 浏览器页面 |
| GET | `/api/state` | 当前建议 + `suggestion_basis` 依据令牌 + 已确认依据 + 全量单据/回执 |
| POST | `/api/import/orders` | 上传支付单 CSV（multipart 字段 `file`） |
| POST | `/api/import/receipts` | 上传回执 CSV（支持补录） |
| POST | `/api/import/orders-json` | JSON 导入支付单 `{"orders":[{order_no,employee_no,amount,period?}]}` |
| POST | `/api/import/receipts-json` | JSON 导入回执 `{"receipts":[{receipt_no,employee_no,amount,period?}]}` |
| POST | `/api/confirm` | 整组确认 `{"items":[{...}],"basis":"<suggestion_basis>"}`；冲突或建议过期返回 409 且全不改 |
| GET | `/api/evidence.csv` | 下载已确认实付依据（无确认记录时 404） |
| POST | `/api/demo` | 载入演示数据（先清空） |
| POST | `/api/demo/supplement` | 演示补录 E005 回执 |
| POST | `/api/demo/stale` | 载入过期建议演示（E710 确定配对 + E810 对照） |
| POST | `/api/demo/stale/supplement-same` | 模拟另一窗口同员工补录 E710 回执 |
| POST | `/api/demo/stale/supplement-other` | 模拟另一窗口其他员工补录 E810 回执 |
| POST | `/api/reset` | 清空全部数据 |

## 自测

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q
```

- `tests/test_matching.py`：300 组随机数据用全量暴力枚举做 oracle，校验最大匹配数、最优方案交集（确定配对）、待核/不可配分类及确认校验规则。
- `tests/test_api.py`：导入上限与校验、确定/人工确认、冲突整组回滚、补录重算、重启持久化、CSV 导入与依据下载、双线程并发确认不重复占用；
  以及过期建议：同员工补录后旧页面确认被拒且整组未写入、其他员工补录不阻止确认、刷新后可人工选择且不自动重提旧选择、缺令牌/伪造令牌被拒、重启后令牌仍可校验、补录与确认并发时过期请求被拒。

