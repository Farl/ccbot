"""Application entry point — CLI dispatcher and bot bootstrap.

Handles three execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Claude Code hook processing.
  2. `ccbot --transport telegram` (default) — starts the Telegram bot.
  3. `ccbot --transport slack` — starts the Slack bot.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def main() -> None:
    """Main entry point."""
    if len(sys.argv) > 1 and sys.argv[1] == "hook":
        from .hook import hook_main

        hook_main()
        return

    parser = argparse.ArgumentParser(description="CCBot - Claude Code Bot")
    parser.add_argument(
        "--transport",
        choices=["telegram", "slack"],
        default="telegram",
        help="Messaging transport to use (default: telegram)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the required environment variables.\n")
        print("  ALLOWED_USERS=comma_separated_user_ids")
        print()
        print("For Telegram transport:")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print()
        print("For Slack transport:")
        print("  SLACK_BOT_TOKEN=xoxb-your-bot-token")
        print("  SLACK_APP_TOKEN=xapp-your-app-token")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    logger = logging.getLogger(__name__)

    # Warn if running inside a Claude Code session with wrong CLAUDE_COMMAND
    if os.environ.get("CLAUDECODE") and "CLAUDECODE" not in config.claude_command:
        logger.warning(
            "Running inside a Claude Code session (CLAUDECODE env var is set) but "
            "CLAUDE_COMMAND does not unset it. Child Claude processes will fail. "
            "Set: CLAUDE_COMMAND=env -u CLAUDECODE claude"
        )

    # Warn if SessionStart hook is not installed
    from .hook import _find_existing_hook

    claude_config_dir = os.environ.get(
        "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")
    )
    settings_path = Path(claude_config_dir) / "settings.json"
    hook_installed = False
    try:
        with open(settings_path) as f:
            settings = json.load(f)
        hook_installed = _find_existing_hook(settings) is not None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    if not hook_installed:
        logger.warning(
            "SessionStart hook not installed. Session tracking will not work. "
            "Run: ccbot hook --install"
        )

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    # Warn if TMUX_SESSION_NAME changed but session_map still has old entries
    from .utils import ccbot_dir

    session_map_path = ccbot_dir() / "session_map.json"
    try:
        with open(session_map_path) as f:
            session_map = json.load(f)
        other_sessions = {
            parts[0]
            for key in session_map
            for parts in [key.split(":", 1)]
            if len(parts) == 2 and parts[0] != config.tmux_session_name
        }
        if other_sessions:
            logger.warning(
                "session_map.json has entries for tmux session(s) %s but "
                "TMUX_SESSION_NAME is '%s'. Existing topic bindings will be dropped. "
                "You will need to recreate sessions in Telegram.",
                other_sessions,
                config.tmux_session_name,
            )
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    if args.transport == "telegram":
        if not config.telegram_bot_token:
            print("Error: TELEGRAM_BOT_TOKEN required for telegram transport")
            sys.exit(1)
        # AIORateLimiter (max_retries=5) handles retries; keep INFO for visibility
        logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
        logger.info("Starting Telegram bot...")
        from .transports.telegram.bot import create_bot

        application = create_bot()
        application.run_polling(
            allowed_updates=["message", "callback_query"],
            bootstrap_retries=-1,
        )
    elif args.transport == "slack":
        if not config.slack_bot_token or not config.slack_app_token:
            print(
                "Error: SLACK_BOT_TOKEN and SLACK_APP_TOKEN required for slack transport"
            )
            sys.exit(1)
        logger.info("Starting Slack bot...")
        from .transports.slack.bot import run_slack_bot

        run_slack_bot()


if __name__ == "__main__":
    main()
