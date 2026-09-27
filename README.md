# 开发中药文化互动闯关判定服务基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

## 药材知识闯关模块

`quiz.py` 在基础层之上实现文化展示区的家庭闯关服务，规则如下：

- **题库冻结与换版**：题库先以草稿登记题目（含知识来源与适用年龄），冻结后不可修改；撤回题目会生成同谱系的新冻结版本，只影响之后建立的会话，不改写既有会话与已结算成绩。新会话必须绑定谱系内最新的冻结版本。
- **独立会话与事件归并**：每位参与者建立一次独立会话。现场终端断网补传的答题、提示、跳过事件按客户端序号归并：同序号同内容沿用第一次处理结果；同序号不同内容保留为待解释冲突，系统不擅自取舍，冲突未解决前会话不能结算，运营人员显式选择保留先到达事件或采用指定分支。
- **成绩与解锁**：成绩只采用每道题目客户端序号最小的有效答题事件；关卡解锁由会话绑定的题库版本与已完成前置条件推导（完成上一关全部适用题目解锁下一关）。结算写入不可改写的成绩快照，重复与并发结算返回同一结果。
- **隐私视图**：提示按参与者年龄过滤，儿童（未满 18 岁）明细不进入公开排行；家庭队伍只共享成员明确同意公开的进度。
- **后台查询**：题库版本列表与详情、会话成绩（含实际采用的有效事件）、关卡进度、完整事件日志、补传断点同步状态、冲突列表、公开排行与队伍进度。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、闯关模块、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、闯关规则（乱序同步、断点恢复、题库换版、并发结算、隐私视图）和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m night_market_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，冻结题库并跑通会话、乱序补传、重传与结算链路，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。

闯关模块接口：

- 题库：`POST /quiz/banks`、`POST /quiz/banks/questions`、`POST /quiz/banks/freeze`、`POST /quiz/banks/withdraw`，查询 `GET /quiz/banks?site_id=`、`GET /quiz/banks/detail?bank_id=`；
- 队伍与参与者：`POST /quiz/teams`、`POST /quiz/participants`、`POST /quiz/participants/consent`；
- 会话与事件：`POST /quiz/sessions`、`POST /quiz/sessions/events`（批量补传）、`POST /quiz/sessions/settle`、`POST /quiz/conflicts/resolve`；
- 查询：`GET /quiz/sessions/score|progress|events|sync?session_id=`、`GET /quiz/sessions/question?session_id=&question_id=`、`GET /quiz/conflicts?site_id=&status=`、`GET /quiz/leaderboard?site_id=`、`GET /quiz/teams/progress?team_id=`。
