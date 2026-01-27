"""Python HTTP Interceptor for requests, aiohttp, httpx, and urllib.request."""

from __future__ import annotations

import functools
import time
from typing import Any, Callable, Dict, Optional
import os
from datetime import datetime

# Debug file logger - writes to /tmp/interceptor.log for troubleshooting
def _debug_log(msg: str) -> None:
    """Write debug message to /tmp/interceptor.log."""
    try:
        with open("/tmp/interceptor.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass

from .http_logger import HTTPRequestLogger

_debug_log("http_python_interceptor module loading...")

# Get server name from environment
# Don't capture server_name at module level - read from environment when needed
# This ensures we get the correct server name even if module is imported before env is set
# Use a lazy initialization pattern: logger will be created on first access
class _LazyLogger:
    """Lazy logger that reads server_name from environment when first accessed."""
    def __init__(self):
        self._logger = None
    
    def _get_logger(self):
        """Get or create the logger instance."""
        if self._logger is None:
            server_name = os.environ.get("MCP_SERVER_NAME") or os.environ.get("SERVER_NAME")
            _debug_log(f"_LazyLogger: Creating HTTPRequestLogger with server_name={server_name}")
            self._logger = HTTPRequestLogger.get_instance(server_name=server_name)
            _debug_log(f"_LazyLogger: HTTPRequestLogger created, _replay_logger_class={self._logger._replay_logger_class}")
        return self._logger
    
    def __getattr__(self, name):
        """Forward all attribute access to the actual logger."""
        return getattr(self._get_logger(), name)

logger = _LazyLogger()

# Lazy load replay manager (only if in replay mode)



def _get_server_name() -> Optional[str]:
    """Try to detect the MCP server name from environment."""
    return os.environ.get("MCP_SERVER_NAME") or os.environ.get("SERVER_NAME")


def _serialize_headers(headers: Any) -> Dict[str, str]:
    """Convert headers to a plain dict."""
    result = {}
    if hasattr(headers, "items"):
        # dict-like object
        for k, v in headers.items():
            result[str(k)] = str(v)
    elif hasattr(headers, "get_all"):
        # CaseInsensitiveDict or similar
        for k in headers:
            result[str(k)] = str(headers[k])
    elif isinstance(headers, dict):
        result = {str(k): str(v) for k, v in headers.items()}
    return result


def _get_body_from_request(request: Any) -> Optional[Any]:
    """Extract body from various request objects."""
    # Try common attributes
    for attr in ["body", "data", "json", "content", "_body"]:
        if hasattr(request, attr):
            value = getattr(request, attr)
            if value is not None:
                return value
    
    # For requests library, check _body_position
    if hasattr(request, "_body_position"):
        return None  # Body is in the file, not accessible
    
    return None


# ===== requests library interceptor =====

_original_requests_request = None
_original_requests_post = None
_original_requests_get = None
_original_requests_put = None
_original_requests_delete = None
_original_requests_patch = None

# Session methods - only need to intercept request() since get/post/put/delete/patch all call it
_original_requests_session_request = None


def _wrap_requests_method(method_name: str, original_func: Callable) -> Callable:
    """Wrap requests library methods."""
    
    @functools.wraps(original_func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        _debug_log(f"[requests] Intercepted {method_name}() call")
        # Extract method and URL
        # For requests.request(method, url, ...), method is first arg, url is second
        # For requests.get(url, ...), url is first arg
        url = None
        method = None
        
        if method_name == "request":
            # requests.request() can be called as:
            # - requests.request("GET", url, ...) - method is first arg, url is second
            # - requests.request(url, method="GET", ...) - url is first arg, method in kwargs
            # - requests.request(method="GET", url=url, ...) - both in kwargs
            
            # First check kwargs
            url = kwargs.get("url")
            method = kwargs.get("method")
            
            # Then check args
            if args:
                if not url and not method:
                    # No kwargs, check args order
                    if len(args) >= 2:
                        # requests.request(method, url, ...)
                        method = args[0] if isinstance(args[0], str) else method
                        url = args[1] if isinstance(args[1], str) and args[1].startswith(('http://', 'https://')) else url
                    elif len(args) == 1:
                        arg0 = args[0]
                        if isinstance(arg0, str):
                            if arg0.upper() in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
                                # First arg is method, need second arg for URL
                                method = arg0
                                url = None
                            elif arg0.startswith(('http://', 'https://')):
                                # First arg is URL
                                url = arg0
                                method = method or "GET"
                elif url and not method and len(args) >= 1:
                    # URL in kwargs, method might be first arg
                    if isinstance(args[0], str) and args[0].upper() in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
                        method = args[0]
                elif method and not url and len(args) >= 1:
                    # Method in kwargs, URL might be first arg
                    if isinstance(args[0], str) and args[0].startswith(('http://', 'https://')):
                        url = args[0]
        else:
            # requests.get(url, ...), requests.post(url, ...), etc.
            # Method is determined by function name
            method = method_name.upper()
            # URL is always the first positional argument or in kwargs
            url = kwargs.get("url") or (args[0] if args else None)
        
        # Normalize method
        if method:
            method = str(method).upper()
        else:
            method = "GET"
        
        # Validate URL
        if not url or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            # URL extraction failed, but we'll try to get it from response later
            url = url if url else "unknown"
        
        start_time = time.time()
        headers = kwargs.get("headers", {}) or {}
        
        # Extract request body - try multiple sources
        body = None
        if kwargs.get("json") is not None:
            import json as json_module
            try:
                body = json_module.dumps(kwargs["json"], ensure_ascii=False)
            except Exception:
                body = str(kwargs["json"])
        elif kwargs.get("data") is not None:
            data = kwargs["data"]
            if isinstance(data, (str, bytes)):
                body = data.decode('utf-8') if isinstance(data, bytes) else data
            elif isinstance(data, dict):
                # Form data - serialize as form-encoded string
                try:
                    from urllib.parse import urlencode
                    body = urlencode(data)
                except Exception:
                    body = str(data)
            else:
                body = str(data)
        elif kwargs.get("files") is not None:
            body = f"[File upload: {len(kwargs['files'])} file(s)]"
        
        try:
            response = original_func(*args, **kwargs)
            duration_ms = (time.time() - start_time) * 1000
            
            # ALWAYS get URL from response object first - this is the most reliable source
            # The response.url attribute contains the final URL after any redirects
            final_url = None
            
            # Try response.url first (most reliable)
            try:
                if hasattr(response, 'url') and response.url:
                    final_url = str(response.url)
            except Exception:
                pass
            
            # Try response.request.url as fallback
            if not final_url or final_url.upper() in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
                try:
                    if hasattr(response, 'request') and hasattr(response.request, 'url') and response.request.url:
                        final_url = str(response.request.url)
                except Exception:
                    pass
            
            # If still no valid URL, try to extract from original call arguments
            if not final_url or final_url.upper() in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
                # Re-extract URL from kwargs/args
                if kwargs.get("url") and isinstance(kwargs["url"], str) and kwargs["url"].startswith(('http://', 'https://')):
                    final_url = str(kwargs["url"])
                elif args:
                    # For requests.get(url), url is args[0]
                    # For requests.request(method, url), url is args[1]  
                    if method_name != "request" and len(args) > 0:
                        if isinstance(args[0], str) and args[0].startswith(('http://', 'https://')):
                            final_url = str(args[0])
                    elif method_name == "request" and len(args) > 1:
                        if isinstance(args[1], str) and args[1].startswith(('http://', 'https://')):
                            final_url = str(args[1])
                        elif len(args) > 0 and isinstance(args[0], str) and args[0].startswith(('http://', 'https://')):
                            final_url = str(args[0])
            
            # Final fallback
            if not final_url or final_url.upper() in ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']:
                final_url = "unknown"
            
            # Get response body
            response_body = None
            try:
                response_body = response.text
            except Exception:
                try:
                    response_body = response.content.decode('utf-8', errors='replace')
                except Exception:
                    pass
            
            _debug_log(f"[requests] Logging: {method} {final_url} -> {response.status_code}")
            _debug_log(f"[requests]   EXECUTION_ID={os.environ.get('EXECUTION_ID')}, WORKFLOW_ID={os.environ.get('WORKFLOW_ID')}")
            logger.log_request(
                language="python",
                method=method,
                url=str(final_url),
                headers=_serialize_headers(headers),
                body=body,
                response_status=response.status_code,
                response_headers=_serialize_headers(response.headers),
                response_body=response_body,
                duration_ms=duration_ms,
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
            _debug_log(f"[requests] log_request() completed")
            
            return response
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.log_request(
                language="python",
                method=method,
                url=str(url),
                headers=_serialize_headers(headers),
                body=body,
                error=str(e),
                duration_ms=duration_ms,
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
            raise
    
    return wrapper


def _intercept_requests() -> None:
    """Intercept requests library."""
    try:
        import requests
        
        global _original_requests_request, _original_requests_post
        global _original_requests_get, _original_requests_put
        global _original_requests_delete, _original_requests_patch
        global _original_requests_session_request
        
        if _original_requests_request is None:
            # Intercept module-level functions
            _original_requests_request = requests.request
            _original_requests_post = requests.post
            _original_requests_get = requests.get
            _original_requests_put = requests.put
            _original_requests_delete = requests.delete
            _original_requests_patch = requests.patch
            
            requests.request = _wrap_requests_method("request", _original_requests_request)
            requests.post = _wrap_requests_method("post", _original_requests_post)
            requests.get = _wrap_requests_method("get", _original_requests_get)
            requests.put = _wrap_requests_method("put", _original_requests_put)
            requests.delete = _wrap_requests_method("delete", _original_requests_delete)
            requests.patch = _wrap_requests_method("patch", _original_requests_patch)
            
            # Intercept Session.request() - this catches all HTTP methods (get, post, put, delete, patch)
            # since Session.get() calls Session.request("GET", url, ...)
            _original_requests_session_request = requests.Session.request
            requests.Session.request = _wrap_requests_method("request", _original_requests_session_request)
            
            logger.log_request(
                language="python",
                method="INTERCEPT",
                url="requests",
                headers={},
                body="requests library interceptor installed (module-level and Session methods)",
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
    except ImportError:
        pass


# ===== aiohttp library interceptor =====

_original_aiohttp_client_session_post = None
_original_aiohttp_client_session_get = None
_original_aiohttp_client_session_request = None


def _wrap_aiohttp_method(method_name: str, original_func: Callable) -> Callable:
    """Wrap aiohttp ClientSession methods."""
    
    @functools.wraps(original_func)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        url = kwargs.get("url") or (args[0] if args else None)
        method = method_name.upper() if method_name != "_request" else kwargs.get("method", "GET").upper()
        
        if not url:
            return await original_func(self, *args, **kwargs)
        
        start_time = time.time()
        headers = kwargs.get("headers", {}) or {}
        
        # Extract request body
        body = None
        if kwargs.get("json") is not None:
            import json as json_module
            try:
                body = json_module.dumps(kwargs["json"], ensure_ascii=False)
            except Exception:
                body = str(kwargs["json"])
        elif kwargs.get("data") is not None:
            data = kwargs["data"]
            if isinstance(data, (str, bytes)):
                body = data.decode('utf-8') if isinstance(data, bytes) else data
            elif isinstance(data, dict):
                try:
                    from urllib.parse import urlencode
                    body = urlencode(data)
                except Exception:
                    body = str(data)
            else:
                body = str(data)
        
        try:
            response = await original_func(self, *args, **kwargs)
            duration_ms = (time.time() - start_time) * 1000
            
            # Get final URL from response
            final_url = str(getattr(response, 'url', url))
            
            # Read response body
            response_body = None
            try:
                response_body = await response.text()
            except Exception:
                try:
                    response_body = (await response.read()).decode('utf-8', errors='replace')
                except Exception:
                    pass
            
            logger.log_request(
                language="python",
                method=method,
                url=final_url,
                headers=_serialize_headers(headers),
                body=body,
                response_status=response.status,
                response_headers=_serialize_headers(response.headers),
                response_body=response_body,
                duration_ms=duration_ms,
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
            
            return response
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            logger.log_request(
                language="python",
                method=method,
                url=str(url),
                headers=_serialize_headers(headers),
                body=body,
                error=str(e),
                duration_ms=duration_ms,
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
            raise
    
    return wrapper


def _intercept_aiohttp() -> None:
    """Intercept aiohttp library."""
    try:
        import aiohttp
        
        global _original_aiohttp_client_session_post, _original_aiohttp_client_session_get
        global _original_aiohttp_client_session_request
        
        if _original_aiohttp_client_session_post is None:
            _original_aiohttp_client_session_post = aiohttp.ClientSession.post
            _original_aiohttp_client_session_get = aiohttp.ClientSession.get
            _original_aiohttp_client_session_request = aiohttp.ClientSession._request
            
            aiohttp.ClientSession.post = _wrap_aiohttp_method("post", _original_aiohttp_client_session_post)
            aiohttp.ClientSession.get = _wrap_aiohttp_method("get", _original_aiohttp_client_session_get)
            aiohttp.ClientSession._request = _wrap_aiohttp_method("_request", _original_aiohttp_client_session_request)
    except ImportError:
        pass


# ===== httpx library interceptor =====

_original_httpx_client_request = None


def _intercept_httpx() -> None:
    """Intercept httpx library."""
    try:
        import httpx
        
        global _original_httpx_client_request
        
        if _original_httpx_client_request is None:
            _original_httpx_client_request = httpx.Client.request
            
            @functools.wraps(_original_httpx_client_request)
            def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                method = kwargs.get("method", "GET").upper()
                url = kwargs.get("url") or (args[0] if args else None)
                
                if not url:
                    return _original_httpx_client_request(self, *args, **kwargs)
                
                start_time = time.time()
                headers = kwargs.get("headers", {}) or {}
                
                # Extract request body
                body = None
                if kwargs.get("json") is not None:
                    import json as json_module
                    try:
                        body = json_module.dumps(kwargs["json"], ensure_ascii=False)
                    except Exception:
                        body = str(kwargs["json"])
                elif kwargs.get("content") is not None:
                    content = kwargs["content"]
                    if isinstance(content, (str, bytes)):
                        body = content.decode('utf-8') if isinstance(content, bytes) else content
                    else:
                        body = str(content)
                elif kwargs.get("data") is not None:
                    data = kwargs["data"]
                    if isinstance(data, (str, bytes)):
                        body = data.decode('utf-8') if isinstance(data, bytes) else data
                    elif isinstance(data, dict):
                        try:
                            from urllib.parse import urlencode
                            body = urlencode(data)
                        except Exception:
                            body = str(data)
                    else:
                        body = str(data)
                
                try:
                    response = _original_httpx_client_request(self, *args, **kwargs)
                    duration_ms = (time.time() - start_time) * 1000
                    
                    # Get final URL from response
                    final_url = str(getattr(response, 'url', url))
                    
                    # Get response body
                    response_body = None
                    try:
                        response_body = response.text
                    except Exception:
                        try:
                            response_body = response.content.decode('utf-8', errors='replace')
                        except Exception:
                            pass
                    
                    logger.log_request(
                        language="python",
                        method=method,
                        url=final_url,
                        headers=_serialize_headers(headers),
                        body=body,
                        response_status=response.status_code,
                        response_headers=_serialize_headers(response.headers),
                        response_body=response_body,
                        duration_ms=duration_ms,
                        server_name=_get_server_name(),
                        execution_id=os.environ.get("EXECUTION_ID"),
                        workflow_id=os.environ.get("WORKFLOW_ID"),
                    )
                    
                    return response
                except Exception as e:
                    duration_ms = (time.time() - start_time) * 1000
                    logger.log_request(
                        language="python",
                        method=method,
                        url=str(url),
                        headers=_serialize_headers(headers),
                        body=body,
                        error=str(e),
                        duration_ms=duration_ms,
                        server_name=_get_server_name(),
                        execution_id=os.environ.get("EXECUTION_ID"),
                        workflow_id=os.environ.get("WORKFLOW_ID"),
                    )
                    raise
            
            httpx.Client.request = wrapper
            
            # Also intercept AsyncClient
            _original_httpx_async_client_request = httpx.AsyncClient.request
            
            @functools.wraps(_original_httpx_async_client_request)
            async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                method = kwargs.get("method", "GET").upper()
                url = kwargs.get("url") or (args[0] if args else None)
                
                if not url:
                    return await _original_httpx_async_client_request(self, *args, **kwargs)
                
                start_time = time.time()
                headers = kwargs.get("headers", {})
                json_data = kwargs.get("json")
                content = kwargs.get("content")
                data = kwargs.get("data")
                body = json_data or content or data
                
                try:
                    response = await _original_httpx_async_client_request(self, *args, **kwargs)
                    duration_ms = (time.time() - start_time) * 1000
                    
                    response_body = None
                    try:
                        response_body = response.text if hasattr(response, "text") else None
                    except Exception:
                        pass
                    
                    logger.log_request(
                        language="python",
                        method=method,
                        url=str(url),
                        headers=_serialize_headers(headers),
                        body=body,
                        response_status=response.status_code,
                        response_headers=_serialize_headers(response.headers),
                        response_body=response_body,
                        duration_ms=duration_ms,
                        server_name=_get_server_name(),
                        execution_id=os.environ.get("EXECUTION_ID"),
                        workflow_id=os.environ.get("WORKFLOW_ID"),
                    )
                    
                    return response
                except Exception as e:
                    duration_ms = (time.time() - start_time) * 1000
                    logger.log_request(
                        language="python",
                        method=method,
                        url=str(url),
                        headers=_serialize_headers(headers),
                        body=body,
                        error=str(e),
                        duration_ms=duration_ms,
                        server_name=_get_server_name(),
                        execution_id=os.environ.get("EXECUTION_ID"),
                        workflow_id=os.environ.get("WORKFLOW_ID"),
                    )
                    raise
            
            httpx.AsyncClient.request = async_wrapper
    except ImportError:
        pass


# ===== urllib library interceptor =====

_original_urllib_urlopen = None


def _extract_request_info(url: Any, data: Any = None) -> tuple[str, str, Dict[str, str], Optional[str]]:
    """Extract URL, method, headers, and body from urllib request."""
    # Extract URL and method
    if isinstance(url, str):
        final_url = url
        method = "POST" if data is not None else "GET"
    elif hasattr(url, 'full_url'):
        final_url = url.full_url
        method = getattr(url, 'get_method', lambda: 'GET')()
    elif hasattr(url, 'get_full_url'):
        final_url = url.get_full_url()
        method = getattr(url, 'get_method', lambda: 'GET')()
    else:
        final_url = str(url)
        method = "POST" if data is not None else "GET"

    # Extract headers
    headers = {}
    if hasattr(url, 'headers'):
        headers = dict(url.headers)
    elif hasattr(url, 'header_items'):
        headers = dict(url.header_items())

    # Extract body
    body = None
    # Check if data is in the Request object (urllib.request.Request has 'data' attribute)
    if hasattr(url, 'data') and url.data is not None:
        request_data = url.data
        if isinstance(request_data, bytes):
            try:
                body = request_data.decode('utf-8')
            except UnicodeDecodeError:
                body = f"<binary data: {len(request_data)} bytes>"
        elif isinstance(request_data, str):
            body = request_data
        else:
            body = str(request_data)
    elif data is not None:
        # Data passed as separate parameter
        if isinstance(data, bytes):
            try:
                body = data.decode('utf-8')
            except UnicodeDecodeError:
                body = f"<binary data: {len(data)} bytes>"
        elif isinstance(data, str):
            body = data
        else:
            body = str(data)

    return final_url, method, headers, body


class _CachedResponseBody:
    """Wrapper to cache response body for logging while allowing caller to read it."""
    def __init__(self, response: Any, body_bytes: bytes):
        self._response = response
        self._body_bytes = body_bytes
        self._read_called = False
    
    def read(self, size: int = -1) -> bytes:
        """Read from cached body."""
        if self._read_called:
            # Already read - return empty or remaining bytes
            if size == -1:
                return b''
            return b''
        
        if size == -1:
            self._read_called = True
            return self._body_bytes
        else:
            # Return requested size
            result = self._body_bytes[:size]
            self._body_bytes = self._body_bytes[size:]
            if not self._body_bytes:
                self._read_called = True
            return result
    
    def __getattr__(self, name: str) -> Any:
        """Delegate all other attributes to original response."""
        return getattr(self._response, name)


class _MockUrllibResponse:
    """Mock urllib HTTPResponse for replay mode."""
    def __init__(self, status: int, headers: Dict[str, str], body: str, url: str):
        self.status = status
        self.code = status
        self._headers = headers
        self._body_bytes = body.encode('utf-8') if body else b''
        self._url = url
        self._read_called = False
    
    def getcode(self) -> int:
        """Return HTTP status code."""
        return self.status
    
    def info(self) -> Any:
        """Return headers as email.message.Message-like object."""
        # Create a simple dict-like object for headers
        class HeadersDict:
            def __init__(self, headers: Dict[str, str]):
                self._headers = headers
            
            def items(self):
                return self._headers.items()
            
            def get(self, key: str, default: Any = None) -> Any:
                return self._headers.get(key, default)
            
            def __getitem__(self, key: str) -> Any:
                return self._headers[key]
            
            def __contains__(self, key: str) -> bool:
                return key in self._headers
        
        return HeadersDict(self._headers)
    
    @property
    def headers(self) -> Dict[str, str]:
        """Return headers dict."""
        return self._headers
    
    def read(self, size: int = -1) -> bytes:
        """Read response body."""
        if self._read_called:
            return b''
        
        if size == -1:
            self._read_called = True
            return self._body_bytes
        else:
            result = self._body_bytes[:size]
            self._body_bytes = self._body_bytes[size:]
            if not self._body_bytes:
                self._read_called = True
            return result
    
    def geturl(self) -> str:
        """Return the URL of the response."""
        return self._url
    
    def __enter__(self) -> Any:
        """Context manager support."""
        return self
    
    def __exit__(self, *args: Any) -> None:
        """Context manager support."""
        pass


def _extract_response_info(response: Any) -> tuple[Optional[int], Dict[str, str], Optional[str], Any]:
    """Extract status, headers, and body from urllib response.
    
    Returns:
        Tuple of (status, headers, body_string, wrapped_response)
        wrapped_response is the original response if body wasn't read, or a wrapper if it was.
    """
    # Extract status
    response_status = None
    if hasattr(response, 'status'):
        response_status = response.status
    elif hasattr(response, 'getcode'):
        response_status = response.getcode()
    elif hasattr(response, 'code'):
        response_status = response.code

    # Extract headers
    response_headers = {}
    if hasattr(response, 'headers'):
        response_headers = _serialize_headers(response.headers)
    elif hasattr(response, 'info'):
        info = response.info()
        if hasattr(info, 'items'):
            response_headers = _serialize_headers(info)

    # Extract body (non-destructive read)
    # CRITICAL: We must not consume the response body as the caller needs to read it
    # Strategy: Read the body, cache it, then wrap the response to provide cached body
    response_body = None
    wrapped_response = response
    
    if hasattr(response, 'read'):
        try:
            # Read the entire body
            response_body_bytes = response.read()
            
            if isinstance(response_body_bytes, bytes):
                try:
                    response_body = response_body_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    response_body = f"<binary data: {len(response_body_bytes)} bytes>"
            else:
                response_body = str(response_body_bytes)
            
            # Wrap the response to provide cached body when caller reads it
            wrapped_response = _CachedResponseBody(response, response_body_bytes)
            _debug_log(f"[urllib] Cached response body ({len(response_body_bytes)} bytes)")
            
        except Exception as e:
            # If reading fails, don't break the response
            response_body = None
            wrapped_response = response
            _debug_log(f"[urllib] Failed to extract response body: {e}")

    return response_status, response_headers, response_body, wrapped_response


def _wrap_urllib_urlopen(original_func: Callable) -> Callable:
    """Wrap urllib.request.urlopen to intercept HTTP requests.
    
    Handles both old and new Python versions:
    - Python 3.4+: (url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *, cafile=None, capath=None, cadefault=False, context=None)
    - Python 3.13+: (url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *, context=None)
    """

    @functools.wraps(original_func)
    def wrapper(url: Any, data: Any = None, timeout: Any = None, **kwargs: Any) -> Any:

        _debug_log("[urllib] Intercepted urlopen() call")
        start_time = time.time()

        # Extract request information
        request_url, method, headers, body = _extract_request_info(url, data)

        try:
            # Make the actual request
            # Use **kwargs to handle both old and new signatures gracefully
            response = original_func(url, data, timeout, **kwargs)

            # Extract response information (this may wrap the response to cache body)
            response_status, response_headers, response_body, wrapped_response = _extract_response_info(response)
            duration_ms = (time.time() - start_time) * 1000

            # Log the successful request
            _debug_log(f"[urllib] Request completed: {method} {request_url} -> {response_status}")
            logger.log_request(
                language="python",
                method=method,
                url=request_url,
                headers=headers,
                body=body,
                response_status=response_status,
                response_headers=response_headers,
                response_body=response_body,
                duration_ms=duration_ms,
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
            _debug_log("[urllib] log_request() completed")

            # Return wrapped response (which provides cached body) or original if body wasn't read
            return wrapped_response

        except Exception as e:
            # Log the failed request
            duration_ms = (time.time() - start_time) * 1000
            logger.log_request(
                language="python",
                method=method,
                url=request_url,
                headers=headers,
                body=body,
                error=str(e),
                duration_ms=duration_ms,
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
            raise

    return wrapper


def _intercept_urllib() -> None:
    """Intercept urllib.request.urlopen."""
    try:
        import urllib.request

        global _original_urllib_urlopen

        if _original_urllib_urlopen is None:
            _original_urllib_urlopen = urllib.request.urlopen
            urllib.request.urlopen = _wrap_urllib_urlopen(_original_urllib_urlopen)

            logger.log_request(
                language="python",
                method="INTERCEPT",
                url="urllib.request.urlopen",
                headers={},
                body="urllib.request.urlopen interceptor installed",
                server_name=_get_server_name(),
                execution_id=os.environ.get("EXECUTION_ID"),
                workflow_id=os.environ.get("WORKFLOW_ID"),
            )
    except ImportError:
        pass


def setup_python_interceptor() -> None:
    """Setup all Python HTTP interceptors.
    
    Call this function at the start of your MCP server to intercept
    all HTTP requests made by Python libraries.
    
    This function is designed to fail gracefully - if interceptor setup fails,
    it will log the error but not raise an exception to allow the server to continue.
    """
    _debug_log("setup_python_interceptor() called")
    _debug_log(f"  EXECUTION_ID={os.environ.get('EXECUTION_ID')}")
    _debug_log(f"  WORKFLOW_ID={os.environ.get('WORKFLOW_ID')}")
    _debug_log(f"  MCP_SERVER_NAME={os.environ.get('MCP_SERVER_NAME')}")
    try:
        _intercept_requests()
        _debug_log("  _intercept_requests() completed")
        _intercept_aiohttp()
        _debug_log("  _intercept_aiohttp() completed")
        _intercept_httpx()
        _debug_log("  _intercept_httpx() completed")
        _intercept_urllib()
        _debug_log("  _intercept_urllib() completed")
        
        # Log setup success (but don't fail if logging fails)
        try:
            logger.log_request(
                language="python",
                method="SETUP",
                url="interceptor",
                headers={},
                body="Python HTTP interceptors initialized",
                server_name=_get_server_name(),
            )
            _debug_log("  setup log_request() completed successfully")
        except Exception as log_error:
            # Logging failed, but don't crash - just print to stderr
            import sys
            _debug_log(f"  setup log_request() failed: {log_error}")
            print(f"Warning: Failed to log interceptor setup: {log_error}", file=sys.stderr)
    except Exception as e:
        # Don't raise - allow server to start even if interceptor fails
        import sys
        _debug_log(f"  setup_python_interceptor() failed: {e}")
        print(f"Warning: HTTP interceptor setup failed: {e}", file=sys.stderr)
        # Return silently instead of raising

