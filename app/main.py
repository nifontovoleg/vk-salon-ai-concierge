"""Application entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from vkbottle.bot import Bot
from vkbottle.polling import BotPolling

from app.clients.openrouter import OpenRouterClient
from app.config import get_settings
from app.db.session import close_db, get_session, init_db
from app.handlers import register_handlers
from app.logging.setup import setup_logging
from app.services.admin_service import AdminService
from app.services.booking_service import sync_master_schedules
from app.utils.instance_lock import InstanceLock

logger = logging.getLogger(__name__)


async def run_bot() -> None:
    """Initialize resources and start VK Long Poll."""
    settings = get_settings()
    setup_logging(settings.log_level)

    instance_lock = InstanceLock(settings.data_dir / "bot.lock")
    instance_lock.acquire()

    logger.info("Starting AI-консьерж for salon: %s", settings.salon_name)

    await init_db()
    await sync_admins(settings.seed_admin_ids)
    async with get_session() as session:
        await sync_master_schedules(session, settings)
    if settings.require_consent and not settings.privacy_policy_url:
        logger.warning("PRIVACY_POLICY_URL не задан: в запросе согласия не будет ссылки на политику")

    openrouter = OpenRouterClient(settings)
    polling = BotPolling(group_id=settings.vk_group_id, skip_old_events=True)
    bot = Bot(token=settings.vk_group_token, polling=polling)
    register_handlers(bot, settings, openrouter)

    polling_task = asyncio.create_task(bot.run_polling(), name="vk-polling")
    shutdown_event = asyncio.Event()

    def _request_shutdown() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_shutdown())

    shutdown_waiter = asyncio.create_task(shutdown_event.wait(), name="shutdown-waiter")

    try:
        done, pending = await asyncio.wait(
            {polling_task, shutdown_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        if polling_task in done and not polling_task.cancelled():
            exc = polling_task.exception()
            if exc is not None:
                raise exc
    finally:
        logger.info("Graceful shutdown initiated...")
        if not polling_task.done():
            polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Polling task stopped with error")
        await openrouter.close()
        await close_db()
        instance_lock.release()
        logger.info("Shutdown complete")


async def sync_admins(vk_ids: list[int]) -> None:
    """Activate administrators from SEED_ADMIN_VK_IDS on every start."""
    async with get_session() as session:
        admin_service = AdminService(session)
        await admin_service.sync_admins(vk_ids)
        active = await admin_service.list_admins()
    if not active:
        logger.warning(
            "Нет активных администраторов: укажите SEED_ADMIN_VK_IDS, "
            "иначе запись и передача диалога останутся без уведомлений"
        )
    else:
        logger.info("Active administrators: %s", len(active))


def main() -> None:
    """CLI entry point."""
    try:
        get_settings()
    except Exception as exc:
        print(
            "\nОшибка конфигурации. Скопируйте .env.example в .env и заполните переменные.\n"
            f"   Детали: {exc}\n",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except RuntimeError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        logger.error("Bot stopped: %s", exc)
        print(
            "\nБот завершился с ошибкой. Проверьте VK_GROUP_TOKEN и настройки Long Poll в сообществе.\n"
            f"   Детали: {exc}\n",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
