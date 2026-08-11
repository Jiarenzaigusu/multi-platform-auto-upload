from webapp.workspaces.paths import AppDataPaths, UserDataPaths

__all__ = [
    "AppDataPaths",
    "UserDataPaths",
    "UserWorkspace",
    "UserWorkspaceRegistry",
]


def __getattr__(name: str):
    """Lazy-import heavy service modules so the agent can import
    UserDataPaths without pulling in the full FastAPI/LM stack."""
    if name == "UserWorkspaceRegistry":
        from webapp.workspaces.registry import (  # noqa: PLC0415
            UserWorkspaceRegistry,
        )

        return UserWorkspaceRegistry
    if name == "UserWorkspace":
        from webapp.workspaces.service import UserWorkspace  # noqa: PLC0415

        return UserWorkspace
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
