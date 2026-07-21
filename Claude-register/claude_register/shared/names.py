"""随机美国姓名生成器。display_name 用，替代写死的 xiaoshua。"""

from __future__ import annotations

import random

_FIRST = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph",
    "Thomas", "Charles", "Christopher", "Daniel", "Matthew", "Anthony", "Mark",
    "Steven", "Paul", "Andrew", "Joshua", "Kenneth", "Mary", "Patricia", "Jennifer",
    "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen", "Lisa",
    "Nancy", "Betty", "Sandra", "Margaret", "Ashley", "Kimberly", "Emily", "Donna",
    "Michelle", "Carol", "Amanda", "Melissa", "Deborah", "Stephanie", "Rebecca",
    "Sharon", "Laura", "Cynthia", "Amy",
]

_LAST = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell",
    "Carter", "Roberts",
]


def random_american_name() -> str:
    """返回 "First Last"，如 "Michael Johnson"。"""
    return f"{random.choice(_FIRST)} {random.choice(_LAST)}"


def random_name_parts() -> tuple[str, str]:
    """返回 (display_name, full_name)，如 ("Michael", "Michael Johnson")。

    display_name 只取名，full_name 含姓，与真实用户习惯一致（显示名 ≠ 全名）。
    """
    first = random.choice(_FIRST)
    last = random.choice(_LAST)
    return first, f"{first} {last}"


if __name__ == "__main__":
    for _ in range(5):
        display, full = random_name_parts()
        print(f"display={display!r}  full={full!r}")
