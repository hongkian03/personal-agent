"""Local conversational interface with persistent conversation IDs."""

import argparse
import asyncio
import sys

from personal_agent.agent.service import ChatValidationError, RuntimeConfigurationError
from personal_agent.bootstrap import open_runtime


async def run_cli(args: argparse.Namespace) -> int:
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
                    if message.strip() == "/new":
                        conversation_id = None
                        print("The next message will start a new conversation.")
                        continue
                    if not message.strip():
                        continue
                result = await runtime.chat(message=message, conversation_id=conversation_id)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Talk to your personal agent")
    parser.add_argument("--conversation", help="Resume a saved conversation ID")
    parser.add_argument("--message", help="Send one message and exit")
    args = parser.parse_args()
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
