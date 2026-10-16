# 六合餐厅点餐与配送服务

本项目提供一套餐厅网站和 FastAPI 后端，包含菜单展示、Milvus 向量检索、口味推荐、特色菜推荐、预订、预订查询、配送范围查询，以及可以调用配送服务的对话 Agent。

默认使用规则型 mock Agent 和本地 mock embedding，因此不需要模型账号、模型 API Key 或模型下载。项目通过 PyMilvus 客户端连接远端 Milvus/Zilliz 服务；不会在本机启动或安装 Milvus 服务。SQLite 保留菜单结构化事实和预订数据，向量检索后再以 SQLite 核实价格与供应状态。配送服务可以使用本地演示坐标，也可以接入高德地图官方 MCP Server。原项目的 LangChain/MySQL 扩展源码保留在 `agent/`，网站启动流程不会加载它。

## 功能

- **菜单展示：** 按菜品分类浏览、关键词搜索，展示价格、风味、供应状态和过敏原信息。
- **Milvus 向量检索：** 菜单搜索框、口味推荐和 Agent 的自然语言菜品推荐调用 Milvus；菜单事实再由 SQLite 核对。默认采用本地 Milvus Lite 和确定性 mock embedding。
- **口味推荐：** 按清淡、鲜香、麻辣、香辣、甜口、咸甜、浓郁七类风味筛选可供应菜品。
- **特色菜推荐：** 展示特色菜及其供应状态。
- **预订：** 登记联系人、人数、儿童人数、上海时间到店时间、座位偏好、主菜偏好和备注；提交前展示完整信息，用户明确确认后才写入。
- **预订查询：** 可按手机号查询近期预订，也可使用预订编号查询单条记录。
- **配送查询：** `POST /api/delivery` 支持骑行、驾车和直线距离，默认按所选方式的距离与 5 公里配送半径比较。
- **对话 Agent：** 可回答菜单、口味推荐、特色菜、预订、预订记录和“能送到 XX 吗”等问题。它以规则方式收集预订信息并等待二次确认，不声称调用了语言模型。
- **预订幂等和恢复：** 每次已确认预订使用稳定 `request_id`；写入记录保存操作账本和事件，进程重启后会恢复未完成操作。重复请求不会创建第二条预订。

## 启动

需要 Python 3.12 或更高版本。

```bash
cd LangChain/03-langchain-order-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
# 在 .env 中填写老师提供的 MILVUS_URI 和需要的 MILVUS_TOKEN
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

打开：

- 网站：<http://localhost:8000>
- Swagger 接口文档：<http://localhost:8000/doc>
- 健康检查：<http://localhost:8000/api/health>

## 高德 MCP 配置

无需配置地图凭据也可以启动网站；默认 `MAPS_PROVIDER=auto`，没有 MCP 地址时会使用本地演示地图数据。演示数据内置人民广场、陆家嘴、外滩、南京西路、徐家汇和静安寺等地址；其他地址按“地址无法解析”返回。健康检查会显示当前地图服务是 `demo` 还是 `amap`。

接入真实高德数据时，在高德开放平台申请 **Web 服务** Key，然后在 `.env` 设置：

```dotenv
MAPS_PROVIDER=amap
AMAP_MCP_URL=https://mcp.amap.com/mcp?key=你的高德Web服务Key
DELIVERY_RADIUS_KM=5
```

服务通过官方 Streamable HTTP MCP 端点调用 `maps_geo` 获取收货地址坐标，再按方式调用 `maps_direction_bicycling`、`maps_direction_driving` 或 `maps_distance`。餐厅坐标从 `RESTAURANT_LONGITUDE` 和 `RESTAURANT_LATITUDE` 读取；默认点位是人民广场近似坐标，实际部署时应填入餐厅的准确坐标。真实地图服务连接或工具调用失败时，API 返回 `503 MAPS_SERVICE_UNAVAILABLE`，不会静默改用演示结果。地址无法解析时返回作业要求的“地址无法解析，请重新输入”。

高德官方接入说明：[MCP Server 快速接入](https://lbs.amap.com/api/mcp-server/gettingstarted) · [创建应用和 Key](https://lbs.amap.com/api/mcp-server/create-project-and-key)

## Milvus 配置

项目使用远端 Milvus 服务，不会在本机安装或启动 Milvus。安装 Python 依赖只会安装 PyMilvus 客户端；服务端地址和凭据由老师或课程环境提供。首次启动时程序会连接该服务，创建 `restaurant_menu` collection（不存在时），并将 SQLite 菜单向量 upsert 到 collection。

```dotenv
MILVUS_URI=https://your-milvus-endpoint
MILVUS_COLLECTION=restaurant_menu
MILVUS_TOKEN=your-token-if-required
```

将 `.env.example` 复制为 `.env` 后，必须把 `MILVUS_URI` 替换成实际服务地址，并按服务配置 `MILVUS_TOKEN`。未配置远端地址时，应用会明确报错，不会退回内存索引或本地数据库。本地默认 embedding 是用于无模型环境的确定性 mock；替换成真实 embedding 模型时，collection 中已有向量应重新生成。真实 token 只放在未提交的 `.env` 中。

## API 一览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/menu` | 菜单，可按 `category`、`flavor`、`keyword` 筛选 |
| `GET` | `/api/search/menu?query=清淡的鱼` | Milvus 菜单语义检索，返回相似度和当前 SQLite 菜单事实 |
| `GET` | `/api/recommendations/flavor?flavor=清淡` | 口味推荐 |
| `GET` | `/api/recommendations/featured` | 特色菜推荐 |
| `POST` | `/api/reservations` | 显式确认后创建预订 |
| `GET` | `/api/reservations?phone=...` | 按手机号查询 |
| `GET` | `/api/reservations/{reservation_id}` | 按预订编号查询 |
| `POST` | `/api/delivery` | 配送范围查询 |
| `POST` | `/api/chat` | 规则型 mock Agent 对话 |

预订列表查询至少需要提供 `phone` 或 `thread_id`，避免无条件返回其他顾客的记录。

配送请求示例：

```json
{
  "address": "上海市浦东新区陆家嘴环路1000号",
  "travel_mode": "2"
}
```

`travel_mode`：`1` 骑行、`2` 驾车（默认）、`3` 直线距离。路线模式按地图返回的路线距离判断；直线模式按 MCP 距离工具的直线距离判断。直线距离不代表行程时间，所以该模式下 `duration_min` 为 `null`。

## 数据与配置

- 本地数据库默认位置：`data/restaurant.sqlite3`，由 `.gitignore` 排除，不应提交。
- 可通过 `DATABASE_PATH` 更换 SQLite 数据库文件位置。
- 菜单演示数据位于 `restaurant_app/database.py` 的 `MENU_SEED`，供应状态保存在数据库中。
- Milvus collection 保存于远端服务；连接配置使用 `.env` 中的 `MILVUS_URI`、`MILVUS_TOKEN` 和 `MILVUS_COLLECTION`。embedding 逻辑位于 `restaurant_app/vector_search.py`。
- `sql/schema.sql` 是旧 LangChain/MySQL Agent 连接方式的 MySQL 表结构；FastAPI 网站使用 SQLite，不需要安装或配置 MySQL。
- `.env` 保存本机配置，不应提交到公开仓库。`.env.example` 不包含密钥。

