"""Own a Windows worker tree, including Python launchers and ExifTool children.

https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
import time


class BasicLimits(ct.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ct.c_int64), ("PerJobUserTimeLimit", ct.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ct.c_size_t),
                ("MaximumWorkingSetSize", ct.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ct.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class ExtendedLimits(ct.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", ct.c_uint64 * 6),
                ("ProcessMemoryLimit", ct.c_size_t), ("JobMemoryLimit", ct.c_size_t),
                ("PeakProcessMemoryUsed", ct.c_size_t), ("PeakJobMemoryUsed", ct.c_size_t)]


class Accounting(ct.Structure):
    _fields_ = [("TotalUserTime", ct.c_int64), ("TotalKernelTime", ct.c_int64),
                ("ThisPeriodTotalUserTime", ct.c_int64), ("ThisPeriodTotalKernelTime", ct.c_int64),
                ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]


class ThreadEntry(ct.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ThreadID", wt.DWORD),
                ("th32OwnerProcessID", wt.DWORD), ("tpBasePri", wt.LONG),
                ("tpDeltaPri", wt.LONG), ("dwFlags", wt.DWORD)]


class WindowsJob:
    CREATE_SUSPENDED = 0x00000004

    def __init__(self):
        self.api = ct.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ct.c_void_p, wt.LPCWSTR], wt.HANDLE),
            "SetInformationJobObject": ([wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD], wt.BOOL),
            "QueryInformationJobObject": ([wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p], wt.BOOL),
            "AssignProcessToJobObject": ([wt.HANDLE, wt.HANDLE], wt.BOOL),
            "TerminateJobObject": ([wt.HANDLE, wt.UINT], wt.BOOL),
            "CloseHandle": ([wt.HANDLE], wt.BOOL),
            "CreateToolhelp32Snapshot": ([wt.DWORD, wt.DWORD], wt.HANDLE),
            "Thread32First": ([wt.HANDLE, ct.POINTER(ThreadEntry)], wt.BOOL),
            "Thread32Next": ([wt.HANDLE, ct.POINTER(ThreadEntry)], wt.BOOL),
            "OpenThread": ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            "ResumeThread": ([wt.HANDLE], wt.DWORD),
            "OpenProcess": ([wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE),
            "WaitForSingleObject": ([wt.HANDLE, wt.DWORD], wt.DWORD),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        self._check(self.handle)
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            self._check(self.api.SetInformationJobObject(self.handle, 9, ct.byref(limits), ct.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(result):
        if not result:
            raise ct.WinError(ct.get_last_error())
        return result

    def attach_and_resume(self, process):
        # Assign before any user code can spawn descendants. Popen closes its
        # primary thread handle, so reopen that suspended thread via Toolhelp.
        self._check(self.api.AssignProcessToJobObject(self.handle, int(process._handle)))
        snapshot = self.api.CreateToolhelp32Snapshot(0x4, 0)  # SNAPTHREAD
        if snapshot == ct.c_void_p(-1).value:
            raise ct.WinError(ct.get_last_error())
        try:
            entry = ThreadEntry()
            entry.dwSize = ct.sizeof(entry)
            found = self.api.Thread32First(snapshot, ct.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self._check(self.api.OpenThread(0x2, False, entry.th32ThreadID))
                    try:
                        if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ct.WinError(ct.get_last_error())
                        return
                    finally:
                        self.api.CloseHandle(thread)
                found = self.api.Thread32Next(snapshot, ct.byref(entry))
            raise RuntimeError("Cannot find suspended worker thread")
        finally:
            self.api.CloseHandle(snapshot)

    def terminate(self, timeout=5.0):
        if self.handle is None:
            return
        handles = self._process_handles()
        try:
            self._check(self.api.TerminateJobObject(self.handle, 1))
            deadline = time.monotonic() + timeout
            while True:
                info = Accounting()
                self._check(self.api.QueryInformationJobObject(self.handle, 1, ct.byref(info), ct.sizeof(info), None))
                if info.ActiveProcesses == 0:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Worker process tree did not stop")
                time.sleep(0.01)
            # ActiveProcesses may reach zero before native/GPU teardown signals
            # the process objects. Waiting only for the launcher misses this.
            for handle in handles:
                milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
                result = self.api.WaitForSingleObject(handle, milliseconds)
                if result == 0xFFFFFFFF:
                    raise ct.WinError(ct.get_last_error())
                if result != 0:
                    raise TimeoutError("Worker process teardown did not finish")
            self.close()
        finally:
            for handle in handles:
                self.api.CloseHandle(handle)

    def _process_handles(self):
        capacity = 16
        while True:
            class ProcessIds(ct.Structure):
                _fields_ = [("assigned", wt.DWORD), ("listed", wt.DWORD),
                            ("ids", ct.c_size_t * capacity)]
            ids = ProcessIds()
            success = self.api.QueryInformationJobObject(self.handle, 3, ct.byref(ids), ct.sizeof(ids), None)
            if success:
                break
            if ct.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ct.WinError(ct.get_last_error())
            capacity = max(capacity * 2, ids.assigned)
        handles = []
        try:
            for pid in ids.ids[:ids.listed]:
                handle = self.api.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
                if handle:
                    handles.append(handle)
                elif ct.get_last_error() != 87:  # Process already exited.
                    raise ct.WinError(ct.get_last_error())
            return handles
        except BaseException:
            for handle in handles:
                self.api.CloseHandle(handle)
            raise

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None
