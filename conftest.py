import math
import random

import pytest

HI = math.radians(83)


@pytest.fixture(scope="session")
def scene():
    from tw_fixtures import room_scene
    return room_scene()


@pytest.fixture(scope="session")
def demos(scene):
    from tw_connector import MockHost
    from tw_fixtures import BLIND_ID
    from tinker_learning.synthetic import record_scripted_demos
    rng = random.Random(1)
    pairs = [(rng.uniform(0, HI), rng.uniform(0, HI)) for _ in range(40)]
    pairs = [p for p in pairs if abs(p[0] - p[1]) > 0.1]
    return record_scripted_demos(MockHost(scene), scene, BLIND_ID, "tilt", pairs, seed=2)


@pytest.fixture(scope="session")
def holdout(scene):
    from tw_connector import MockHost
    from tw_fixtures import BLIND_ID
    from tinker_learning.synthetic import record_scripted_demos
    rng = random.Random(99)
    pairs = [(rng.uniform(0, HI), rng.uniform(0, HI)) for _ in range(14)]
    pairs = [p for p in pairs if abs(p[0] - p[1]) > 0.1]
    return record_scripted_demos(MockHost(scene), scene, BLIND_ID, "tilt", pairs, seed=3)


@pytest.fixture(scope="session")
def trained(tmp_path_factory, scene, demos, holdout):
    from tinker_learning import ModelRegistry
    from tinker_learning.pipeline import learn_skill
    reg = ModelRegistry(tmp_path_factory.mktemp("registry"))
    res = learn_skill(scene, demos, name="blind-tilt", registry=reg, holdout=holdout, seed=0, epochs=800)
    return reg, res
