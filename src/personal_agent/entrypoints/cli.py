"""Local conversational interface with persistent conversation IDs."""

import argparse
import asyncio
import base64
import sys
from pathlib import Path

import httpx

from personal_agent.agent.progress import ChatEvent
from personal_agent.agent.service import ChatValidationError, RuntimeConfigurationError
from personal_agent.bootstrap import open_runtime, skill_tool_ids
from personal_agent.config import Settings
from personal_agent.skills.importing import prepare_import


async def export_graph(args: argparse.Namespace) -> int:
    if args.railway:
        # Run the authenticated download inside the private network, without copying
        # the hosted API token to this computer. Only the resulting HTML is returned.
        code = (
            "import os,urllib.request,base64; "
            "u='http://127.0.0.1:'+os.getenv('PORT','8000')+'/memory/graph?full="
            + str(args.full).lower() + "'; "
            "r=urllib.request.Request(u,headers={'Authorization':'Bearer '"
            "+os.environ['PERSONAL_AGENT_API_TOKEN']}); "
            "data=urllib.request.urlopen(r,timeout=180).read(10000001); "
            "assert len(data)<=10000000,'Graph exceeds 10 MB'; "
            "print('GRAPH_EXPORT:'+base64.b64encode(data).decode())"
        )
        process = await asyncio.create_subprocess_exec(
            "railway", "ssh", "--service", "agent-api", "--environment", "production",
            "--", "/app/.venv/bin/python", "-c", code,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=210)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise ValueError("Railway graph download failed; check the linked project and login")
        encoded = next((line.removeprefix(b"GRAPH_EXPORT:") for line in stdout.splitlines()
                        if line.startswith(b"GRAPH_EXPORT:")), None)
        if encoded is None:
            raise ValueError("Railway returned no graph snapshot")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > 10_000_000:
            raise ValueError("Graph export exceeds 10 MB")
        with args.output.open("xb") as destination:
            destination.write(data)
        print(f"Saved graph snapshot to {args.output}")
        return 0
    settings = Settings()
    url = httpx.URL(args.api_url)
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password:
        raise ValueError("Use an HTTP(S) agent API URL without embedded credentials")
    if settings.api_token is None:
        raise ValueError("Set PERSONAL_AGENT_API_TOKEN for the agent API")
    # This path does not open local databases or start a second ingestion worker.
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        async with client.stream(
            "GET", args.api_url.rstrip("/") + "/memory/graph",
            params={"full": str(args.full).lower()},
            headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
        ) as response:
            if response.status_code != 200:
                raise ValueError(f"Graph export failed (HTTP {response.status_code})")
            if "text/html" not in response.headers.get("content-type", ""):
                raise ValueError("Agent returned an invalid graph export")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 10_000_000:
                    raise ValueError("Graph export exceeds 10 MB")
    # Refuse to overwrite an existing file; failed downloads leave no partial export.
    with args.output.open("xb") as destination:
        destination.write(data)
    print(f"Saved graph snapshot to {args.output}")
    return 0


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
    memory = commands.add_parser("memory", help="Inspect long-term memory")
    memory_actions = memory.add_subparsers(dest="memory_action", required=True)
    graph = memory_actions.add_parser("export-graph", help="Download an interactive HTML snapshot")
    connection = graph.add_mutually_exclusive_group(required=True)
    connection.add_argument("--api-url", help="URL of the running agent API")
    connection.add_argument("--railway", action="store_true", help="Use the linked Railway project")
    graph.add_argument("--output", type=Path, default=Path("memory-graph.html"))
    graph.add_argument("--full", action="store_true", help="Export the entire graph (may be large)")
    skills = commands.add_parser("skills", help="Prepare skills for local startup and deployment")
    actions = skills.add_subparsers(dest="skill_action", required=True)
    importer = actions.add_parser("import", help="Package metadata.json and instructions.md")
    importer.add_argument("directory", type=Path)
    importer.add_argument("--activate", action="store_true", help="Activate on the next startup")
    importer.add_argument("--bundle", type=Path, default=Path("config/skills.json"))
    args = parser.parse_args(argv)
    if args.command == "memory":
        try:
            status = asyncio.run(export_graph(args))
        except (OSError, ValueError) as exc:
            print(f"Graph export failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        except Exception as exc:
            print(f"Graph export failed ({type(exc).__name__}).", file=sys.stderr)
            raise SystemExit(1) from None
        raise SystemExit(status)
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
