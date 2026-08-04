from django.db import migrations, models
import django.db.models.deletion


def preserve_existing_jobs(apps, schema_editor):
    SewingJob = apps.get_model("production", "SewingJob")
    SewingJob.objects.filter(worker_type="").update(worker_type="PARTNER")


class Migration(migrations.Migration):
    dependencies = [("production", "0006_multi_color_projects")]

    operations = [
        migrations.AddField(
            model_name="sewingjob",
            name="worker_type",
            field=models.CharField(
                choices=[("PARTNER", "Sewing Partner"), ("STAFF", "Internal Staff")],
                default="PARTNER",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="sewingjob",
            name="staff_name",
            field=models.CharField(blank=True, default="", max_length=150),
        ),
        migrations.AlterField(
            model_name="sewingjob",
            name="partner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sewing_jobs",
                to="production.sewingpartner",
            ),
        ),
        migrations.RunPython(preserve_existing_jobs, migrations.RunPython.noop),
    ]
