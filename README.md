# 考古地层序列核定 API

合并各探方的地层清单与带记录号的早晚关系，核定某一地层在**所有合规的全区
排列**中能取到的最早、最晚名次，并各附一份完整可行排列。

* FastAPI 应用，交互文档在 `/docs`
* SQLite（WAL）保存分区、地层、记录、批次与已发布版本
* 名次只做可达性计算，不枚举排列：`最早 = 1 + |祖先|`，
  `最晚 = n - |后代|`，再过滤拓扑序得到取得该名次的排列

## 启动

```bash
docker compose up --build
# 文档：http://localhost:8000/docs
# 健康检查：http://localhost:8000/health
```

`docker-compose.yml` 会把 SQLite 放到命名卷 `strat-data`，并默认写入演示数据
（`STRAT_SEED_DEMO=true`）。无需 Docker 时：

```bash
python3 -m pip install -r requirements.txt
STRAT_DB_PATH=./data/strat.db STRAT_SEED_DEMO=true \
  python3 -m uvicorn app.main:app --reload
```

## /docs 演示：撤证后重查名次

1. `POST /demo/seed`：建立 T1/T2 演示数据（重复调用不会重复写入）。响应给出
   `M2` 的 `layer_id` 和两个区当时的 `version`。
2. `GET /ranks/{layer_id}`：`M2` 的 `earliest_rank=2`、`latest_rank=5`。
3. `GET /areas/T2` 记下 `version`，`POST /areas/T2/batches` 只撤回
   `R-T2-002`（`base_version` 填该版本）。
4. 再查 `GET /ranks/{layer_id}`：名次**不变**——`R-T2-001` 还记录着同一对
   早晚关系。
5. 重新读取 T2 版本，再提交一单撤回 `R-T2-001`；重查后
   `earliest_rank` 变为 `1`。
6. 用 T1 的版本提交撤回 `R-T1-004`；重查后 `latest_rank` 变为 `6`。

演示数据中的跨探方关系 `R-T1-004`（T2 的 M2 早于 T1 的 L4）说明记录号由
**持有记录的分区**签发，但两端地层可以属于别的探方。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查与已发布站点版本 |
| POST | `/demo/seed` | 建立/读取演示数据 |
| POST/GET | `/areas` | 登记/列出分区 |
| GET | `/areas/{code}` | 分区详情（含提交所需 `version`） |
| POST/GET | `/areas/{code}/layers` | 分区地层清单 |
| GET | `/layers/{id}`、`/areas/{code}/records` | 地层与记录（可含已撤回） |
| GET | `/relations` | 当前已发布的合并关系（按同一早晚关系分组） |
| POST | `/areas/{code}/batches` | **整单**补录/撤回 |
| GET | `/areas/{code}/batches`、`/batches/{id}` | 批次审计 |
| GET | `/ranks/{layer_id}` | 首末名次 + 两份可行排列 |

## 语义要点

* **版号与整单退回**：提交携带 `base_version`。同区先有别单或地层清单变更
  生效，则版本不符，本单整单退回（`409 area_version_conflict`），有效关系
  不变。
* **原子性**：批次内的补录与撤回一次性生效；任一校验失败、成环或写入失败
  均回滚，保留原有效关系。
* **重复记录**：同一对早晚关系可有多条记录；撤回其中一条关系仍在，全部撤回
  才解除。
* **并发**：写事务以 `BEGIN IMMEDIATE` 串行化，读者使用 WAL 快照，始终看到
  最近一次已发布的整版关系；核定期间其他分区仍可提交。
* **成环**：整单生效后若形成环，返回 `409 cycle_detected`，列出环上地层、每条
  边的记录号以及全部相关记录号。
* **记录号**：分区内唯一且不可复用（含已撤回记录号）。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
# 或用容器跑同一套测试（tests 服务位于 test profile）
docker compose --profile test run --rm --build tests
```

测试覆盖：图上界与暴力枚举对拍、首末名次与见证排列、重复记录撤回、跨区成环、
版号冲突、整单回滚、并发写入串行化、读快照隔离、OpenAPI 文档可用性。
