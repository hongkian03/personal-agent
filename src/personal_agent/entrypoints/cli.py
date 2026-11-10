"""Local conversational interface with persistent conversation IDs."""

import argparse
import asyncio
import sys
from pathlib import Path

from personal_agent.agent.progress import ChatEvent
from personal_agent.agent.service import ChatValidationError, RuntimeConfigurationError
from personal_agent.bootstrap import open_runtime, skill_tool_ids
from personal_agent.skills.importing import prepare_import


async def run_cli(args: argparse.Namespace) -> int:
    def show_progress(event: ChatEvent) -> None:
        if event.kind in {"acknowledgment", "status"}:
            print(f"Agent: {event.text}", flush=True)

    conversation_id = args.conversation
    async with open_runtime() as runtime:
        if args.message is None:
            print("Personal Agent — /new starts a conversation; /exit quits.")
            if conversation_id:
                print(f"Resuming conversation: {conversation_id}")
        while True:
            try:
                message = args.message
                if message is None:
                    message = await asyncio.to_thread(input, "You: ")
                    if message.strip() == "/exit":
                        return 0
                    if not message.strip():
                        continue
                result = await runtime.chat(
                    message=message, conversation_id=conversation_id, on_event=show_progress
                )
            except EOFError:
                return 0
            except RuntimeConfigurationError:
                print(
                    "Configure an OpenAI API key before chatting. See .env.example.",
                    file=sys.stderr,
                )
                return 1
            except ChatValidationError as exc:
                print(str(exc), file=sys.stderr)
                if args.message is not None:
                    return 1
                continue
            except TimeoutError:
                print("The agent request timed out.", file=sys.stderr)
                if args.message is not None:
                    return 1
                continue
            except Exception as exc:
                print(f"Agent request failed ({type(exc).__name__}).", file=sys.stderr)
                if args.message is not None:
                    return 1
                continue
            conversation_id = result.conversation_id
            print(f"Agent: {result.text}")
            print(f"[conversation: {conversation_id}]")
            if args.message is not None:
                return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Talk to your personal agent")
    parser.add_argument("--conversation", help="Resume a saved conversation ID")
    parser.add_argument("--message", help="Send one message and exit")
    commands = parser.add_subparsers(dest="command")
    skills = commands.add_parser("skills", help="Prepare skills for local startup and deployment")
    actions = skills.add_subparsers(dest="skill_action", required=True)
    importer = actions.add_parser("import", help="Package metadata.json and instructions.md")
    importer.add_argument("directory", type=Path)
    importer.add_argument("--activate", action="store_true", help="Activate on the next startup")
    importer.add_argument("--bundle", type=Path, default=Path("config/skills.json"))
    args = parser.parse_args(argv)
    if args.command == "skills":
        try:
            skill = prepare_import(
                args.directory,
                args.bundle,
                activate=args.activate,
                available_tool_ids=skill_tool_ids(),
            )
        except (OSError, ValueError) as exc:
            print(f"Skill import failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        action = "saved and activated" if skill.activate else "saved without changing activation"
        print(f"Packaged {skill.skill_id} in {args.bundle}.")
        print(f"On the next app startup, changed content will be {action} in that app's database.")
        print(
            "Commit the bundle with your source files, then deploy. No database was changed here."
        )
        raise SystemExit(0)
    try:
        status = asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        status = 130
    except Exception as exc:
        print(f"Agent startup failed ({type(exc).__name__}).", file=sys.stderr)
        status = 1
    raise SystemExit(status)


if __name__ == "__main__":
    main()
