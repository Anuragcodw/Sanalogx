from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, PharmacistProfile, EmailVerification


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = (
        'username', 'email', 'full_name', 'role', 'status',
        'email_verified', 'is_active'
    )
    list_filter = ('role', 'status', 'is_active', 'is_staff', 'email_verified')
    search_fields = ('username', 'email', 'full_name', 'phone')
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = BaseUserAdmin.fieldsets + (
        ('MediVault Role & Status', {
            'fields': ('full_name', 'phone', 'role', 'status', 'rejection_reason')
        }),
        ('Email Verification Status', {
            'fields': ('email_verified',)
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('MediVault Details', {
            'fields': ('full_name', 'phone', 'role', 'status')
        }),
    )


@admin.register(PharmacistProfile)
class PharmacistProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'registration_number', 'qualification', 'created_at')
    search_fields = ('user__username', 'user__email', 'registration_number')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(EmailVerification)
class EmailVerificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'token', 'is_verified', 'created_at', 'expires_at')
    list_filter = ('is_verified',)
    search_fields = ('user__username', 'user__email', 'token')
    readonly_fields = ('created_at',)
