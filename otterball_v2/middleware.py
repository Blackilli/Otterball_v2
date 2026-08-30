"""Middleware for the single-container deployment.

There is no reverse proxy in front of the `web` service - compose.yml
publishes gunicorn on 8000 directly - so whatever serves `/static/` has to
serve `/media/` as well, or nothing does.
"""

from django.conf import settings
from whitenoise.middleware import WhiteNoiseMiddleware


class WhiteNoiseWithMediaMiddleware(WhiteNoiseMiddleware):
    """WhiteNoise, plus MEDIA_ROOT served at MEDIA_URL.

    Team crests used to be served by `django.conf.urls.static.static()` in the
    URLconf, which returns an empty list when DEBUG is off - so media worked in
    development and 404ed in production, and every crest on the public pages
    silently fell back to initials. Routing both trees through one middleware
    is the actual fix: development and production now take the same code path,
    and the old split is precisely what let the difference go unnoticed.

    WhiteNoise's docs warn that it is not built for user-uploaded media. Two of
    the three reasons do not apply here: nothing is user-uploaded (the Celery
    worker downloads crests during ingestion) and there are ~600 files totalling
    ~15 MB, a fixed set that tracks the number of teams. The third one does -
    WhiteNoise indexes files once, at startup, while the worker writes new
    crests into the shared volume `web` is already running against - which is
    what `WHITENOISE_AUTOREFRESH` in settings is for.
    """

    def __init__(self, get_response=None, settings=settings):
        super().__init__(get_response, settings)

        if settings.MEDIA_ROOT and settings.MEDIA_URL:
            # After super().__init__, so that in autorefresh mode - where
            # add_files prepends - media is checked before static. The two
            # prefixes cannot overlap anyway; this just keeps the order
            # meaningful if MEDIA_URL is ever changed to sit under STATIC_URL.
            self.add_files(settings.MEDIA_ROOT, prefix=settings.MEDIA_URL)
