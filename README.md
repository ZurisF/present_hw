# 六合餐厅点餐与配送 Agent

这是 day11 LangChain 课程项目的完整实现版本：前端使用 Vite，后端使用 FastAPI；LangChain Agent 通过工具访问 MySQL 菜单/预订、Redis FAQ、BGE + Milvus 菜单检索和高德 MCP 配送服务。项目保留菜单浏览、语义搜索、口味与特色菜推荐、预订与查单、配送 API 和流式对话。

默认 `AGENT_PROVIDER=mock` 使用项目内的确定性 LangChain ChatModel。它仍经过 `create_agent`、工具调用和 InMemorySaver 会话图，不调用外部模型；设置 `AGENT_PROVIDER=openai` 并配置密钥后才使用 ChatOpenAI。预订的完整信息收集和二次确认由服务端流程负责，模型不能自行授权写操作。

## 技术栈

| 层 | 技术 | 作用 |
| --- | --- | --- |
| 前端 | Node.js、Vite、原生 JavaScript | 独立开发服务器，代理 API，展示菜单、预约、配送和 SSE 对话 |
| API | Python 3.12、uv、FastAPI、Pydantic | REST API、参数校验、生命周期管理、SSE 流式聊天 |
| Agent | LangChain `create_agent`、LangGraph `InMemorySaver`、ChatOpenAI/本地 Mock | 工具调用和会话级对话状态；mock 默认启用 |
| 结构化数据 | MySQL 8、SQLAlchemy、PyMySQL | 菜单、预订、幂等操作日志、预约对话槽位 |
| FAQ | Redis、SequenceMatcher、Jaccard | FAQ 哈希记录与模糊问题匹配 |
| 向量检索 | BGE-base-zh、LangChain HuggingFace、PyMilvus | 菜单语义召回；MySQL 始终提供价格、口味标签和供应状态等事实 |
| 地图 | LangChain MCP Adapter、高德地图 MCP | 地址解析、驾车/骑行路线与直线距离 |

依赖已经写入 `pyproject.toml` 和 `frontend/package.json`，但本次没有安装依赖、生成锁文件、下载 BGE 模型或启动任何数据库/向量服务。

## 目录

```text
agent/
  langchain_assistant.py    create_agent、工具集、模型选择和流式输出
  mock_chat_model.py        生成真实 LangChain tool call 的本地 mock 模型
  prompt/system_prompt.txt  Agent 行为和安全边界
frontend/
  index.html                Vite 前端入口
  app.js                    菜单、预约、配送、SSE 对话交互
  vite.config.js            3000 端口及 FastAPI 代理
restaurant_app/
  database.py               SQLAlchemy 表定义、MySQL 连接与菜单初始化
  repository.py              MySQL 菜单、预订、幂等和会话数据访问
  faq.py                     Redis FAQ 与字符串/Jaccard 匹配
  vector_search.py           本地 BGE Embeddings + 远端 Milvus
  delivery.py                Demo 地图和 MCP Adapter 高德地图
  mock_agent.py              服务端控制的预约信息收集与确认 SOP
sql/schema.sql               MySQL 建表和演示菜单数据
main.py                      FastAPI 路由与应用生命周期
```

## 环境准备

需要 Python 3.12、uv、Node.js，以及可访问的 MySQL、Redis、远端 Milvus 和本地 BGE 模型目录。Milvus 按本项目约束配置为远端 URI；代码不会配置或启动本地 Milvus。BGE 模型需要由课程环境或使用者预先放到 `models/bge-base-zh`，应用设置 Hugging Face 离线模式，不会下载模型。

复制 `.env.example` 为 `.env`，填写 MySQL、Redis、远端 Milvus 和地图服务参数。不要把真实密码、token 或模型密钥提交到仓库。MySQL 数据库本身需先创建，例如 `menu`；应用启动时按 SQLAlchemy metadata 建表，并在菜单表为空时写入演示菜单。也可以先执行 `sql/schema.sql`。

依赖声明和运行命令供后续环境使用；本次实现没有执行安装：

```bash
uv sync
uv run uvicorn main:app --reload --port 8000
```

另开终端启动 Vite 前端：

