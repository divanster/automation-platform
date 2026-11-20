"""Child-process workers for the real multiprocess tests of the Windows DPAPI secret store (AI-SEC-1c). Test-only; run as
``python -m tests.secret_store_workers <mode> <directory> <timeout> <count> <tag>`` from the backend directory.

Every value is synthetic (``sk-test-SENTINEL-conc-...``). A worker prints one JSON line of counters and codes and never a value, a reference or a path."""

import collections
import json
import os
import re
import sys
import time

REFERENCE = "AP_AI_CRED_CONCURRENCY"
PREFIX = "sk-test-SENTINEL-conc-"
VALID = re.compile(r"sk-test-SENTINEL-conc-[a-z]+-\d{5}")


def value(tag: str, index: int) -> str:
    return f"{PREFIX}{tag}-{index:05d}"


def run(mode, directory, timeout, count, tag):
    from apps.secret_store import SecretStoreError
    from config import secret_store_dpapi as module

    store = module.WindowsDpapiSecretStore(directory, timeout=timeout)
    codes = collections.Counter()
    report = {}

    def attempt(call):
        try:
            call()
            codes["ok"] += 1
            return True
        except SecretStoreError as error:
            codes[error.code] += 1
            return False

    if mode == "reader":
        bad = 0
        for _ in range(count):
            try:
                text = store.get(REFERENCE).reveal()
                codes["ok"] += 1
                bad += 0 if VALID.fullmatch(text) else 1
            except SecretStoreError as error:
                codes[error.code] += 1
        report["bad"] = bad
    elif mode == "rotator":
        for index in range(count):
            attempt(lambda: store.put(REFERENCE, value(tag, index), replace=True))
    elif mode == "creator":
        attempt(lambda: store.put(REFERENCE, value(tag, 0)))
        report["tag"] = tag
    elif mode == "replacer":
        starts = []
        for index in range(count):
            began = time.perf_counter()
            if attempt(lambda: store.put(REFERENCE, value(tag, index), replace=True)):
                starts.append(began)
        report["ok_starts"] = starts
    elif mode == "deleter":
        done = []
        for _ in range(count):
            if attempt(lambda: store.delete(REFERENCE)):
                done.append(time.perf_counter())
        report["done"] = done
    elif mode == "rawcheck":
        path = os.path.join(directory, module.file_name_for(REFERENCE))
        protector = module.WindowsDpapiProtector()
        bad = 0
        for _ in range(count):
            try:
                with open(path, "rb") as handle:
                    content = handle.read()
            except OSError:
                codes["unreadable_file"] += 1  # replaced or locked at that instant: not a torn read
                continue
            try:
                assert content.startswith(module.FORMAT_TAG)
                text = protector.unprotect(content[len(module.FORMAT_TAG) :], module.entropy_for(REFERENCE)).decode("utf-8")
                assert VALID.fullmatch(text)
                codes["ok"] += 1
            except Exception:
                bad += 1
        report["bad"] = bad
    elif mode == "hold":
        with module.WindowsFileLocker().hold(directory, tag == "exclusive", 5.0):
            print("locked", flush=True)
            time.sleep(120)
        return None
    elif mode == "stall":
        def stall(descriptor):
            print("writing", flush=True)
            time.sleep(120)

        module.os.fsync = stall  # only this child: the process is killed while the partial file exists and the lock is held
        store.put(REFERENCE, value(tag, 1), replace=(tag != "create"))
        return None
    else:
        raise SystemExit(2)
    report["codes"] = dict(codes)
    return report


if __name__ == "__main__":
    mode, directory, timeout, count, tag = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
    result = run(mode, directory, timeout, count, tag)
    if result is not None:
        print(json.dumps(result), flush=True)
