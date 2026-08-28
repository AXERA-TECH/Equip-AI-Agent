from __future__ import annotations

import asyncio
import argparse
from pathlib import Path
from uuid import uuid4

from cat_assistant.bootstrap import build_demo_app, load_env_file
from cat_assistant.domain.models import Utterance


async def run_cli(config_path: Path | None = None) -> None:
    app = build_demo_app(config_path=config_path)
    session_id = str(uuid4())
    print("Equip AI Agent demo. 输入 exit 退出。")
    try:
        while True:
            # This demo owns the terminal and has no concurrent channel work.
            text = input("> ").strip()
            if text.casefold() in {"exit", "quit"}:
                return
            if not text:
                continue
            response = await app.handle(
                Utterance(
                    text=text,
                    session_id=session_id,
                    machine_id="cat-306-demo",
                    operator_id="demo-operator",
                )
            )
            print(response.text)
    finally:
        await app.shutdown()


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Run the Equip AI Agent edge assistant CLI")
    parser.add_argument(
        "--config-path",
        type=Path,
        default=Path("runtime/runtime-config.json"),
        help="local JSON configuration file (UI is optional)",
    )
    args = parser.parse_args()
    asyncio.run(run_cli(args.config_path))


if __name__ == "__main__":
    main()
