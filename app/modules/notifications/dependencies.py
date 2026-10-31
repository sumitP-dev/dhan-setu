from typing import Annotated

from fastapi import Depends, Request

from app.modules.notifications.service import SmsSender


def get_sms_sender(request: Request) -> SmsSender:
    sender: SmsSender = request.app.state.sms_sender
    return sender


SmsSenderDep = Annotated[SmsSender, Depends(get_sms_sender)]
