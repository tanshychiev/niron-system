from django.db import migrations, models
import django.db.models.deletion


def copy_expected_qty_to_plan(apps, schema_editor):
    """Preserve old projects by putting the legacy total into the first active size."""
    ProductionProject = apps.get_model("production", "ProductionProject")
    ProductionPlanSize = apps.get_model("production", "ProductionPlanSize")
    Size = apps.get_model("inventory", "Size")
    first_size = Size.objects.filter(is_active=True).order_by("sort_order", "id").first()
    if not first_size:
        return
    for project in ProductionProject.objects.filter(expected_qty__gt=0).iterator():
        ProductionPlanSize.objects.get_or_create(
            project_id=project.id,
            size_id=first_size.id,
            defaults={"planned_qty": project.expected_qty},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_initial"),
        ("production", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductionPlanSize",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("planned_qty", models.PositiveIntegerField(default=0)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="plan_sizes", to="production.productionproject")),
                ("size", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_plan_lines", to="inventory.size")),
            ],
            options={"ordering": ["size__sort_order", "size__id"]},
        ),
        migrations.AddConstraint(
            model_name="productionplansize",
            constraint=models.UniqueConstraint(fields=("project", "size"), name="uniq_project_plan_size"),
        ),
        migrations.RunPython(copy_expected_qty_to_plan, migrations.RunPython.noop),
    ]
