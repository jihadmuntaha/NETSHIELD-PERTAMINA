import os
from typing import Optional
from fastapi import Request
from fastapi.responses import RedirectResponse

# Environment configuration with default fallbacks
NOC_ADMIN_USER = os.getenv("NOC_ADMIN_USER", "admin")
NOC_ADMIN_PASSWORD = os.getenv("NOC_ADMIN_PASSWORD", "password")


def verify_credentials(username: str, password: str) -> bool:
    """
    Validate username and password against environment variables.
    Defaults to 'admin' / 'password' if not set in environment.
    """
    expected_user = os.getenv("NOC_ADMIN_USER", "admin")
    expected_pass = os.getenv("NOC_ADMIN_PASSWORD", "password")
    return username == expected_user and password == expected_pass


def get_current_user(request: Request) -> Optional[str]:
    """
    Retrieve current logged-in operator username from request session.
    Returns username string or None if unauthenticated.
    """
    if hasattr(request, "session") and request.session:
        return request.session.get("user")
    return None


def require_login(request: Request) -> Optional[RedirectResponse]:
    """
    Helper function to check login status.
    Returns RedirectResponse to /login if user is unauthenticated, otherwise None.
    """
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return None
