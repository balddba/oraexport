"""ProvisionPreviewResponse Pydantic model."""

from .response_model import ResponseModel


class ProvisionPreviewResponse(ResponseModel):
    """Generated reviewable provisioning SQL script.

    Attributes:
        sql (list[str]): Sequence of DDL and DCL SQL statements for provisioning.
        note (str): Guidance note describing manual execution or verification steps.
    """

    sql: list[str]
    note: str
