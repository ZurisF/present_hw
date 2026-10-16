from dotenv import load_dotenv
from pathlib import Path
from dataclasses import dataclass
from typing import Any
from langchain.tools import ToolRuntime, tool
import logging
import pymysql
from pymysql.cursors import DictCursor
import os
import time
from uuid import uuid4

from agent.mysql_reservations import MysqlReservationService, ReservationRequest
from agent.recovery import OperationState

load_dotenv()

ROOT_PATH = Path(__file__).parent.parent
agent = None
reservation_service = None
logger = logging.getLogger(__name__)
READ_RETRY_CODES = {1205, 1213, 2002, 2003, 2006, 2013}


@dataclass(frozen=True)
class AgentContext:
    """Application-verified context; the model cannot supply these fields."""

    confirmed_reservation: ReservationRequest | None = None
    recovery_results: tuple[dict[str, Any], ...] = ()


@tool
def search_main_dishes():
    """查询数据库中的特色菜和供应状态；空列表表示没有匹配记录。"""
    trace_id = str(uuid4())
    for attempt in range(1, 4):
        try:
            with pymysql.connect(
                host=os.getenv("HOST"),
                user=os.getenv("USER"),
                password=os.getenv("PASSWORD"),
                database=os.getenv("DATABASE"),
                port=int(os.getenv("PORT", "3306")),
                charset=os.getenv("CHARSET", "utf8mb4"),
                connect_timeout=5,
                read_timeout=10,
                write_timeout=10,
            ) as conn:
                with conn.cursor(DictCursor) as cursor:
                    cursor.execute("""
                select 
                    dish_name as 菜品名称, 
                    price as 价格, 
                    description as 描述, 
                    category as 菜品类别, 
                    spice_level as 麻辣程度, 
                    flavor as 口味, 
                    main_ingredients as 主料, 
                    cooking_method as 烹饪方法, 
                    is_vegetarian as 是否素食, 
                    allergens as 过敏源,
                    is_available as 当前可供应
                    from menu_items
                where 
                    is_featured = 1
                    """)
                    main_dishes = cursor.fetchall()
            logger.info(
                "menu query completed",
                extra={"trace_id": trace_id, "attempt": attempt},
            )
            return {
                "status": "ok" if main_dishes else "not_found",
                "items": main_dishes,
            }
        except pymysql.err.OperationalError as exc:
            error_number = exc.args[0] if exc.args else None
            if error_number in READ_RETRY_CODES and attempt < 3:
                logger.warning(
                    "transient menu query failure; retrying",
                    extra={
                        "trace_id": trace_id,
                        "attempt": attempt,
                        "error_code": "MENU_QUERY_TRANSIENT",
                    },
                )
                time.sleep(0.25 * (2 ** (attempt - 1)))
                continue
            logger.error(
                "menu database query failed",
                extra={
                    "trace_id": trace_id,
                    "attempt": attempt,
                    "error_code": "MENU_QUERY_FAILED",
                },
            )
            return {"status": "tool_error", "error_code": "MENU_QUERY_FAILED"}
        except (pymysql.MySQLError, TypeError, ValueError):
            logger.error(
                "menu database query failed",
                extra={
                    "trace_id": trace_id,
                    "attempt": attempt,
                    "error_code": "MENU_QUERY_FAILED",
                },
            )
            return {"status": "tool_error", "error_code": "MENU_QUERY_FAILED"}


