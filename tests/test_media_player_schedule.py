"""Exercise the shipped actions with device state and volume reports."""
import ast
from datetime import datetime
from types import SimpleNamespace
import unittest

from jinja2 import StrictUndefined
from jinja2.nativetypes import NativeEnvironment

from test_manual_override_and_modes import load


class PlayerSchedule:
    def __init__(self, state="idle", volume=0.5, features=4101, **options):
        self.bp = load("media-player-schedule-control.yaml")
        self.inputs = {key: spec["default"] for key, spec in self.bp["blueprint"]["input"].items()
                       if "default" in spec}
        self.inputs.update(media_players=["media_player.test"], playback_block_enabled=True,
                           playback_block_action="volume_zero")
        self.inputs.update(options)
        self.players = {"media_player.test": dict(state=state, volume_level=volume,
                                                 supported_features=features)}
        self.failed_actions = set()
        self.env = NativeEnvironment(undefined=StrictUndefined)
        self.env.filters["bitwise_and"] = lambda value, mask: value & mask
        self.env.globals.update(
            states=lambda entity: self.players[entity]["state"],
            state_attr=lambda entity, attr: self.players[entity].get(attr),
            is_number=lambda value: isinstance(value, (int, float)))

    def render(self, value, context):
        if isinstance(value, dict):
            if set(value) == {"input"}:
                return self.inputs[value["input"]]
            return {key: self.render(item, context) for key, item in value.items()}
        if isinstance(value, str) and ("{{" in value or "{%" in value):
            result = self.env.from_string(value).render(**context)
            if isinstance(result, str):
                try:
                    return ast.literal_eval(result.strip())
                except (ValueError, SyntaxError):
                    return result.strip()
            return result
        return value

    def run(self, hour="23:00:00", trigger=None):
        current = datetime.fromisoformat("2026-11-18T" + hour)
        self.env.globals.update(now=lambda: current, today_at=lambda value: datetime.combine(
            current.date(), datetime.strptime(value, "%H:%M:%S").time()))
        commands = []

        def passes(conditions, context):
            return all(self.render(item["value_template"], context) for item in conditions)

        def execute(sequence, context):
            for step in sequence:
                if "variables" in step:
                    for key, value in step["variables"].items():
                        context[key] = self.render(value, context)
                elif "repeat" in step:
                    for entity in self.render(step["repeat"]["for_each"], context):
                        execute(step["repeat"]["sequence"], dict(context, repeat=SimpleNamespace(item=entity)))
                elif "choose" in step:
                    for branch in step["choose"]:
                        if passes(branch["conditions"], context):
                            execute(branch["sequence"], context)
                            break
                elif "if" in step:
                    if passes(step["if"], context):
                        execute(step["then"], context)
                elif "action" in step:
                    action = self.render(step["action"], context)
                    entity = self.render(step["target"]["entity_id"], context)
                    data = self.render(step.get("data", {}), context)
                    commands.append((action, entity, data))
                    if (action, entity) in self.failed_actions:
                        assert step["continue_on_error"] is True
                        continue
                    if action == "media_player.volume_set":
                        self.players[entity]["volume_level"] = data["volume_level"]
                    # Deliberately keep the reported playback state stale.
                else:
                    raise AssertionError(step)

        variables = self.render(self.bp["variables"], {})
        if trigger is not None:
            variables["trigger"] = trigger
        execute(self.bp["actions"], variables)
        return commands


