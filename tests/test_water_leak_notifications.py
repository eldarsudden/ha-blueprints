from types import SimpleNamespace
import unittest

import jinja2

from test_manual_override_and_modes import load


class LeakNotifications(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bp = load("water-leak-notifications.yaml")

    def render(self, template, states=None, areas=None, **variables):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.globals["states"] = lambda entity: (states or {}).get(entity, "unknown")
        env.globals["area_name"] = lambda entity: (areas or {}).get(entity)
        env.globals["state_attr"] = lambda entity, key: None
        return env.from_string(template).render(**variables).strip()

    def acknowledgment(self, stored, received):
        template = self.bp["conditions"][0]["conditions"][0]["conditions"][1]["value_template"]
        return self.render(template, {"input_text.token": stored}, acknowledgment_token="input_text.token",
                           trigger=SimpleNamespace(event=SimpleNamespace(data={"action": received}))) == "True"

    def test_current_acknowledgment_is_accepted(self):
        self.assertTrue(self.acknowledgment("LEAK_ACK_current", "LEAK_ACK_current"))

    def test_old_and_unrelated_actions_cannot_acknowledge_a_new_alert(self):
        self.assertFalse(self.acknowledgment("LEAK_ACK_current", "LEAK_ACK_previous"))
        self.assertFalse(self.acknowledgment("LEAK_ACK_current", "OPEN_DOOR"))
        self.assertFalse(self.acknowledgment("", ""))

    def test_rooms_are_deduplicated(self):
        template = self.bp["actions"][1]["variables"]["affected_rooms"]
        result = self.render(template, areas={"binary_sensor.a": "Ванная", "binary_sensor.b": "Ванная", "binary_sensor.c": "Кухня"},
                             wet_sensors=["binary_sensor.a", "binary_sensor.b", "binary_sensor.c"], rooms_memory="input_text.rooms")
        self.assertEqual(result, "Ванная, Кухня")

    def test_rooms_survive_restart_after_the_sensor_dries(self):
        template = self.bp["actions"][1]["variables"]["affected_rooms"]
        self.assertEqual(self.render(template, {"input_text.rooms": "Туалет"}, wet_sensors=[], rooms_memory="input_text.rooms"), "Туалет")

    def test_missing_room_has_a_readable_fallback(self):
        template = self.bp["actions"][1]["variables"]["affected_rooms"]
        self.assertEqual(self.render(template, wet_sensors=[], rooms_memory="input_text.rooms"), "Помещение не определено")

    def test_room_memory_fits_the_helper(self):
        template = self.bp["actions"][1]["variables"]["affected_rooms"]
        result = self.render(template, areas={"binary_sensor.a": "Длинное имя " * 50}, wet_sensors=["binary_sensor.a"], rooms_memory="input_text.rooms")
        self.assertLessEqual(len(result), 180)

    def test_acknowledgment_exits_without_reactivating_the_alarm(self):
        sequence = self.bp["actions"][0]["choose"][0]["sequence"]
        self.assertEqual(sequence[0]["action"], "input_boolean.turn_off")
        self.assertIn("stop", sequence[-1])

    def test_no_valve_or_device_control_can_be_cancelled_by_acknowledgment(self):
        def actions(value):
            if isinstance(value, dict):
                if isinstance(value.get("action"), str):
                    yield value["action"]
                for child in value.values():
                    yield from actions(child)
            elif isinstance(value, list):
                for child in value:
                    yield from actions(child)
        self.assertTrue(all(a.split(".")[0] in {"input_boolean", "input_text", "persistent_notification"} for a in actions(self.bp)))


if __name__ == "__main__":
    unittest.main()
