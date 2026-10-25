"""Composition boundary for remote connections and application services.

This module acts as the dependency injection composition root for OraExport,
instantiating application services with production Oracle thin driver connection
factories and shared configuration.
"""

from oraexport.api.dependencies import Db
from oraexport.metadata_service import MetadataService
from oraexport.notifications import NotificationService
from oraexport.oracle_utilities import OracleUtilities


def metadata_service(db: Db) -> MetadataService:
    """Bind metadata workflows to the request transaction and remote driver.

    Args:
        db (Connection): Request-scoped Oracle metadata transaction.

    Returns:
        MetadataService: Metadata workflows with production remote connections.
    """
    return MetadataService(db=db, connection_factory=OracleUtilities.connect)


def notification_service() -> NotificationService:
    """Construct the NotificationService with production dependencies.

    Initializes the alert notification workflow coordinator.

    Returns:
        NotificationService: Configured notification service instance.
    """
    # Instantiate stateless notification service for channel configuration and test events
    service_instance = NotificationService()
    return service_instance
