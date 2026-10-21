"""The example's finish line. Run the loop with --protect test_fizzbuzz.py."""
import unittest

from fizzbuzz import fizzbuzz


class FizzBuzzTest(unittest.TestCase):
    def test_plain_numbers(self):
        self.assertEqual([fizzbuzz(n) for n in (1, 2, 4)], ["1", "2", "4"])

    def test_multiples_of_three(self):
        self.assertEqual([fizzbuzz(n) for n in (3, 6, 9)], ["Fizz", "Fizz", "Fizz"])

    def test_multiples_of_five(self):
        self.assertEqual([fizzbuzz(n) for n in (5, 10, 20)], ["Buzz", "Buzz", "Buzz"])

    def test_multiples_of_fifteen(self):
        self.assertEqual([fizzbuzz(n) for n in (15, 30, 45)], ["FizzBuzz", "FizzBuzz", "FizzBuzz"])


if __name__ == "__main__":
    unittest.main()
