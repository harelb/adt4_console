"""The chat goal card's repair warning (Nov 20 field log)."""

from adt4_console.chat import repair_warning

RUNNING = ("(and (object-placed-by euclid o749 t6784) (object-placed-by euclid o750 t7259) "
           "(object-placed-by euclid o751 t6762) (visited-place-by hamilton t1670) "
           "(visited-object-by gauss o128) (visited-object-by gauss o129))")  # fmt: skip
EG = [{"predicate": "use-robots", "symbols": ["euclid", "gauss"]}]


def test_only_the_new_tasks_warns_about_every_dropped_one():
    # 10:36:04: the chat's first repair
    w = repair_warning("(and (object-placed-by euclid o752 t6762) (visited-object-by gauss o753))", EG, [RUNNING])
    for t in ("o749", "o750", "o751", "o128", "o129"):
        assert t in w
    assert "hamilton" not in w  # not covered by use-robots: its task is not replaced
    assert "O751, O752 all go to the same place" not in w  # o751 is not in the new goal


def test_the_full_repair_warns_about_nothing_but_a_shared_place():
    goal = ("(and (object-placed-by euclid o749 t6784) (object-placed-by euclid o750 t7259) "
            "(object-placed-by euclid o752 t7259) (visited-object-by gauss o128) "
            "(visited-object-by gauss o129) (visited-object-by gauss o753))")  # fmt: skip
    w = repair_warning(goal, EG, [RUNNING])
    assert "o751" in w and "already done" in w  # left out: fine only if done
    assert "O750, O752 all go to the same place T7259" in w


def test_a_team_goal_covers_every_robot():
    w = repair_warning("(object-placed-by euclid o749 t6784)", [], [RUNNING])
    assert "hamilton" in w and "gauss" in w


def test_nothing_running_nothing_to_warn():
    assert repair_warning("(object-placed-by euclid o749 t6784)", EG, []) == ""


def test_sim_initial_goal_three_cones_one_place():
    w = repair_warning("(and (object-placed-by hamilton o749 t6792) (object-placed-by euclid o750 t6792) "
                       "(object-in-place o751 t6792))", [], [])  # fmt: skip
    assert "O749, O750, O751 all go to the same place T6792" in w
