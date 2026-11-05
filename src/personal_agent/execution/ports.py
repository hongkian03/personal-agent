"""Optional future execution of generated code outside the application process."""

from dataclasses import dataclass
from typing import Protocol

from personal_agent.core.context import ExecutionContext


@dataclass(frozen=True)
class ScriptRequest:
    artifact_id: str
    revision: int
    timeout_seconds: int
    network_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptResult:
    exit_code: int
    stdout: str
    stderr: str


class SandboxExecutor(Protocol):
    async def run(self, request: ScriptRequest, *, context: ExecutionContext) -> ScriptResult:
        """Run an immutable validated artifact with resource limits and scoped access.

        No host shell, application environment, or database credentials are inherited.
        No executor is provided by this scaffold; generated execution stays unavailable.
        """
        ...
