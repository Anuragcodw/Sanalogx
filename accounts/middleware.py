"""
accounts/middleware.py
Security & Cache-Control Middleware for MediVault.
"""


class NoCacheAuthenticatedMiddleware:
    """
    Ensures that authenticated responses and auth-sensitive endpoints are never
    cached by client browsers or intermediate proxies.
    Prevents sensitive dashboard data from appearing via back-button or history after logout.
    Preserves normal caching for static and media assets.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # Do not alter cache headers for static or media assets
        path = request.path_info
        if path.startswith('/static/') or path.startswith('/media/'):
            return response

        user = getattr(request, 'user', None)
        is_authenticated = bool(user and user.is_authenticated)

        is_auth_sensitive = (
            path.startswith('/auth/') or
            path.startswith('/login/') or
            path.startswith('/register/') or
            path.startswith('/admin-dashboard/') or
            path.startswith('/pharmacist-dashboard/') or
            path.startswith('/medicines/') or
            path.startswith('/inventory/') or
            path.startswith('/sales/') or
            path.startswith('/purchases/') or
            path.startswith('/dispensing/') or
            path.startswith('/reports/') or
            path.startswith('/analytics/') or
            path.startswith('/suppliers/') or
            path.startswith('/notifications/')
        )

        if is_authenticated or is_auth_sensitive:
            response['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0, private'
            response['Pragma'] = 'no-cache'
            response['Expires'] = '0'

        return response
