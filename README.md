# 实验室仪器校准与方法验证

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8309`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、放行闸门（隔离/失效/超期/适用范围）和跨对象校验。
- `src/repository.py`：SQLite建表、单事务（`BEGIN IMMEDIATE`）、乐观锁和实体版本快照。
- `src/field_store.py`：现场端离线登记库（无网络时也可登记整批校准记录）。
- `src/batch_service.py`：批次回传合并、冲突处理、双人复核联动放行、到期风险视图。
- `src/service.py`：单用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和批次并发测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --field-db ./field.db --port 8309
```

服务启动时会自动建表。`--host`可修改监听地址，`--today`可覆盖当前日期（演示/测试用）。

## 核心对象

- `instrument`：仪器状态；`calibration`：校准记录；`method`：方法版本；`result`：检测结果。
- 校准批次（`calibration_batches` / `batch_entries` / `batch_reviews`）把现场登记、回传和复核按同一批次号串起来。

## 校准批次流程

1. **现场离线登记**：异地校准期间，在现场库登记每条记录——仪器（名称/序列号）、方法版本、
   通过/失败、不确定度、到期日、样品测量值。同一批次号可用更高`batch_version`更正重登记。
2. **网络恢复整批回传**：`upload`（或直接`merge`）在中心库**单个事务**内创建/更新仪器、方法、
   校准记录和检测结果，任何一步失败整批回滚，不会出现各对象成功一半。
3. **幂等与冲突**：
   - 同一批次号 + 同一版本重复提交只算一次（返回`duplicate`，不产生重复对象）；
   - 更早的版本晚到返回`stale`，不写入；
   - 更高版本覆盖旧版本，旧版本产生的仪器/校准/结果记录仍保留可查；
   - 并发回传由`BEGIN IMMEDIATE`串行化，响应负载始终反映已提交的最新批次版本
     （后到的高版本胜出，`applied`标记本次提交是否真正落库）。
4. **放行闸门**：回传时若方法版本已失效（revoked/非validated）、仪器被隔离、校准已超期或方法
   不适用该仪器，结果置为`held`并保留全部现场数据，绝不直接放行。
5. **双人复核**：两名**不同**人员（且都不能是上传者）分别复核；第二次复核在单事务内联动更新
   仪器状态（通过→active/失败→quarantined）、方法适用范围（现场登记的方法经双人复核转为
   validated并纳入仪器适用范围）和待放行结果（闸门通过才released，否则继续held）。
6. **整改后复检**：阻断项消除后（如启用新方法版本并重新指向结果），`recheck`重新评估held结果。
7. **历史可查**：实体每次变更写入`entity_versions`，旧结果/旧状态随时可查；全程写审计日志。
8. **到期风险**：`GET /api/risk/report`汇总已过期、30天内到期、隔离中的仪器和held结果，
   消除离线期间的到期盲区（可用`?within_days=`调整窗口）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/entities/versions/<id>`：读取对象全部历史版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `POST /api/field/batches`：现场离线登记批次。
- `GET /api/field/batches`：查看现场批次（`?include_uploaded=false`只看待回传）。
- `POST /api/batches/<batch_no>/upload`：把已登记的现场批次整批回传合并。
- `POST /api/batches/merge`：直接提交批次负载（`batch_no/batch_version/entries`）。
- `GET /api/batches` / `GET /api/batches/<batch_no>`：批次列表/详情（含每条记录的闸门原因）。
- `POST /api/batches/<batch_no>/reviews`：复核签名，两人两签后联动定稿。
- `POST /api/batches/<batch_no>/recheck`：整改后重新评估held结果。
- `GET /api/risk/report`：到期/隔离/held风险汇总。

批次合并响应中的`status`：`merged`（本次版本落库）、`duplicate`（同版本重复提交）、
`stale`（旧版本晚到）、`superseded`（提交后被并发更高版本赶超）；`batch_version`始终为
当前最新版本，`submitted_version`为本次提交版本，`applied`表示是否本次落库。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

校准周期、误差和放行规则是可演示的业务模型，不替代实验室质量体系或计量认证。
