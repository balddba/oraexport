"""ResponseModel Pydantic model."""

from .strict_model import StrictModel


class ResponseModel(StrictModel):
    """Base model for validated API response shapes supporting dict-like subscripting."""

    def __getitem__(self, item: str) -> object:
        """Allow subscript access for backwards compatibility.

        Args:
            item (str): Attribute name to retrieve.

        Returns:
            object: Attribute value for the requested attribute name.
        """
        return getattr(self, item)

    def get(self, item: str, default: object = None) -> object:
        """Allow dict-like get method access with default fallback.

        Args:
            item (str): Attribute name to retrieve.
            default (object): Fallback value if attribute is absent or None.

        Returns:
            object: Attribute value or fallback default.
        """
        return getattr(self, item, default)
