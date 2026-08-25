import httpx2
from aiolimiter import AsyncLimiter


class RateLimitedAsyncTransport(httpx2.AsyncHTTPTransport):
    def __init__(self, requests_per_second: float, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.limiter = AsyncLimiter(max_rate=requests_per_second, time_period=1.0)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        async with self.limiter:
            return await super().handle_async_request(request)