class PlaybackBlock(unittest.TestCase):
    def actions(self, commands):
        return [command[0] for command in commands]

    def test_zero_mode_handles_wrong_playback_states(self):
        for state in ["idle", "paused", "on", "off", "unknown", "playing", "buffering"]:
            with self.subTest(state=state):
                schedule = PlayerSchedule(state=state)
                commands = schedule.run()
                self.assertEqual(self.actions(commands), ["media_player.volume_set", "media_player.media_stop"])
                self.assertEqual(commands[0][2], {"volume_level": 0})
                self.assertEqual(schedule.run(), [])

    def test_raising_volume_repeats_zero_and_stop_with_stale_status(self):
        schedule = PlayerSchedule(state="idle", volume=0)
        self.assertEqual(schedule.run(), [])
        for volume in [0.01, 0.8, 0.001]:
            schedule.players["media_player.test"]["volume_level"] = volume
            self.assertEqual(self.actions(schedule.run()), ["media_player.volume_set", "media_player.media_stop"])
            self.assertEqual(schedule.run(), [])

    def test_muted_yandex_station_keeps_reported_volume_without_repeated_commands(self):
        schedule = PlayerSchedule(state="paused", volume=0.25, features=5)
        player = schedule.players["media_player.test"]
        player["is_volume_muted"] = True
        self.assertEqual(schedule.run(trigger={"id": "periodic_check"}), [])
        self.assertEqual(schedule.run(trigger={"id": "player_mute"}), [])
        player["is_volume_muted"] = False
        self.assertEqual(self.actions(schedule.run(trigger={"id": "player_mute"})),
                         ["media_player.volume_set", "media_player.media_pause"])

    def test_new_playback_stops_only_the_triggering_player_even_when_silent(self):
        for state in ["playing", "buffering"]:
            for volume, muted in [(0, False), (0.25, True)]:
                with self.subTest(state=state, volume=volume, muted=muted):
                    schedule = PlayerSchedule(state=state, volume=volume, features=5)
                    player = schedule.players["media_player.test"]
                    player["is_volume_muted"] = muted
                    schedule.inputs["media_players"].append("media_player.other")
                    schedule.players["media_player.other"] = player.copy()
                    trigger = dict(id="player_state", entity_id="media_player.test", to_state=dict(state=state))
                    self.assertEqual(schedule.run(trigger=trigger),
                                     [("media_player.media_pause", "media_player.test", {})])
                    self.assertEqual(schedule.run(trigger={"id": "periodic_check"}), [])
                    player["state"] = "paused"
                    self.assertEqual(schedule.run(trigger=trigger), [])

    def test_block_start_and_startup_stop_already_playing_silent_device(self):
        schedule = PlayerSchedule(state="playing", volume=0)
        for event in ["playback_block_start", "startup"]:
            self.assertEqual(self.actions(schedule.run(hour="22:00:00", trigger={"id": event})),
                             ["media_player.media_stop"])
        self.assertEqual(schedule.run(trigger={"id": "schedule_boundary"}), [])

    def test_volume_events_are_monitored(self):
        triggers = load("media-player-schedule-control.yaml")["triggers"]
        for attribute in ("volume_level", "is_volume_muted"):
            self.assertTrue(any(t.get("attribute") == attribute for t in triggers))
        start = next(t for t in triggers if t["id"] == "playback_block_start")
        self.assertEqual(start["at"], {"input": "playback_block_start"})

    def test_unknown_volume_is_set_without_waiting_for_a_numeric_report(self):
        for volume in [None, "unknown"]:
            with self.subTest(volume=volume):
                self.assertEqual(self.actions(PlayerSchedule(volume=volume).run()),
                                 ["media_player.volume_set", "media_player.media_stop"])

    def test_zero_mode_works_with_normal_volume_management_disabled(self):
        self.assertEqual(len(PlayerSchedule(volume_enabled=False).run()), 2)

    def test_zero_mode_uses_supported_stop_fallback(self):
        for features, expected in [(4101, "media_stop"), (5, "media_pause"), (260, "turn_off"), (4, None)]:
            with self.subTest(features=features):
                expected_actions = ["media_player.volume_set"]
                if expected:
                    expected_actions.append("media_player." + expected)
                self.assertEqual(self.actions(PlayerSchedule(features=features).run()), expected_actions)

    def test_unsupported_or_unavailable_players_are_skipped(self):
        self.assertEqual(PlayerSchedule(state="unavailable").run(), [])
        self.assertEqual(PlayerSchedule(features=4096).run(), [])

    def test_volume_failure_does_not_prevent_stop_or_other_players(self):
        schedule = PlayerSchedule()
        schedule.inputs["media_players"].append("media_player.other")
        schedule.players["media_player.other"] = dict(state="idle", volume_level=0.8, supported_features=4101)
        schedule.failed_actions.add(("media_player.volume_set", "media_player.test"))
        schedule.failed_actions.add(("media_player.media_stop", "media_player.test"))
        self.assertEqual(self.actions(schedule.run()),
                         ["media_player.volume_set", "media_player.media_stop"] * 2)

    def test_schedule_boundaries_and_disabled_block(self):
        for hour, expected_count in [("21:59:59", 0), ("22:00:00", 2), ("00:00:00", 2),
                                     ("06:59:59", 2), ("07:00:00", 0)]:
            with self.subTest(hour=hour):
                self.assertEqual(len(PlayerSchedule(volume_enabled=False).run(hour)), expected_count)
        self.assertEqual(PlayerSchedule(playback_block_enabled=False, volume_enabled=False).run(), [])

    def test_original_stop_modes_keep_their_state_checks(self):
        for mode, features, expected in [("auto", 4101, "media_stop"), ("auto", 5, "media_pause"),
                                         ("auto", 260, "turn_off"), ("media_pause", 4101, "media_pause"),
                                         ("turn_off", 4101, None)]:
            for state in ["playing", "buffering", "idle"]:
                with self.subTest(mode=mode, state=state):
                    commands = PlayerSchedule(state=state, features=features, playback_block_action=mode).run()
                    expected_actions = ["media_player." + expected] if expected and state != "idle" else []
                    self.assertEqual(self.actions(commands), expected_actions)

    def test_zero_does_not_restore_old_volume_at_end_of_block(self):
        schedule = PlayerSchedule()
        schedule.run()
        self.assertEqual(schedule.run("07:00:00"), [])
        self.assertEqual(schedule.players["media_player.test"]["volume_level"], 0)

    def test_outside_schedule_can_apply_its_configured_volume_after_block(self):
        schedule = PlayerSchedule(outside_volume_enabled=True, outside_volume_percent=40)
        schedule.run()
        self.assertEqual(schedule.run("08:00:00"),
                         [("media_player.volume_set", "media_player.test", {"volume_level": 0.4})])


if __name__ == "__main__":
    unittest.main()
