import unittest

from statement_parser.money import parse_minor_units


class ParseMinorUnitsTest(unittest.TestCase):
    def test_parses_grouped_rupee_amount_exactly(self) -> None:
        self.assertEqual(5_009_998, parse_minor_units("50,099.98"))

    def test_parses_indian_lakh_grouping_exactly(self) -> None:
        self.assertEqual(45_404_160, parse_minor_units("4,54,041.60"))

    def test_rejects_malformed_grouping(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid monetary amount"):
            parse_minor_units("4,5,4041.60")

    def test_rejects_more_than_two_fraction_digits(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid monetary amount"):
            parse_minor_units("1.001")


if __name__ == "__main__":
    unittest.main()
