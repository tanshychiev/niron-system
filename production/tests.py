from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from production.views import _validate_borib_required_for_cut


class BoribProductionValidationTests(SimpleTestCase):
    def test_cut_colour_requires_borib(self):
        with self.assertRaisesMessage(ValidationError, "Borib is required"):
            _validate_borib_required_for_cut("Black", 10, Decimal("0"))

    def test_uncut_colour_does_not_require_borib(self):
        _validate_borib_required_for_cut("White", 0, Decimal("0"))

    def test_cut_colour_accepts_positive_borib(self):
        _validate_borib_required_for_cut("Black", 10, Decimal("0.250"))
