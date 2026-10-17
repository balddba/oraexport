"""OracleConnectionParameters Pydantic model."""

from pydantic import BaseModel


class OracleConnectionParameters(BaseModel):
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
