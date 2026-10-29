"""Offline characterization of a missing manager process restart, not desired behavior.

These assertions record the current retained-dead-process behavior. If automatic
restart is implemented, update this evidence rather than preserving the defect.
Only a harmless local child is launched: no card, manager loop, Panda, ECU,
actuation, diagnostics, messaging transport, or network telemetry is exercised.
An offroad/onroad process lifecycle is not evidence of ECU or Panda recovery.
"""

import logging
import os
import unittest
from multiprocessing import Process
from tempfile import TemporaryDirectory
from unittest.mock import patch

from opendbc.car.structs import car
from openpilot.common.hardware import PC
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.system.manager import process


EXIT_CODE = 23
JOIN_TIMEOUT = 3.0


def harmless_nonzero_launcher(_module: str, _name: str) -> None:
  # A top-level multiprocessing target, bypassing the production launcher and
  # its module import, messaging reset, and Sentry exception reporting entirely.
  raise SystemExit(EXIT_CODE)


def onroad_only(started: bool, _params: Params, _cp: car.CarParams) -> bool:
  return started


@unittest.skipUnless(PC, "Offline host characterization only; do not run on vehicle hardware")
class TestMissingProcessRecoveryCharacterization(unittest.TestCase):
  def setUp(self):
    temporary_root = self.enterContext(TemporaryDirectory(prefix="process-recovery-evidence-"))
    # Do not let an inherited LOG_ROOT direct OpenpilotPrefix cleanup at real logs.
    self.enterContext(patch.dict(os.environ, {
      "LOG_ROOT": os.path.join(temporary_root, "logs"),
      "COMMA_CACHE": os.path.join(temporary_root, "cache"),
    }))
    self.enterContext(OpenpilotPrefix())
    self.params = Params()
    self.cp = car.CarParams.new_message()
    # Suppress the production IPC log handler; keep lifecycle code unmodified.
    offline_log = logging.Logger("offline-process-characterization")
    offline_log.addHandler(logging.NullHandler())
    self.enterContext(patch.object(process, "cloudlog", offline_log))
    self.managed = process.PythonProcess("offline_nonzero_child", "unused.offline.target", onroad_only)
    self.managed.launcher = harmless_nonzero_launcher
    self.children: list[Process] = []
    self.addCleanup(self.cleanup_children)

  def cleanup_children(self):
    # Avoid ManagerProcess.stop's unbounded final join if an assertion fails.
    children = self.children.copy()
    if self.managed.proc is not None and self.managed.proc not in children:
      children.append(self.managed.proc)
    for child in children:
      if child.pid is not None:
        if child.is_alive():
          child.kill()
        child.join(JOIN_TIMEOUT)
        self.assertFalse(child.is_alive(), "local child did not exit within cleanup bound")
        child.close()
    self.managed.proc = None

  def ensure(self, started: bool):
    self.params.put_bool("IsOffroad", not started, block=True)
    return process.ensure_running({self.managed.name: self.managed}.values(), started, self.params, self.cp)

  def start_and_observe_exit(self):
    self.assertEqual(self.ensure(True), [self.managed])
    child = self.managed.proc
    self.assertIsNotNone(child)
    self.children.append(child)
    child.join(JOIN_TIMEOUT)
    self.assertFalse(child.is_alive(), "harmless child did not exit within test bound")
    self.assertEqual(child.exitcode, EXIT_CODE)
    return child

  def assert_reported_dead_but_expected(self, child):
    # manager.py builds managerState.processes with this exact state method.
    state = self.managed.get_process_state_msg()
    self.assertEqual(state.name, self.managed.name)
    self.assertTrue(state.shouldBeRunning)
    self.assertFalse(state.running)
    self.assertEqual(state.exitCode, EXIT_CODE)
    self.assertEqual(state.pid, child.pid)

  def test_current_gap_repeated_onroad_ensure_does_not_restart_dead_child(self):
    child = self.start_and_observe_exit()
    self.assert_reported_dead_but_expected(child)
    for _ in range(5):
      self.assertEqual(self.ensure(True), [self.managed])
      self.assertIs(self.managed.proc, child)
      self.assertFalse(self.managed.shutting_down)
      self.assert_reported_dead_but_expected(child)

  def test_explicit_offroad_cleanup_allows_new_onroad_child_not_hardware_recovery(self):
    first_child = self.start_and_observe_exit()
    self.assert_reported_dead_but_expected(first_child)

    self.assertEqual(self.ensure(False), [])
    self.assertIsNone(self.managed.proc)
    self.assertFalse(self.managed.shutting_down)
    offroad_state = self.managed.get_process_state_msg()
    self.assertFalse(offroad_state.shouldBeRunning)
    self.assertFalse(offroad_state.running)
    self.assertEqual(offroad_state.pid, 0)
    # Cleared manager state no longer carries the prior failure exit code.
    self.assertEqual(offroad_state.exitCode, 0)
    self.assertEqual(first_child.exitcode, EXIT_CODE)

    second_child = self.start_and_observe_exit()
    self.assertIsNot(second_child, first_child)
    self.assertNotEqual(second_child.pid, first_child.pid)
    self.assert_reported_dead_but_expected(second_child)


if __name__ == "__main__":
  unittest.main(verbosity=2)
