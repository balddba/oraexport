"""DirectoryInfo Pydantic model."""

from .response_model import ResponseModel


class DirectoryInfo(ResponseModel):
    """Oracle filesystem directory name and path.

    Attributes:
        name (str): Directory object name in the Oracle data dictionary.
        path (str): Filesystem path referenced by the directory object.
    """

    name: str
    path: str
