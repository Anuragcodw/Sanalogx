from django.urls import path
from . import views

urlpatterns = [
    path('ai-intelligence/', views.ai_dashboard_view, name='ai_dashboard'),
    path('ai-intelligence/recommendations/', views.recommendation_center_view, name='ai_recommendation_center'),
    path('ai-intelligence/recommendations/<int:pk>/action/', views.handle_recommendation_action, name='ai_recommendation_action'),
    path('ai-intelligence/recommendations/convert-pr/', views.convert_recommendation_to_pr, name='ai_convert_recommendation_pr'),
    path('api/ai/chat/', views.ai_chat_api, name='api_ai_chat'),
    path('api/ai/recommendations/', views.ai_recommendations_api, name='api_ai_recommendations'),
    path('api/voice-assistant/', views.voice_assistant_api, name='api_voice_assistant'),
]

