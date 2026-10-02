from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("production", "0012_fabric_delivery_charge"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SewingJobHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(choices=[("CREATE", "Created"), ("UPDATE", "Updated")], default="UPDATE", max_length=20)),
                ("summary", models.CharField(blank=True, default="", max_length=255)),
                ("before_data", models.JSONField(blank=True, default=dict)),
                ("after_data", models.JSONField(blank=True, default=dict)),
                ("changes", models.JSONField(blank=True, default=dict)),
                ("changed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("changed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="production_sewing_job_history", to=settings.AUTH_USER_MODEL)),
                ("job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="history_entries", to="production.sewingjob")),
            ],
            options={"ordering": ["-changed_at", "-id"]},
        ),
    ]
