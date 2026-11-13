from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from gateway.schemas import Error, ErrorEnvelope


class GatewayError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorEnvelope(error=Error(code=code, message=message)).model_dump(mode="json"),
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        try:
            code = HTTPStatus(exc.status_code).name
        except ValueError:
            code = f"HTTP_{exc.status_code}"
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        response = error_response(exc.status_code, code, message)
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(422, "VALIDATION_ERROR", "Request validation failed.")

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "INTERNAL_ERROR", "An internal error occurred.")
