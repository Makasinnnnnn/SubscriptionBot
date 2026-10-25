from pathlib import Path

from alembic.config import Config

from alembic import command


def configuration():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return config


def main():
    command.upgrade(configuration(), "head")


if __name__ == "__main__":
    main()
