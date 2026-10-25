"""Application errors with public status and detail."""


class ApplicationError(Exception):
    """Carry a stable error category without depending on an HTTP framework.

    Attributes:
        status_code (int): HTTP-compatible status category (e.g., 401, 404, 409).
        detail (str): Sanitized error message safe for public exposure.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        """Store the response category and public, secret-free explanation.

        Args:
            status_code (int): HTTP-compatible status code representing error type.
            detail (str): Sanitized explanation free of database credentials or internal secrets.

        Returns:
            None: Initializes the error instance.
        """
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
