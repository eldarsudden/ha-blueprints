from ast import literal_eval
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

from test_manual_override_and_modes import load


class SensorHealth(unittest.TestCase):
    def test_outage_grace_period_and_recovery(self):
        now = datetime.now(timezone.utc)
        sensors = ["binary_sensor.kitchen", "binary_sensor.bathroom"]
        records = {entity: SimpleNamespace(state="off", last_changed=now) for entity in sensors}

        class States(dict):
            def __call__(self, entity):
                return self[entity].state

        env = NativeEnvironment(undefined=StrictUndefined)
        template = env.from_string(load("water-leak-sensor-health.yaml")["variables"]["affected"])

        def affected(previous):
            value = template.render(now=lambda: now, states=States(records), sensors=sensors,
                                    previous=previous, delay_minutes=30)
            return literal_eval(value.strip()) if isinstance(value, str) else value

        kitchen, bathroom = sensors
        records[kitchen].state = "unknown"
        self.assertEqual(affected([]), [])
        records[kitchen].last_changed = now - timedelta(minutes=30)
        self.assertEqual(affected([]), [kitchen])
        records[kitchen].state = "unavailable"
        records[kitchen].last_changed = now
        self.assertEqual(affected([kitchen]), [kitchen])
        records[bathroom].state = "unavailable"
        records[bathroom].last_changed = now - timedelta(hours=1)
        self.assertEqual(affected([kitchen]), sorted(sensors))
        records[kitchen].state = "on"
        self.assertEqual(affected(sensors), [bathroom])
        records[bathroom].state = "off"
        self.assertEqual(affected([bathroom]), [])
