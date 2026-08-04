from django.db import migrations, models
import django.db.models.deletion


def migrate_existing_projects(apps, schema_editor):
    Project = apps.get_model("production", "ProductionProject")
    ProjectColor = apps.get_model("production", "ProductionProjectColor")
    Plan = apps.get_model("production", "ProductionPlanSize")
    Cut = apps.get_model("production", "CuttingSizeLine")
    Usage = apps.get_model("production", "CuttingRollUsage")
    Job = apps.get_model("production", "SewingJob")

    for project in Project.objects.exclude(color_id=None).iterator():
        pc, _ = ProjectColor.objects.get_or_create(
            project_id=project.id,
            color_id=project.color_id,
            defaults={"sort_order": 0},
        )
        Plan.objects.filter(project_id=project.id, project_color_id=None).update(project_color_id=pc.id)
        Cut.objects.filter(project_id=project.id, project_color_id=None).update(project_color_id=pc.id)
        Usage.objects.filter(project_id=project.id, project_color_id=None).update(project_color_id=pc.id)
        Job.objects.filter(project_id=project.id, project_color_id=None).update(project_color_id=pc.id)


class Migration(migrations.Migration):
    dependencies = [("production", "0005_productionproject_additional_fabric_types")]

    operations = [
        migrations.CreateModel(
            name="ProductionProjectColor",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("note", models.CharField(blank=True, default="", max_length=255)),
                ("color", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_project_colors", to="inventory.color")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="project_colors", to="production.productionproject")),
            ],
            options={"ordering": ["sort_order", "color__name", "id"]},
        ),
        migrations.AddConstraint(
            model_name="productionprojectcolor",
            constraint=models.UniqueConstraint(fields=("project", "color"), name="uniq_production_project_color"),
        ),
        migrations.AlterField(
            model_name="productionproject",
            name="color",
            field=models.ForeignKey(blank=True, help_text="Legacy first colour; new projects use project_colors.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name="production_projects", to="inventory.color"),
        ),
        migrations.AddField(model_name="productionplansize", name="project_color", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="plan_sizes", to="production.productionprojectcolor")),
        migrations.AddField(model_name="cuttingsizeline", name="project_color", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="cut_sizes", to="production.productionprojectcolor")),
        migrations.AddField(model_name="cuttingrollusage", name="project_color", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="roll_usages", to="production.productionprojectcolor")),
        migrations.AddField(model_name="sewingjob", name="project_color", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="sewing_jobs", to="production.productionprojectcolor")),
        migrations.RunPython(migrate_existing_projects, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="productionplansize", name="uniq_project_plan_size"),
        migrations.RemoveConstraint(model_name="cuttingsizeline", name="uniq_project_cut_size"),
        migrations.RemoveConstraint(model_name="cuttingrollusage", name="uniq_project_fabric_roll"),
        migrations.AddConstraint(model_name="productionplansize", constraint=models.UniqueConstraint(fields=("project_color", "size"), name="uniq_project_color_plan_size")),
        migrations.AddConstraint(model_name="cuttingsizeline", constraint=models.UniqueConstraint(fields=("project_color", "size"), name="uniq_project_color_cut_size")),
        migrations.AddConstraint(model_name="cuttingrollusage", constraint=models.UniqueConstraint(fields=("project_color", "roll"), name="uniq_project_color_fabric_roll")),
        migrations.RemoveField(model_name="productionproject", name="additional_fabric_types"),
    ]
