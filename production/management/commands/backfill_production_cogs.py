from django.core.management.base import BaseCommand

from production.models import ProductionProject, SewingReturn
from production.services import sync_project_finished_goods_cost


class Command(BaseCommand):
    help = "Backfill finished-shirt unit costs and historical COGS from production records."

    def handle(self, *args, **options):
        projects = (
            ProductionProject.objects.filter(
                sewing_jobs__returns__status=SewingReturn.STATUS_STOCKED
            )
            .distinct()
            .order_by("id")
        )

        project_count = 0
        item_count = 0
        consumption_count = 0

        for project in projects.iterator():
            result = sync_project_finished_goods_cost(project)
            project_count += 1
            item_count += result["inventory_items"]
            consumption_count += result["consumptions"]
            self.stdout.write(
                f"{project.project_no}: {result['inventory_items']} stock rows, "
                f"{result['consumptions']} consumption rows updated"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {project_count} projects, {item_count} stock rows, "
                f"{consumption_count} COGS consumption rows updated."
            )
        )