```bash
cd frontend
npm install
npm run dev
```

前端默认访问 `http://localhost:3000`，API 地址为 `http://localhost:8000`，OpenAPI 文档为 `http://localhost:8000/doc`。数据库、Redis、远端 Milvus、BGE 模型或配置缺失时，后端会在启动阶段明确失败，不会静默切换到 SQLite、内存向量库或在线模型下载。

## Agent 与工具

Agent 将下列能力注册为 LangChain 工具：

- `search_menu`：先用 BGE 向量在 Milvus 排序候选，再以 MySQL 菜单事实为准。
- `check_menu_item`：核实菜品、价格和可供应状态。
- `faq_search`：在 Redis FAQ 中按 SequenceMatcher 与 Jaccard 相似度匹配。
- `check_delivery`：通过 MCP Adapter 组合地址解析与路线/距离工具，服务不可用和地址无法解析使用不同结果。
- `reservation_lookup`：按编号、手机号或当前会话查单。
- `create_reservation`：只有应用传入已确认的预订上下文才执行写入；否则返回 `confirmation_required`。
- 高德 MCP 工具：配置真实地图 MCP 后由 Adapter 发现并加入 Agent 工具集。

`InMemorySaver` 为 Agent 图保存当前进程中的对话消息。预约草稿另写入 MySQL `chat_sessions`，因此预约收集阶段可在进程重启后继续。

## 预订 SOP 与异常处理

1. 用户表达预订意向后，服务端创建稳定 `request_id` 并持久化预约草稿。
2. 从对话中提取姓名、手机号、人数、儿童人数、到店时间、座位偏好、主菜偏好和备注；缺项时继续询问。
3. 校验人数范围、儿童人数、手机号、时间格式和未来时间；歧义或非法字段继续追问，不提交。
4. 字段齐全后完整复述摘要。用户只能通过清晰确认词提交；可补充修改或取消。
5. MySQL 用 `agent_operations` 先记录 `PENDING` 和内容哈希，再通过唯一请求编号写入预订、标记成功并记事件。同一编号同一内容复用既有结果；同编号不同内容返回冲突。
6. 启动恢复时扫描 `PENDING` 操作，复用原 `request_id` 核对或恢复，避免重复预订。

数据库异常不能伪装成“没有结果”或“预订成功”；系统会保留请求编号和恢复状态并提示稍后核对。预订列表必须按手机号或会话过滤，避免无条件暴露其他用户数据。

## 地图与长尾情况

- `MAPS_PROVIDER=amap` 强制调用高德 MCP；地图 MCP 无法连接、缺少工具或返回不完整距离时返回服务不可用。
- 地址解析成功但没有可用坐标时返回“地址无法解析”，与网络/工具故障区分。
- 直线模式调用距离工具，不返回行程时间；驾车和骑行分别调用对应路线工具。
- 未配置地图 URL 时可使用 `MAPS_PROVIDER=demo` 的确定性演示坐标；演示结果不代表真实导航数据。
- 默认配送半径 5 公里，餐厅位置和半径可以通过环境变量调整。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/health` | 显示 Agent、数据库、FAQ、Milvus 和地图配置 |
| `GET` | `/api/menu` | 菜单和分类、口味、关键词筛选 |
| `GET` | `/api/search/menu?query=清淡的鱼` | Milvus 菜单语义检索 |
| `GET` | `/api/recommendations/flavor?flavor=清淡` | 口味推荐 |
| `GET` | `/api/recommendations/featured` | 特色菜推荐 |
| `POST` | `/api/reservations` | 显式确认后创建预订 |
| `GET` | `/api/reservations?phone=...` | 按手机号查询 |
| `GET` | `/api/reservations/{reservation_id}` | 按预订编号查询 |
| `POST` | `/api/delivery` | 地图配送范围查询 |
| `POST` | `/api/chat` | LangChain Agent JSON 对话 |
| `POST` | `/api/chat/stream` | LangChain Agent SSE 流式对话 |

配送请求示例：

```json
{
  "address": "上海市浦东新区陆家嘴环路1000号",
  "travel_mode": "2"
}
```

`travel_mode`：`1` 骑行、`2` 驾车（默认）、`3` 直线距离。
