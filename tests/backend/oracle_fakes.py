"""Driver connection doubles with recording capability adapters."""

from unittest.mock import MagicMock


class RemoteConnection:
    """Provide a driver-shaped session backed by recording operation doubles."""

    def __init__(self, remote):
        """Store the operation recorder and allocate a cursor double.

        Args:
            remote (object): Recording Oracle operation double.
        """
        self.remote = remote
        self._cursor = MagicMock()

    def cursor(self):
        """Return the session's shared cursor double."""
        return self._cursor


def remote_connection(remote):
    """Wrap recording operations in a driver-shaped connection double.

    Args:
        remote (object): Recording Oracle operation double.

    Returns:
        RemoteConnection: Connection double for an injected connection factory.
    """
    return RemoteConnection(remote)
