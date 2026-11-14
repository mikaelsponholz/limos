from fastapi import APIRouter

from gateway import customers
from gateway.api.gateway_tools import Env
from gateway.schemas import Customer, CustomerSummary

router = APIRouter()


@router.get("/customers", response_model=list[CustomerSummary])
def list_customers(env: Env) -> list[CustomerSummary]:
    return customers.list_customers(env.store, **env.customer_paths)


@router.get("/customers/{customer_id}", response_model=Customer)
def get_customer(customer_id: str, env: Env) -> Customer:
    return customers.get_customer(customer_id, env.store, **env.customer_paths)
