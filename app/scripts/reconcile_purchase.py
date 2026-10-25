import argparse
import asyncio
import json

from sqlalchemy.exc import SQLAlchemyError

from app.admin.reconciliation import PurchaseReconciliationService
from app.db.session import async_session_factory


async def reconcile(arguments: argparse.Namespace) -> dict:
    async with async_session_factory() as session:
        changes = await PurchaseReconciliationService(session).reconcile(
            actor_id=arguments.actor,
            payment_id=arguments.payment,
            channel_id=arguments.channel,
            duration_days=arguments.days,
            reference=arguments.reference,
            apply=arguments.apply,
            confirmed_unapplied=arguments.confirm_unapplied,
        )
        if arguments.apply:
            await session.commit()
        else:
            await session.rollback()
        return changes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Сверка условий старой покупки по подтверждённым документам."
    )
    parser.add_argument("--actor", type=int, required=True)
    parser.add_argument("--payment", type=int, required=True)
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-unapplied", action="store_true")
    arguments = parser.parse_args()
    try:
        changes = asyncio.run(reconcile(arguments))
    except (ValueError, PermissionError) as exc:
        parser.exit(2, f"{exc}\n")
    except SQLAlchemyError:
        parser.exit(
            1,
            "Не удалось подтвердить сохранение. Проверьте подключение к БД и журнал сверки перед повтором.\n",
        )
    print(json.dumps(changes, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
