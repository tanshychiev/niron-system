from decimal import Decimal

from django.test import TestCase

from .models import Order, OrderItem


class PrintingServiceOrderTests(TestCase):
    def test_printing_service_requires_customer_cloth_details(self):
        order = Order(
            service_type=Order.SERVICE_PRINT_HEATPRESS,
            customer_name="Test",
            deadline="2026-08-10",
        )
        order.save()

        item = OrderItem(
            order=order,
            description="250 GSM Cotton / Black / XL",
            quantity=2,
            unit_price=Decimal("3.00"),
        )
        item.save()

        self.assertEqual(item.line_total, Decimal("6.00"))
        self.assertIsNone(item.shirt_item)

from django.utils import timezone

from inventory.models import Color, InventoryBatch, InventoryBatchItem, InventoryItem, Size
from .models import OrderDesign
from .services import deduct_stock_for_order, restore_stock_for_order


class ToteBagInventoryOrderTests(TestCase):
    def setUp(self):
        self.tote = InventoryItem.objects.create(
            code="TB-TEST",
            name="Tote Bag Test",
            item_type=InventoryItem.TYPE_TOTE_BAG,
            unit=InventoryItem.UNIT_PCS,
        )
        self.size = Size.objects.create(
            code="TB-30X20",
            name="30x20",
            product_type=Size.PRODUCT_TOTE_BAG,
            sort_order=1,
        )
        self.color = Color.objects.create(
            code="TB-BLK",
            name="Tote Black",
            hex_code="#000000",
        )
        self.batch = InventoryBatch.objects.create(
            batch_no="TB-BATCH-TEST",
            supplier="Test",
            received_date=timezone.localdate(),
            status=InventoryBatch.STATUS_RECEIVED,
        )
        self.stock = InventoryBatchItem.objects.create(
            batch=self.batch,
            item=self.tote,
            color=self.color,
            size=self.size,
            qty_received=Decimal("10"),
            qty_arrived=Decimal("10"),
            qty_remaining=Decimal("10"),
        )

    def test_tote_bag_order_deducts_exact_variant_and_can_restore(self):
        order = Order.objects.create(
            customer_name="Tote Customer",
            deadline=timezone.localdate(),
            service_type=Order.SERVICE_FULL,
        )
        design = OrderDesign.objects.create(order=order, name="Tote", sort_order=1)
        OrderItem.objects.create(
            order=order,
            design=design,
            shirt_item=self.tote,
            color=self.color,
            size=self.size,
            quantity=Decimal("3"),
            unit_price=Decimal("2.00"),
        )

        deduct_stock_for_order(order)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.qty_remaining, Decimal("7"))

        restore_stock_for_order(order)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.qty_remaining, Decimal("10"))

    def test_tote_bag_rejects_shirt_size(self):
        shirt_size = Size.objects.create(
            code="S-TEST",
            name="S",
            product_type=Size.PRODUCT_SHIRT,
            sort_order=1,
        )
        order = Order.objects.create(
            customer_name="Tote Customer",
            deadline=timezone.localdate(),
            service_type=Order.SERVICE_FULL,
        )
        item = OrderItem(
            order=order,
            shirt_item=self.tote,
            color=self.color,
            size=shirt_size,
            quantity=Decimal("1"),
            unit_price=Decimal("2.00"),
        )

        with self.assertRaises(Exception):
            item.full_clean()
