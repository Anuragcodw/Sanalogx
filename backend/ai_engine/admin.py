from django.contrib import admin
from .models import AIRecommendation, AuditLog


@admin.register(AIRecommendation)
class AIRecommendationAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'recommendation_type', 'engine_type', 'medicine',
        'confidence_score', 'priority_score', 'status', 'generated_at'
    ]
    list_filter = ['recommendation_type', 'engine_type', 'status']
    search_fields = ['title', 'medicine__generic_name', 'supplier__company_name']
    readonly_fields = ['generated_at', 'reviewed_at']
    ordering = ['-priority_score', '-generated_at']
    fieldsets = (
        ('Recommendation', {
            'fields': ('recommendation_type', 'engine_type', 'title', 'message', 'status')
        }),
        ('Context', {
            'fields': ('medicine', 'supplier', 'recommended_supplier',
                       'recommended_quantity', 'forecasted_demand_30d', 'forecasted_demand_60d')
        }),
        ('Scores', {
            'fields': ('confidence_score', 'priority_score')
        }),
        ('Action Data', {
            'fields': ('action_data',)
        }),
        ('Review', {
            'fields': ('expires_at', 'reviewed_by', 'reviewed_at', 'rejection_reason', 'generated_at')
        }),
    )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = [
        'action_type', 'performed_by', 'object_type', 'object_id',
        'object_repr', 'ip_address', 'created_at'
    ]
    list_filter = ['action_type', 'created_at']
    search_fields = ['performed_by__username', 'object_repr', 'description']
    readonly_fields = ['created_at', 'action_type', 'performed_by', 'object_type',
                       'object_id', 'object_repr', 'changes', 'ip_address', 'user_agent']
    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False  # Audit logs must never be manually created via admin

    def has_change_permission(self, request, obj=None):
        return False  # Audit logs are immutable

    def has_delete_permission(self, request, obj=None):
        return False  # Audit logs must never be deleted
