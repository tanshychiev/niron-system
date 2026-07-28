from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal


def preserve_suppliers(apps, schema_editor):
    Supplier = apps.get_model('production', 'ProductionSupplier')
    Receipt = apps.get_model('production', 'FabricReceipt')
    for receipt in Receipt.objects.exclude(supplier='').iterator():
        supplier, _ = Supplier.objects.get_or_create(name=receipt.supplier.strip())
        receipt.supplier_ref_id = supplier.id
        receipt.save(update_fields=['supplier_ref'])


class Migration(migrations.Migration):
    dependencies = [
        ('production', '0002_production_plan_sizes'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name='ProductionSupplier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=150, unique=True)),
                ('phone', models.CharField(blank=True, default='', max_length=50)),
                ('location', models.CharField(blank=True, default='', max_length=255)),
                ('is_active', models.BooleanField(default=True)),
                ('note', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'ordering': ['name']},
        ),
        migrations.AddField(
            model_name='fabricreceipt', name='supplier_ref',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='fabric_receipts', to='production.productionsupplier'),
        ),
        migrations.RunPython(preserve_suppliers, migrations.RunPython.noop),
        migrations.CreateModel(
            name='ProductionExpense',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('expense_date', models.DateField(default=django.utils.timezone.localdate)),
                ('category', models.CharField(choices=[('SUPPLIER','Supplier Purchase'),('SEWING','Sewing Partner Expense'),('STAFF_COMMISSION','Staff Commission'),('OTHER','Other Production Expense')], max_length=30)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('payment_method', models.CharField(blank=True, default='', max_length=50)),
                ('reference', models.CharField(blank=True, default='', max_length=120)),
                ('note', models.TextField(blank=True, default='')),
                ('finance_expense_id', models.PositiveIntegerField(blank=True, editable=False, null=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='production_expenses_created', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expense_records', to='production.productionproject')),
                ('sewing_partner', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='expenses', to='production.sewingpartner')),
                ('supplier', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='expenses', to='production.productionsupplier')),
            ],
            options={'ordering':['-expense_date','-id'], 'permissions':[('view_production_expense','Can view production expenses')]},
        ),
    ]
