"""Behavior checks for the templates actually shipped in the blueprints."""
from pathlib import Path
from types import SimpleNamespace
import unittest

import jinja2
import yaml


class BlueprintLoader(yaml.SafeLoader):
    pass


BlueprintLoader.add_constructor("!input", lambda loader, node: {"input": loader.construct_scalar(node)})
ROOT = Path(__file__).resolve().parents[1]


def load(name):
    return yaml.load((ROOT / name).read_text(), Loader=BlueprintLoader)


def render_bool(template, entity_states=None, **variables):
    states = entity_states or {}
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["is_state"] = lambda entity, state: states.get(entity, "unknown") == state
    env.globals["is_state"] = env.tests["is_state"]
    return env.from_string(template).render(**variables).strip() == "True"


class OverrideBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bp = load("presence-device-control.yaml")
        cls.manual_template = cls.bp["actions"][0]["if"][1]["value_template"]
        cls.unpaused = cls.bp["actions"][1]["value_template"]

    def manual(self, state, user, timers=("timer.override",)):
        trigger = SimpleNamespace(to_state=SimpleNamespace(state=state, context=SimpleNamespace(user_id=user)))
        return render_bool(self.manual_template, pause_timers=list(timers), trigger=trigger)

    def test_user_on_and_off_start_override(self):
        for state in ("on", "off"):
            with self.subTest(state=state):
                self.assertTrue(self.manual(state, "signed-in-user"))

    def test_automation_and_recovery_events_do_not_start_override(self):
        self.assertFalse(self.manual("on", None))
        self.assertFalse(self.manual("off", None))
        self.assertFalse(self.manual("unknown", "signed-in-user"))
        self.assertFalse(self.manual("unavailable", "signed-in-user"))

    def test_no_timer_retains_original_behavior(self):
        self.assertFalse(self.manual("off", "signed-in-user", timers=()))
        self.assertTrue(render_bool(self.unpaused, pause_timers=[]))

    def test_active_paused_and_missing_timer_block_control(self):
        for state in ("active", "paused", "unknown", "unavailable"):
            with self.subTest(state=state):
                self.assertFalse(render_bool(self.unpaused, {"timer.override": state}, pause_timers=["timer.override"]))

    def test_all_timers_must_be_idle(self):
        self.assertTrue(render_bool(self.unpaused, {"timer.a": "idle"}, pause_timers=["timer.a"]))
        self.assertFalse(render_bool(self.unpaused, {"timer.a": "idle", "timer.b": "active"}, pause_timers=["timer.a", "timer.b"]))

    def test_timer_state_change_reconciles_without_waiting_for_presence_edge(self):
        triggers = self.bp["triggers"]
        self.assertTrue(any(t.get("id") == "pause_changed" and t.get("to", "absent") is None for t in triggers))


class NightModeBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bp = load("night-lighting-occupancy.yaml")
        cls.allowed = cls.bp["conditions"][0]["value_template"]
        cls.sleep = cls.bp["actions"][0]["choose"][0]["conditions"][0]["conditions"][1]["value_template"]

    def evaluate(self, state, timers=("input_select.mode",)):
        variables = dict(home_mode_entities=list(timers), away_mode_value="Ушли", sleep_mode_value="Сон")
        states = {"input_select.mode": state}
        return (render_bool(self.allowed, states, **variables), render_bool(self.sleep, states, **variables))

    def test_away_suppresses_occupancy_commands(self):
        self.assertEqual(self.evaluate("Ушли"), (False, False))

    def test_sleep_enables_night_lighting_outside_time_window(self):
        self.assertEqual(self.evaluate("Сон"), (True, True))

    def test_home_leaves_time_schedule_in_charge(self):
        self.assertEqual(self.evaluate("Дома"), (True, False))

    def test_optional_helper_preserves_original_schedule(self):
        self.assertEqual(self.evaluate("Сон", timers=()), (True, False))

    def test_sleep_dims_main_light_while_home_can_keep_its_original_behavior(self):
        condition = self.bp["actions"][0]["choose"][0]["sequence"][0]["choose"][1]["sequence"][0]["if"][0]["value_template"]
        variables = dict(home_mode_entities=["input_select.mode"], sleep_mode_value="Сон", turn_off_main_in_ambilight=False)
        self.assertFalse(render_bool(condition, {"input_select.mode": "Дома"}, **variables))
        self.assertTrue(render_bool(condition, {"input_select.mode": "Сон"}, **variables))


class HomeModePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bp = load("home-mode-control.yaml")

    def commands(self, mode, media_state):
        # Interpret only native state/or conditions and the two if branches.
        # Expected commands below describe the user-visible contract.
        inputs = {"mode_entity": "input_select.mode", "away_mode_value": "Ушли", "sleep_mode_value": "Сон"}
        def value(item):
            return inputs[item["input"]] if isinstance(item, dict) and "input" in item else item
        def passes(condition):
            if condition["condition"] == "or":
                return any(passes(c) for c in condition["conditions"])
            return value(condition["entity_id"]) == "input_select.mode" and mode == value(condition["state"])
        out = []
        for branch in self.bp["actions"]:
            if all(passes(c) for c in branch["if"]):
                for step in branch["then"]:
                    if "action" in step:
                        out.append(step["action"])
                    else:
                        inner = step["repeat"]["sequence"][0]
                        condition = inner["if"][0]["value_template"]
                        if render_bool(condition, {"media_player.test": media_state}, repeat=SimpleNamespace(item="media_player.test")):
                            out.extend(s["action"] for s in inner["then"])
        return out

    def test_home_never_forces_devices_back_on(self):
        self.assertEqual(self.commands("Дома", "playing"), [])

    def test_away_turns_lights_off_and_pauses_playing_music(self):
        self.assertEqual(self.commands("Ушли", "playing"), ["light.turn_off", "media_player.media_pause"])

    def test_sleep_pauses_music_without_blackout(self):
        self.assertEqual(self.commands("Сон", "playing"), ["media_player.media_pause"])

    def test_idle_and_unavailable_players_receive_no_pause(self):
        for state in ("idle", "off", "paused", "unavailable", "unknown"):
            with self.subTest(state=state):
                self.assertEqual(self.commands("Сон", state), [])

    def test_unrestored_mode_performs_no_actions(self):
        self.assertEqual(self.commands("unknown", "playing"), [])

    def test_mode_trigger_ignores_attribute_only_changes(self):
        self.assertIsNone(self.bp["triggers"][0]["to"])


if __name__ == "__main__":
    unittest.main()
