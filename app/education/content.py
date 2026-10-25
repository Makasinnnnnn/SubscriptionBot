from pathlib import Path

ROOT = Path(__file__).parent
TITLES = {
    "intro": "Как пользоваться Upscale",
    "open": "Как открыть сделку по сигналу",
    "risk": "Как рассчитать риск",
    "protect": "Как поставить стоп и тейк",
    "change": "Как изменить стоп или тейк",
    "orders": "Как создать, изменить или удалить ордер",
    "close": "Как закрыть сделку",
    "rules": "Правила счетов и челленджей",
    "errors": "Частые ошибки",
    "signals": "Как читать сигналы и обновления",
}


def lessons() -> dict[str, list[str]]:
    result = {}
    for section in (ROOT / "guide.md").read_text(encoding="utf-8").split("\n## ")[1:]:
        heading, body = section.split("\n", 1)
        key = heading.split(" — ")[0]
        result[key] = [part.strip() for part in body.split("\n---\n") if part.strip()]
    return result


LESSONS = lessons()

MEDIA = {
    ("intro", 0): ["01-demo-account"],
    ("open", 1): ["02-search-btc", "03-directions"],
    ("open", 2): ["16-market-amount", "13-limit-form"],
    ("open", 3): ["04-long-form", "05-open-long"],
    ("open", 4): ["11-short-form", "12-open-short"],
    ("protect", 0): ["04-long-form"],
    ("protect", 1): ["17-add-protection", "06-protective-orders"],
    ("change", 1): ["07-edit-stop", "08-edit-take"],
    ("orders", 0): ["13-limit-form", "14-pending-limit"],
    ("orders", 1): ["14-pending-limit", "15-cancelled-limit"],
    ("orders", 2): ["18-remove-protection"],
    ("close", 0): ["09-close-half"],
    ("close", 1): ["10-close-rest", "19-history"],
}
