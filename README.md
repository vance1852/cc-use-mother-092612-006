# 开发中药文化互动闯关判定服务基础服务

本项目提供中医文化夜市的通用后台基础能力，负责活动机构、服务站点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。新的业务模块可以在这些稳定边界之上增加领域状态、规则和接口。

`night_market_foundation.quiz` 在基础层之上实现文化展示区的「药材知识闯关」：

- 活动开始前冻结题库版本（题目、知识来源、适用年龄一并快照并生成内容哈希），冻结后不可改写；撤回题目只生成新版本，仅影响之后开启的会话，已结束成绩保持不变。
- 每个家庭以化名开启一次独立会话，会话钉住开启时的最新冻结版本；关卡是否解锁只取决于该版本的前置图和已生效事件。
- 现场终端断网补传按 `(会话, 客户端序号)` 归并：重复补传沿用第一次处理结果；同序号不同内容全部保留为待解释冲突，由运营人员显式裁决，系统不自动选取。
- 结算落库后不可改写，成绩附带「实际采用的有效事件」列表与内容哈希，可逐条复核。
- 儿童只能看到适龄提示且明细不进公开排行；家庭视图只共享成员明确同意公开的进度，另提供运营/家庭/公开三种事件视图。

## 目录

- `src/night_market_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由、闯关模块和离线验收；
- `tests/`：基础规则、事务边界、接口路由、闯关乱序同步/断点恢复/题库换版/并发结算/隐私视图和端到端验收测试。

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

验收命令会在临时 SQLite 数据库中登记活动机构、操作者、站点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m night_market_foundation.api --database night_market.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计链继续保留。
