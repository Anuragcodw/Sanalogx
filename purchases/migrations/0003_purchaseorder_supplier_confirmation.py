import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('purchases', '0002_purchaserequest_purchaserequestitem_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseorder',
            name='email_status',
            field=models.CharField(
                choices=[('NOT_SENT', 'Not Sent'), ('SENT', 'Sent'), ('FAILED', 'Failed')],
                default='NOT_SENT',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='purchaseorder',
            name='email_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='purchaseorder',
            name='supplier_confirmation_reference',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='purchaseorder',
            name='supplier_confirmed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='purchaseorder',
            name='supplier_confirmed_eta',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='purchaseorder',
            name='supplier_confirmed_by',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='purchase_orders_supplier_confirmed',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]