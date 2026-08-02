"""frontend_server URL configuration."""

from django.contrib import admin
from django.urls import include, path, re_path

from translator import views as translator_views

urlpatterns = [
    path("", translator_views.landing, name="landing"),
    path("simulator_home", translator_views.home, name="home"),
    re_path(
        r"^demo/(?P<sim_code>[\w-]+)/(?P<step>[\w-]+)/(?P<play_speed>[\w-]+)/$",
        translator_views.demo,
        name="demo",
    ),
    re_path(
        r"^replay/(?P<sim_code>[\w-]+)/(?P<step>[\w-]+)/$",
        translator_views.replay,
        name="replay",
    ),
    re_path(
        r"^replay_persona_state/(?P<sim_code>[\w-]+)/(?P<step>[\w-]+)/(?P<persona_name>[\w-]+)/$",
        translator_views.replay_persona_state,
        name="replay_persona_state",
    ),
    path("process_environment/", translator_views.process_environment, name="process_environment"),
    path("update_environment/", translator_views.update_environment, name="update_environment"),
    path("replay_movement/", translator_views.replay_movement, name="replay_movement"),
    path("path_tester/", translator_views.path_tester, name="path_tester"),
    path("path_tester_update/", translator_views.path_tester_update, name="path_tester_update"),
    path("health/", translator_views.health, name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
]
