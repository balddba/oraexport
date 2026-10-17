"""StrictModel Pydantic model."""

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Reject unknown request fields.

    Serves as the base model for all API request contracts, ensuring unexpected
    or misspelled attributes fail validation immediately via extra='forbid'.
    """

    model_config = ConfigDict(extra="forbid")
