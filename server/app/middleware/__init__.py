"""
Enterprise Middleware - Performance tracking, compression, security headers, and request handling
"""

import asyncio
import time
import uuid
import gzip
from io import BytesIO
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from fastapi import FastAPI

from app.services.performance_service import performance_monitor
from app.services.logger_service import setup_logger
from app.middleware.security import SecurityHeadersMiddleware

logger = setup_logger(__name__)


class PerformanceMiddleware(BaseHTTPMiddleware):
    """
    Middleware to track API request performance
    Records timing, status codes, and request metadata
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generate request ID
        request_id = str(uuid.uuid4())[:8]
        
        # Skip monitoring endpoints to avoid recursion
        if "/monitoring/" in request.url.path:
            return await call_next(request)
        
        start_time = time.perf_counter()
        
        # Track request
        operation = f"api_{request.method.lower()}_{self._normalize_path(request.url.path)}"
        
        try:
            response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Record successful request
            performance_monitor.record_operation(
                operation=operation,
                duration_ms=duration_ms,
                success=response.status_code < 400,
                metadata={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code
                }
            )
            
            # Add performance headers
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
            
            # Log slow requests
            if duration_ms > 5000:
                logger.warning(
                    f"Slow request: {request.method} {request.url.path} "
                    f"took {duration_ms:.2f}ms (ID: {request_id})"
                )
            
            return response
            
        except asyncio.CancelledError:
            logger.debug(f"Request cancelled: {request.method} {request.url.path} (ID: {request_id})")
            raise
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # Record failed request
            performance_monitor.record_operation(
                operation=operation,
                duration_ms=duration_ms,
                success=False,
                error_message=str(e),
                metadata={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path
                }
            )
            
            logger.error(f"Request failed: {request.method} {request.url.path} - {e}")
            raise
    
    def _normalize_path(self, path: str) -> str:
        """Normalize path for operation naming"""
        # Remove leading/trailing slashes
        path = path.strip("/")
        
        # Replace path separators with underscores
        path = path.replace("/", "_")
        
        # Remove query parameters
        if "?" in path:
            path = path.split("?")[0]
        
        # Truncate long paths
        if len(path) > 50:
            path = path[:50]
        
        return path or "root"


class CompressionMiddleware(BaseHTTPMiddleware):
    """
    GZip compression middleware for responses
    Compresses responses larger than threshold
    """
    
    def __init__(self, app, minimum_size: int = 500):
        super().__init__(app)
        self.minimum_size = minimum_size
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Check if client accepts gzip
        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding.lower():
            try:
                return await call_next(request)
            except asyncio.CancelledError:
                raise
        
        try:
            response = await call_next(request)
        except asyncio.CancelledError:
            raise
        
        # Never consume SSE responses. Starlette's BaseHTTPMiddleware may wrap
        # StreamingResponse in a private streaming response class, so an
        # isinstance check alone is not reliable. Iterating body_iterator here
        # buffers the complete agent run and prevents live progress delivery.
        content_type = (response.headers.get("content-type") or "").lower()
        if (
            response.headers.get("content-encoding") or
            isinstance(response, StreamingResponse) or
            content_type.startswith("text/event-stream")
        ):
            return response
        
        # Get response body
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        
        # Skip small responses
        if len(body) < self.minimum_size:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type
            )
        
        # Compress the body
        compressed_body = gzip.compress(body, compresslevel=6)
        
        # Only use compressed if it's smaller
        if len(compressed_body) < len(body):
            headers = dict(response.headers)
            headers["content-encoding"] = "gzip"
            headers["content-length"] = str(len(compressed_body))
            
            return Response(
                content=compressed_body,
                status_code=response.status_code,
                headers=headers,
                media_type=response.media_type
            )
        
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type
        )


class CORSMiddleware:
    """
    Enhanced CORS middleware with preflight caching
    """
    
    def __init__(
        self,
        app,
        allow_origins: list = None,
        allow_methods: list = None,
        allow_headers: list = None,
        max_age: int = 86400  # 24 hours preflight cache
    ):
        self.app = app
        self.allow_origins = allow_origins or ["*"]
        self.allow_methods = allow_methods or ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
        self.allow_headers = allow_headers or ["*"]
        self.max_age = max_age
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        request = Request(scope, receive)
        origin = request.headers.get("origin", "")
        
        # Handle preflight requests
        if request.method == "OPTIONS":
            response = Response(
                status_code=200,
                headers={
                    "access-control-allow-origin": origin if origin in self.allow_origins or "*" in self.allow_origins else "",
                    "access-control-allow-methods": ", ".join(self.allow_methods),
                    "access-control-allow-headers": ", ".join(self.allow_headers),
                    "access-control-max-age": str(self.max_age)
                }
            )
            await response(scope, receive, send)
            return
        
        # Process request and add CORS headers to response
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                
                if origin in self.allow_origins or "*" in self.allow_origins:
                    headers.append((b"access-control-allow-origin", origin.encode() if origin else b"*"))
                    headers.append((b"access-control-allow-credentials", b"true"))
                
                message["headers"] = headers
            
            await send(message)
        
        await self.app(scope, receive, send_wrapper)


def setup_middleware(app: FastAPI):
    """
    Configure all middleware for the application
    Order matters: first added = last executed (outer layer first)
    """
    # Security headers (outer layer - applied to all responses)
    app.add_middleware(SecurityHeadersMiddleware)
    
    # Performance tracking (executed first for accurate timing)
    app.add_middleware(PerformanceMiddleware)
    
    # Response compression
    app.add_middleware(CompressionMiddleware, minimum_size=500)
    
    logger.debug("Enterprise middleware configured with security headers")
