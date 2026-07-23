from datetime import datetime


def access_delivery_text(
    *,
    expires_at: datetime,
    primary_link: str,
    materials_link: str | None,
) -> str:
    lines = [
        f"Доступ активирован до {expires_at:%d.%m.%Y %H:%M}",
        "",
        "Основной канал:",
        primary_link,
    ]
    if materials_link:
        lines.extend(["", "Методички:", materials_link])
    lines.extend(
        [
            "",
            "После нажатия Telegram отправит заявку. "
            "PropGate подтвердит её автоматически.",
        ]
    )
    return "\n".join(lines)
