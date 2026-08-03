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
