import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)

class SpotterConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = 'spotter'

    def ready(self):
        from .services.stations import get_store

        store = get_store()
        logger.info(f"Loaded {len(store)} fuel stations, skipped with no coordinates: {store.skipped_no_coordinates}")