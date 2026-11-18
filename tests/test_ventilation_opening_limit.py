import ast
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

from test_manual_override_and_modes import load


class OpeningLimitCurve(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bp = load("air-quality-window-ventilation.yaml")
        cls.loop = cls.bp["actions"][1]["repeat"]["sequence"][0]["variables"]
        cls.keys = ["nominal_outdoor_band", "candidate_outdoor_band", "warming_since", "outdoor_band", "warming_target", "effective_opening_limit"]

    def new_cycle(self, base=15):
        return dict(sensor_opening_percentage=base, outdoor_band=None, warming_since=None, warming_target=None,
                    outdoor_warm=15, outdoor_cool=10, outdoor_cold=0, outdoor_hysteresis=1,
                    outdoor_warmup_seconds=120, outdoor_thresholds_valid=True)

    def step(self, variables, temperature, seconds=0, enabled=True, valid=True):
        env = NativeEnvironment(undefined=StrictUndefined)
        env.globals["now"] = lambda: datetime.fromtimestamp(seconds, timezone.utc)
        env.globals["as_timestamp"] = lambda dt: dt.timestamp()
        variables["readings"] = SimpleNamespace(outdoor_enabled=enabled, outdoor_valid=valid, outdoor_temperature=temperature)
        for key in self.keys:
            variables[key] = env.from_string(self.loop[key]).render(**variables)
        return variables["effective_opening_limit"]

    def test_default_base_is_fifteen_in_blueprint(self):
        self.assertEqual(self.bp["blueprint"]["input"]["opening_section"]["input"]["sensor_opening_percentage"]["default"], 15)

    def test_temperature_boundaries_at_base_fifteen(self):
        for temperature, expected in [(20, 15), (15, 15), (14.9, 11), (10, 11), (9.9, 8), (0, 8), (-0.1, 4), (-30, 4)]:
            with self.subTest(temperature=temperature):
                self.assertEqual(self.step(self.new_cycle(), temperature), expected)

    def test_user_base_controls_every_threshold(self):
        for temperature, expected in [(20, 20), (12, 15), (5, 10), (-5, 5)]:
            with self.subTest(temperature=temperature):
                self.assertEqual(self.step(self.new_cycle(20), temperature), expected)

    def test_curve_does_not_depend_on_current_device_limit(self):
        for device_value in (1, 11, 70, 100):
            variables = self.new_cycle(20)
            variables["device_opening_limit"] = device_value
            self.assertEqual(self.step(variables, 5), 10)

    def test_small_base_stays_above_zero(self):
        self.assertEqual(self.step(self.new_cycle(1), -10), 1)

    def test_disabled_outdoor_sensor_preserves_existing_base(self):
        self.assertEqual(self.step(self.new_cycle(11), None, enabled=False), 11)

    def test_unavailable_outdoor_sensor_uses_conservative_quarter(self):
        self.assertEqual(self.step(self.new_cycle(20), None, valid=False), 5)

    def test_cooling_reduces_opening_immediately(self):
        variables = self.new_cycle()
        self.assertEqual(self.step(variables, 20), 15)
        self.assertEqual(self.step(variables, -1, 1), 4)

    def test_hysteresis_does_not_increase_opening_near_boundary(self):
        variables = self.new_cycle()
        self.assertEqual(self.step(variables, 14), 11)
        self.assertEqual(self.step(variables, 15.5, 300), 11)

    def test_increase_waits_for_continuous_warming(self):
        variables = self.new_cycle()
        self.step(variables, 14)
        self.assertEqual(self.step(variables, 16.1, 1), 11)
        self.assertEqual(self.step(variables, 16.2, 120), 11)
        self.assertEqual(self.step(variables, 16.3, 121), 15)

    def test_temperature_drop_cancels_warmup(self):
        variables = self.new_cycle()
        self.step(variables, 14)
        self.step(variables, 16.2, 1)
        self.step(variables, 14.5, 60)
        self.assertEqual(self.step(variables, 16.2, 121), 11)
        self.assertEqual(self.step(variables, 16.2, 241), 15)

    def test_outdoor_sensor_is_removed_from_automatic_room_lookup(self):
        env = NativeEnvironment(undefined=StrictUndefined)
        result = env.from_string(self.bp["variables"]["temperature_entities"]).render(
            selected_temperature_sensors=["sensor.room", "sensor.oair"],
            outdoor_temperature_entities=["sensor.oair"])
        self.assertEqual(result, ["sensor.room"])

    def test_only_automatically_opened_windows_are_adjusted(self):
        condition = self.bp["actions"][1]["repeat"]["sequence"][1]["if"][0]["value_template"]
        env = NativeEnvironment(undefined=StrictUndefined)
        variables = dict(readings=SimpleNamespace(outdoor_enabled=True), sensor_opening_entity="number.limit",
                         outdoor_thresholds_valid=True, automatic_windows=[], close_windows=[],
                         effective_opening_limit=4, last_applied_opening_limit=15)
        self.assertFalse(env.from_string(condition).render(**variables))
        variables["automatic_windows"] = ["cover.automatic"]
        self.assertTrue(env.from_string(condition).render(**variables))
        variables["close_windows"] = ["cover.automatic"]
        self.assertFalse(env.from_string(condition).render(**variables))

    def readings(self, outdoor_state, outdoor_unit, apply_limit=True):
        states = {"sensor.room": "23", "sensor.co2": "1500", "sensor.outdoor": outdoor_state}
        units = {"sensor.room": "°C", "sensor.co2": "ppm", "sensor.outdoor": outdoor_unit}
        env = NativeEnvironment(undefined=StrictUndefined)
        env.globals.update(states=lambda entity: states[entity], state_attr=lambda entity, attr: units[entity],
                           is_number=lambda value: value not in ["unknown", "unavailable"])
        result = env.from_string(self.bp["variables"]["readings"]).render(
            co2_entities=["sensor.co2"], tvoc_entities=[], temperature_entities=["sensor.room"],
            outdoor_temperature_entities=["sensor.outdoor"], apply_outdoor_opening_limit=apply_limit, selected_area="room", co2_open=1000, co2_close=800,
            tvoc_open=500, tvoc_close=250)
        return ast.literal_eval(result.strip()) if isinstance(result, str) else result

    def test_warm_weather_independent_of_opening_limit(self):
        env = NativeEnvironment(undefined=StrictUndefined)
        active = env.from_string(self.bp["variables"]["warm_weather_active"])
        for outside, enabled, expected in [("21", True, True), ("20.9", True, False),
                                           ("23", False, False), ("unavailable", True, False)]:
            with self.subTest(outside=outside, enabled=enabled):
                readings = self.readings(outside, "°C", apply_limit=False)
                self.assertFalse(readings["outdoor_enabled"])
                self.assertEqual(active.render(readings=readings, keep_open_in_warm_weather=enabled,
                                               warm_outdoor_threshold=21), expected)

    def test_warm_opening_still_requires_room_heat_and_configured_limit(self):
        env = NativeEnvironment(undefined=StrictUndefined)
        env.tests["is_state"] = lambda entity, state: state == "closed"
        sequence = self.bp["actions"][1]["repeat"]["sequence"]
        choose = next(action["choose"] for action in sequence if "choose" in action)
        conditions = [self.bp["conditions"][1], choose[1]["conditions"][0]]
        for condition in conditions:
            template = env.from_string(condition["value_template"])
            for room, curve, number, expected in [(23, False, "", True), (18, False, "", False),
                                                   (23, True, "", False), (23, True, "number.limit", True)]:
                with self.subTest(condition=condition["alias"], room=room, curve=curve, number=number):
                    readings = dict(air_valid=False, high=False, temperature_valid=True, temperature=room,
                                    outdoor_enabled=curve)
                    self.assertEqual(template.render(readings=readings, warm_weather_active=True,
                                                     reopening_temperature=21, windows=["cover.test"],
                                                     sensor_opening_entity=number, outdoor_thresholds_valid=True), expected)

    def test_warm_weather_suppresses_air_closure_but_keeps_cold_protection(self):
        env = NativeEnvironment(undefined=StrictUndefined)
        template = env.from_string(self.loop["close_windows"])
        for warm, expected in [(True, ["cover.cold"]), (False, ["cover.cold", "cover.auto"])]:
            self.assertEqual(template.render(readings=dict(air_valid=True, recovered=True),
                                             automatic_windows=["cover.auto"], cold_close_windows=["cover.cold"],
                                             warm_weather_active=warm), expected)

    def test_unconfigured_optional_number_still_compiles(self):
        target = self.bp["actions"][1]["repeat"]["sequence"][1]["then"][0]["target"]["entity_id"]
        self.assertEqual(target, "{{ sensor_opening_entity }}")

    def test_outdoor_fahrenheit_is_converted_separately(self):
        result = self.readings("32", "°F")
        self.assertTrue(result["valid"])
        self.assertEqual(result["temperature"], 23)
        self.assertEqual(result["outdoor_temperature"], 0)

    def test_outdoor_kelvin_is_converted(self):
        self.assertEqual(self.readings("273.15", "K")["outdoor_temperature"], 0)

    def test_outdoor_failure_does_not_invalidate_room_cold_protection(self):
        result = self.readings("unavailable", "°C")
        self.assertTrue(result["temperature_valid"])
        self.assertTrue(result["valid"])
        self.assertFalse(result["outdoor_valid"])

    def test_missing_opening_actuator_blocks_new_opening_but_not_manual_window_protection(self):
        env = NativeEnvironment(undefined=StrictUndefined)
        variables = dict(windows=["cover.window"], readings=SimpleNamespace(air_valid=True, temperature_valid=True, high=True, temperature=23, outdoor_enabled=True),
                         reopening_temperature=22, sensor_opening_entity="", outdoor_thresholds_valid=True, warm_weather_active=False)
        template = self.bp["conditions"][1]["value_template"]
        env.tests["is_state"] = lambda entity, state: state == "closed"
        self.assertFalse(env.from_string(template).render(**variables))
        env.tests["is_state"] = lambda entity, state: state == "open"
        self.assertTrue(env.from_string(template).render(**variables))


if __name__ == "__main__":
    unittest.main()
