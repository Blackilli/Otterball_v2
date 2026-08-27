from django.urls import path

from sports import views

app_name = "sports"

urlpatterns = [
    path("", views.UpcomingMatchesView.as_view(), name="upcoming-matches"),
    path("seasons/<int:season_id>/", views.UpcomingMatchesView.as_view(), name="season-matches"),
    path("seasons/<int:season_id>/bracket/", views.BracketView.as_view(), name="season-bracket"),
    path("seasons/<int:season_id>/leaderboard/", views.LeaderboardView.as_view(), name="season-leaderboard"),
    path("seasons/<int:season_id>/stats/", views.StatsView.as_view(), name="season-stats"),
]
