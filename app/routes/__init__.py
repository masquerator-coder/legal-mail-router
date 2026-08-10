from app.routes.dashboard import router as dashboard_router
from app.routes.email_config import router as email_router
from app.routes.llm_config import router as llm_router
from app.routes.routing import router as routing_router
from app.routes.logs import router as logs_router
from app.routes.ocr_config import router as ocr_router
from app.routes.settings import router as settings_router
from app.routes.backup import router as backup_router
from app.routes.auto_update import router as auto_update_router

__all__ = ["dashboard_router", "email_router", "llm_router", "routing_router", "logs_router", "ocr_router", "settings_router", "backup_router", "auto_update_router"]
