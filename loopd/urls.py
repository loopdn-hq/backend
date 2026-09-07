"""
URL configuration for loopd project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
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
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve

from loopd.health import healthz

urlpatterns = [
    # No trailing slash, deliberately: APPEND_SLASH would answer /healthz with a
    # 301 to /healthz/, and the load balancer's health check matches 200 only.
    path('healthz', healthz, name='healthz'),

    path('admin/', admin.site.urls),

    # Ahead of user.urls so the nested phone routes are matched first. The two
    # cannot actually collide - the user router's detail pattern excludes
    # slashes - but the ordering says the intent out loud.
    path('api/v1/users/me/phone/', include('otp.urls')),

    path('api/v1/users/', include('user.urls')),
    path('api/v1/chat/', include('chat.urls')),

    # The one public route a phone network calls. Authenticated by signature
    # rather than by a token - see sms/webhooks.py.
    path('api/v1/sms/', include('sms.urls')),
]

if not settings.AWS_STORAGE_BUCKET_NAME:
    # Keyed on the bucket rather than on DEBUG, because storage is what decides
    # this: with AWS_STORAGE_BUCKET_NAME set, uploads live in S3 and are served
    # from presigned URLs without ever reaching Django, so this branch is dead
    # in staging and production. With it unset, FileSystemStorage puts them on
    # local disk under MEDIA_ROOT and Django is the ONLY thing that can hand
    # them back - whitenoise covers STATIC_ROOT and nothing else.
    #
    # It used to be `if settings.DEBUG`, which held only as long as local meant
    # `runserver`. The compose stack runs with DEBUG=False, so every upload
    # 404'd on read while continuing to write to disk perfectly happily.
    #
    # django.views.static.serve directly rather than the conf.urls.static
    # helper, because that helper returns [] whenever DEBUG is False - so the
    # condition above could never have worked through it. serve() is single
    # threaded and unaccelerated, which is exactly why the bucket exists for
    # anything real; here it is the difference between attachments loading and
    # not. It refuses `..` itself, so the prefix match is not the only guard.
    urlpatterns += [
        re_path(
            r'^%s(?P<path>.*)$' % settings.MEDIA_URL.lstrip('/'),
            serve,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
