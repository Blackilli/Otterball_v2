"""
URL configuration for otterball_v2 project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

urlpatterns = [
    path("health/", lambda request: HttpResponse("OK", content_type="text/plain")),
    path("admin/", admin.site.urls),
    # Last, so it can own "" without shadowing anything above it.
    path("", include("sports.urls")),
]
# /media/ is not routed here: it is served by
# otterball_v2.middleware.WhiteNoiseWithMediaMiddleware, in every environment.
# `static()` used to do it and returns nothing when DEBUG is off, so crests
# worked in development and 404ed in production.