@tool
def check_menu_item(dish_name: str):
    """用 MySQL 核实一道指定菜品是否存在及当前是否可供应。"""
    if not dish_name.strip():
        return {"status": "invalid_input", "error_code": "DISH_NAME_REQUIRED"}
    trace_id = str(uuid4())
    for attempt in range(1, 4):
        try:
            with pymysql.connect(
                host=os.getenv("HOST"),
                user=os.getenv("USER"),
                password=os.getenv("PASSWORD"),
                database=os.getenv("DATABASE"),
                port=int(os.getenv("PORT", "3306")),
                charset=os.getenv("CHARSET", "utf8mb4"),
                connect_timeout=5,
                read_timeout=10,
                write_timeout=10,
            ) as conn:
                with conn.cursor(DictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT dish_name, price, is_available
                        FROM menu_items
                        WHERE dish_name = %s
                        LIMIT 1
                        """,
                        (dish_name.strip(),),
                    )
                    item = cursor.fetchone()
            if item is None:
                logger.info(
                    "menu item lookup returned no match",
                    extra={"trace_id": trace_id, "attempt": attempt},
                )
                return {"status": "not_found", "items": []}
            return {
                "status": "available" if item["is_available"] else "unavailable",
                "item": item,
            }
        except pymysql.err.OperationalError as exc:
            error_number = exc.args[0] if exc.args else None
            if error_number in READ_RETRY_CODES and attempt < 3:
                logger.warning(
                    "transient availability query failure; retrying",
                    extra={
                        "trace_id": trace_id,
                        "attempt": attempt,
                        "error_code": "AVAILABILITY_QUERY_TRANSIENT",
                    },
                )
                time.sleep(0.25 * (2 ** (attempt - 1)))
                continue
            logger.error(
                "availability database query failed",
                extra={
                    "trace_id": trace_id,
                    "attempt": attempt,
                    "error_code": "AVAILABILITY_QUERY_FAILED",
                },
            )
            return {"status": "tool_error", "error_code": "AVAILABILITY_QUERY_FAILED"}
        except (pymysql.MySQLError, TypeError, ValueError):
            logger.error(
                "availability database query failed",
                extra={
                    "trace_id": trace_id,
                    "attempt": attempt,
                    "error_code": "AVAILABILITY_QUERY_FAILED",
                },
            )
            return {"status": "tool_error", "error_code": "AVAILABILITY_QUERY_FAILED"}


def get_reservation_service():
    global reservation_service
    if reservation_service is None:
        reservation_service = MysqlReservationService()
    return reservation_service


@tool
def create_reservation(runtime: ToolRuntime[AgentContext]) -> dict:
    """提交已经由应用层二次确认的预约；未确认时不会写入数据库。"""
    context = runtime.context
    request = context.confirmed_reservation if context is not None else None
    if request is None:
        return {
            "status": "confirmation_required",
            "message": "Collect the reservation details and wait for explicit confirmation.",
        }

    outcome = get_reservation_service().create_or_resume(request)
    result = outcome.result if isinstance(outcome.result, dict) else {}
    return {
        "status": outcome.state.value,
        "reservation_id": result.get("reservation_id", result.get("id")),
        "error_code": outcome.error_code,
        "attempts": outcome.attempts,
        "message": outcome.message,
    }


@tool
def get_recovery_status(runtime: ToolRuntime[AgentContext]) -> list[dict[str, Any]]:
    """读取应用恢复中间件查库后的预约操作状态。"""
    context = runtime.context
    if context is None:
        return []
    return list(context.recovery_results)


def recover_pending_reservations(thread_id: str):
    """Application recovery hook: reconcile DB writes before resuming the agent."""
    return get_reservation_service().recover_thread(thread_id)


def invoke_agent(
    messages: list[dict[str, str]],
    thread_id: str,
    confirmed_reservation: ReservationRequest | None = None,
    recover_pending: bool = False,
):
    """Invoke with app-owned confirmation context and a stable conversation ID.

    The host application must only pass ``confirmed_reservation`` after the
    second confirmation and must preserve its request_id across retries.
    """
    recovery_results: tuple[dict[str, Any], ...] = ()
    if recover_pending:
        try:
            outcomes = recover_pending_reservations(thread_id)
            recovery_results = tuple(
                {
                    "operation_id": outcome.operation_id,
                    "state": outcome.state.value,
                    "reservation_id": (
                        outcome.result.get("reservation_id", outcome.result.get("id"))
                        if isinstance(outcome.result, dict)
                        else None
                    ),
                    "error_code": outcome.error_code,
                    "message": outcome.message,
                }
                for outcome in outcomes
            )
        except Exception:
            recovery_results = (
                {
                    "state": OperationState.NEEDS_REVIEW.value,
                    "error_code": "RECOVERY_CHECK_FAILED",
                    "message": "The operation status could not be verified.",
                },
            )

    return get_agent().invoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id}},
        context=AgentContext(
            confirmed_reservation=confirmed_reservation,
            recovery_results=recovery_results,
        ),
    )


def get_agent():
    global agent
    if agent is None:
        from langchain.agents import create_agent
        from langchain.chat_models import init_chat_model
        from langgraph.checkpoint.memory import InMemorySaver
        
        checkpointer = InMemorySaver()
        llm = init_chat_model("gpt-4o")
        with open(str(ROOT_PATH / "agent/prompt/system_prompt.txt"), "r", encoding="utf-8") as f:
            system_prompt = f.read()
        
        all_tools = [
            search_main_dishes,
            check_menu_item,
            create_reservation,
            get_recovery_status,
        ]
        agent = create_agent(
            model=llm,
            tools=all_tools,
            system_prompt=system_prompt,
            checkpointer=checkpointer,
            context_schema=AgentContext,
        )
        
    return agent
        
        
if __name__ == "__main__":
    res = invoke_agent(
        messages=[
            {"role": "user", "content": "你们餐厅有什么特色菜品吗？"}
        ],
        thread_id="001",
    )
    print(res["messages"][-1].content)
    
    
    # res = search_main_dishes.invoke({})
    # print(res)
    
