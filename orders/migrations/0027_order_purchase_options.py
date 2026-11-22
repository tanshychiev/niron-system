from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("orders", "0026_order_production_film_meter_and_more")]
    operations = [
        migrations.AddField(model_name="order", name="product_source", field=models.CharField(max_length=12, default="EXISTING", choices=[("EXISTING", "Niron Stock"), ("OUTSIDE", "Buy Outside (Skip Stock)")])),
        migrations.AddField(model_name="order", name="printing_provider", field=models.CharField(max_length=12, default="NIRON", choices=[("NIRON", "Niron Printing / Service"), ("SUPPLIER", "Supplier Full Service"), ("NONE", "No Printing / Service")])),
    ]
