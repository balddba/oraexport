"""Domain and application custom types and protocol definitions."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Callable, Protocol, TypedDict, runtime_checkable

import oracledb
from fastapi import Request, Response

from oraexport.models.credential import Credential
from oraexport.models.delivery import Delivery


@runtime_checkable
class OracleLobVariable(Protocol):
    """Protocol for Oracle cursor bind variables returning LOB data."""

    def getvalue(self) -> object:
        """Retrieve the underlying bound variable value or LOB locator."""
        ...


class OracleConnectionParameters(TypedDict):
    """Typed metadata connection arguments parsed from an Oracle URL.

    Attributes:
        user (str): Decoded Oracle username.
        password (str): Decoded Oracle password.
        dsn (str): Driver connection descriptor.
        tcp_connect_timeout (int): Connection handshake timeout in seconds.
    """

    user: str
    password: str
    dsn: str
    tcp_connect_timeout: int


type OracleConnectionFactory = Callable[
    [str, Credential],
    AbstractContextManager[oracledb.Connection],
]


@runtime_checkable
class NotificationSender(Protocol):
    """Protocol for channel notification dispatchers."""

    def send(
        self,
        config: Mapping[str, object],
        secrets: dict[str, str],
        delivery: Delivery,
    ) -> None:
        """Send notification payload to the configured channel."""
        ...


class CallNext(Protocol):
    """Next HTTP middleware handler accepting a named request argument."""

    async def __call__(self, request: Request) -> Response:
        """Process the request through the remaining middleware and route."""
        ...
