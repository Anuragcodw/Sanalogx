from django.contrib import admin
from .models import Inventory, StockTransaction, StockAlert


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ['medicine', 'current_stock', 'available_stock', 'stock_status', 'last_updated']
    search_fields = ['medicine__medicine_name', 'medicine__generic_name']
    list_filter = ['stock_status']
    readonly_fields = ['created_at', 'last_updated']


@admin.register(StockTransaction)
class StockTransactionAdmin(admin.ModelAdmin):
    list_display = [
        'medicine', 'transaction_type', 'quantity',
        'previous_stock', 'new_stock', 'reference_number',
        'performed_by', 'created_at'
    ]
    list_filter = ['transaction_type', 'created_at']
    search_fields = ['medicine__medicine_name', 'reference_number']
    readonly_fields = [
        'inventory', 'medicine', 'batch', 'transaction_type',
        'quantity', 'previous_stock', 'new_stock', 'reference_number',
        'notes', 'performed_by', 'created_at'
    ]

    def has_add_permission(self, request):
        # Transactions should be immutable audit records
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = [
        'medicine', 'alert_type', 'severity', 'message',
        'is_active', 'is_acknowledged', 'created_at'
    ]
    list_filter = ['alert_type', 'severity', 'is_active', 'is_acknowledged']
    search_fields = ['medicine__medicine_name']
    readonly_fields = ['created_at', 'resolved_at', 'acknowledged_at']
