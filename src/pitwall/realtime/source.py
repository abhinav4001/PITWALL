from typing import Protocol

from pitwall.data.models import WorldState, WorldStateDelta


class DataSource(Protocol):
    async def start(self) -> None: ...

    async def next_state(self) -> WorldState | WorldStateDelta: ...

    async def close(self) -> None: ...
