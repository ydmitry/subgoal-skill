"""slugify: the example module. Its README.md is the task. The loop's worker must not change this file."""
import re


def slugify(title, max_length=40):
    """Lowercase the title, turn every run of characters other than a-z and 0-9 into one "-",
    strip "-" from both ends, and cut the result to max_length characters (never ending in "-")."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_length].rstrip("-")
