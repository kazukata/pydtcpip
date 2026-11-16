"""Interactive terminal interface started by the tui command."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

from pydtcpip.dlna.client import DLNAClient
from pydtcpip.store import DeviceStore
from pydtcpip.tui.app import PyDTCPIPApp, TransferRunner


def run(store: DeviceStore, *, interface: str | None, search_port: int, directory: Path) -> list[Path]:
    """
    Run the interface until the user quits and any finalizing Move ends.

    Args:
        store (DeviceStore): Credential and SRM storage
        interface (str | None): Local IPv4 address used for discovery
        search_port (int): UDP port for discovery responses
        directory (Path): Default destination directory for Moves

    Returns:
        list[Path]: Outputs of Moves that still require resumption
    """

    return asyncio.run(_run(store, interface=interface, search_port=search_port, directory=directory))


async def _run(store: DeviceStore, *, interface: str | None, search_port: int, directory: Path) -> list[Path]:
    """
    Own the clients and active transfer outside the application so a committing Move outlives it.

    Args:
        store (DeviceStore): Credential and SRM storage
        interface (str | None): Local IPv4 address used for discovery
        search_port (int): UDP port for discovery responses
        directory (Path): Default destination directory for Moves

    Returns:
        list[Path]: Outputs of Moves that still require resumption
    """

    async with AsyncExitStack() as stack:
        dlna = await stack.enter_async_context(DLNAClient())
        runner = TransferRunner(store, stack)
        app = PyDTCPIPApp(dlna=dlna, runner=runner, interface=interface, search_port=search_port, directory=directory)
        try:
            await app.run_async()
        finally:
            # The application may exit after an unexpected error; never drop a Move during finalization
            await runner.settle()
        return app.outputs_requiring_resume()
