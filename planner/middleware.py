import time


class ResponseTimeMiddleware:
    """Adds ``X-Response-Time-ms`` so latency is visible in Postman."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        response["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        return response
