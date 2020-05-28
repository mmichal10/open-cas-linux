#
# Copyright(c) 2020 Intel Corporation
# SPDX-License-Identifier: BSD-3-Clause-Clear
#

import re
import pytest
from time import sleep

from api.cas import casadm
from api.cas.cache_config import (CacheMode,
                                  CacheModeTrait,
                                  CleaningPolicy,
                                  FlushParametersAlru,
                                  Time)
from storage_devices.disk import DiskType, DiskTypeSet
from core.test_run import TestRun
from test_tools.disk_utils import Filesystem
from test_tools.fs_utils import create_random_test_file
from test_utils import os_utils
from test_utils.size import Size, Unit

mount_point = "/mnt/cas"
syslog_path = "/var/log/messages"


@pytest.mark.require_plugin("scsi_debug_fua_signals", dev_size_mb="4096", opts="1")
@pytest.mark.parametrize("cache_mode", CacheMode.with_traits(CacheModeTrait.LazyWrites))
@pytest.mark.require_disk("cache", DiskTypeSet([DiskType.optane, DiskType.nand]))
def test_flush_signal_core(cache_mode):
    """
        title: Test for FLUSH nad FUA signals sent to core device in modes with lazy writes.
        description: |
          Test if OpenCAS transmits FLUSH and FUA signals to core device in modes with lazy writes.
        pass_criteria:
          - FLUSH requests should be passed to core device.
          - FUA requests should be passed to core device.
    """
    with TestRun.step("Set mark in syslog to not read entries existing before the test."):
        Logs._read_syslog(Logs.last_read_line)

    with TestRun.step("Prepare devices for cache and core."):
        cache_dev = TestRun.disks['cache']
        cache_dev.create_partitions([Size(2, Unit.GibiByte)])
        cache_part = cache_dev.partitions[0]
        core_dev = TestRun.scsi_debug_devices[0]

    with TestRun.step("Start cache and add SCSI device with xfs filesystem as core."):
        cache = casadm.start_cache(cache_part, cache_mode)
        core_dev.create_filesystem(Filesystem.xfs)
        core = cache.add_core(core_dev)

    with TestRun.step("Mount exported object."):
        if core.is_mounted():
            core.unmount()
        core.mount(mount_point)

    with TestRun.step("Turn off cleaning policy."):
        cache.set_cleaning_policy(CleaningPolicy.nop)

    with TestRun.step("Create temporary file on exported object."):
        tmp_file = create_random_test_file(f"{mount_point}/tmp.file", Size(1, Unit.GibiByte))
        os_utils.sync()

    with TestRun.step("Flush cache."):
        cache.flush_cache()
        os_utils.sync()

    with TestRun.step(f"Check {syslog_path} for flush request and delete temporary file."):
        Logs.check_syslog_for_signals()
        tmp_file.remove(True)

    with TestRun.step("Create temporary file on exported object."):
        tmp_file = create_random_test_file(f"{mount_point}/tmp.file", Size(1, Unit.GibiByte))
        os_utils.sync()

    with TestRun.step("Flush core."):
        core.flush_core()
        os_utils.sync()

    with TestRun.step(f"Check {syslog_path} for flush request and delete temporary file."):
        Logs.check_syslog_for_signals()
        tmp_file.remove(True)

    with TestRun.step("Turn on alru cleaning policy and set policy params."):
        cache.set_cleaning_policy(CleaningPolicy.alru)
        cache.set_params_alru(FlushParametersAlru(
            Time(milliseconds=5000), 10000, Time(seconds=10), Time(seconds=10))
        )

    with TestRun.step("Create big temporary file on exported object."):
        tmp_file = create_random_test_file(f"{mount_point}/tmp.file", Size(5, Unit.GibiByte))
        os_utils.sync()

    with TestRun.step("Wait for automatic flush from alru cleaning policy and check log."):
        wait_time = (
            int(cache.get_flush_parameters_alru().staleness_time.total_seconds())
            + int(cache.get_flush_parameters_alru().activity_threshold.total_seconds())
            + int(cache.get_flush_parameters_alru().wake_up_time.total_seconds())
            + 5
        )
        sleep(wait_time)

    with TestRun.step(f"Check {syslog_path} for flush request and delete temporary file."):
        Logs.check_syslog_for_signals()
        tmp_file.remove(True)

    with TestRun.step("Create temporary file on exported object."):
        create_random_test_file(f"{mount_point}/tmp.file", Size(1, Unit.GibiByte))
        os_utils.sync()

    with TestRun.step("Unmount exported object and remove it from cache."):
        core.unmount()
        core.remove_core()
        os_utils.sync()

    with TestRun.step(f"Check {syslog_path} for flush request."):
        Logs.check_syslog_for_signals()

    with TestRun.step("Stop cache."):
        cache.stop()


class Logs:
    last_read_line = 1
    FLUSH = re.compile(r"scsi_debug:[\s\S]*cmd 35")
    FUA = re.compile(r"scsi_debug:[\s\S]*cmd 2a 08")

    @staticmethod
    def check_syslog_for_signals():
        Logs.check_syslog_for_flush()
        Logs.check_syslog_for_fua()

    @staticmethod
    def check_syslog_for_flush():
        """Check syslog for FLUSH logs"""
        log_lines = Logs._read_syslog(Logs.last_read_line)
        flush_logs_counter = Logs._count_logs(log_lines, Logs.FLUSH)
        log_type = "FLUSH"
        Logs._validate_logs_amount(flush_logs_counter, log_type)

    @staticmethod
    def check_syslog_for_fua():
        """Check syslog for FUA logs"""
        log_lines = Logs._read_syslog(Logs.last_read_line)
        fua_logs_counter = Logs._count_logs(log_lines, Logs.FUA)
        log_type = "FUA"
        Logs._validate_logs_amount(fua_logs_counter, log_type)

    @staticmethod
    def _read_syslog(last_read_line: int):
        """Read recent lines in syslog, mark last line and return read lines as list."""
        log_lines = TestRun.executor.run_expect_success(
            f"tail -qn +{last_read_line} {syslog_path}"
        ).stdout.splitlines()
        # mark last read line to continue next reading from here
        Logs.last_read_line += len(log_lines)

        return log_lines

    @staticmethod
    def _count_logs(log_lines: list, expected_log):
        """Count specified log in list and return its amount."""
        logs_counter = 0

        for line in log_lines:
            is_log_in_line = expected_log.search(line)
            if is_log_in_line is not None:
                logs_counter += 1

        return logs_counter

    @staticmethod
    def _validate_logs_amount(logs_counter: int, log_type: str):
        """Validate amount of logs and return"""
        if logs_counter == 0:
            if Logs._is_flush(log_type):
                TestRun.LOGGER.error(f"{log_type} log not occured")
            else:
                TestRun.LOGGER.warning(f"{log_type} log not occured")
        elif logs_counter == 1:
            TestRun.LOGGER.warning(f"{log_type} log occured only once.")
        else:
            TestRun.LOGGER.info(f"{log_type} log occured {logs_counter} times.")

    @staticmethod
    def _is_flush(log_type: str):
        return log_type == "FLUSH"
