from fastapi import APIRouter

from gateway.api.gateway_tools import Env
from gateway.audit import get_audit
from gateway.schemas import Audit

router = APIRouter()


@router.get("/audit/{request_id}", response_model=Audit)
def audit(request_id: str, env: Env) -> Audit:
    return get_audit(env.store, request_id, env.request_buyer(request_id))
