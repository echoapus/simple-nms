"""Select the configured UDP Syslog collector."""

import queue

from collectors.rust_syslog import RustSyslogCollector
from collectors.syslog_listener import SyslogCollector


def build_syslog_collector(collector: str, write_queue: "queue.Queue[dict]", host: str,
                           port: int, deny_ips, runtime_dir: str):
    if collector == "rust":
        return RustSyslogCollector(write_queue, host, port, deny_ips, runtime_dir)
    if collector == "python":
        return SyslogCollector(write_queue, host, port, deny_ips)
    raise ValueError(f"unknown UDP Syslog collector: {collector}")
