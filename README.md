# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址、采样周期 |
| 数据源采样清单 | `sampling` | 采样轮次 / 派发记录 | 轮次、来源版本、接入地址、截止分钟 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 采集报告 | 报告编号、采集源、统计周期 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 数据源采样清单

采集调度不再直接读数据源配置，而是从「数据源采样清单」领用待执行项。规则集中在
`backend/app/services/sampling.py`：

- **按启用状态与周期编排**：只有「已启用」的来源生成轮次；某分钟能落轮次取决于该来源
  采样周期（如 10 分钟只在 :00、:10、:20… 对齐）。`POST /api/sampling/rounds`
  传 `start`/`end`（或 `start` + `count`）即可批量编排。
- **来源版本**：来源的接入地址与采样周期一变就登记新的来源版本。轮次和派发记录都存
  `来源版本`，地址、周期、截止分钟取自同一条版本，成套生效，不会跨版本拼接。
- **一轮只派发一次**：同一来源同一轮次只有一条采样清单记录；领用即生成唯一派发记录，
  并发领取只有一个能成功，其余返回已有派发编号。
- **停用与领用并发**：领用、确认、停用共用同一把锁。停用后未派发的轮次和尚未确认的
  派发记录全部作废，不能再确认；还没开始处理时来源被停用，领用会被直接拒绝。
- **午间换地址**：尚未领用的轮次会在换地址时整轮刷新成新版本（地址、周期、截止分钟
  来自同一版本）；已领用/已派发的轮次保持原快照，保证派发记录与安排一致。

接口一览：

```text
GET  /api/sampling/summary                     采样看板统计
GET  /api/sampling/items                       采样清单（status、source_id、round_key、keyword）
POST /api/sampling/rounds                      编排采集轮次
POST /api/sampling/items/{id}/claim            领用待执行项，生成派发记录（待确认）
GET  /api/sampling/dispatches                  派发记录（status、source_id、round_key、valid）
POST /api/sampling/dispatches/{id}/confirm     确认派发
GET  /api/sampling/versions?source_id=         来源的地址/周期版本历史
PUT  /api/datasource/{id}                      修改接入地址/采样周期，登记新版本
```

测试：`cd backend && .venv/bin/pip install pytest httpx && .venv/bin/python -m pytest tests -q`
