from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.admin.repository import AdminRepository
from app.modules.admin.service import AdminConsoleService


def get_admin_console_service(db: DbSession) -> AdminConsoleService:
    return AdminConsoleService(AdminRepository(db))


AdminConsoleServiceDep = Annotated[AdminConsoleService, Depends(get_admin_console_service)]
