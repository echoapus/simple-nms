"""SNMP Trap collector — listens on UDP 162 via pysnmp with MIB resolution."""

import json
import logging
import queue
import threading
from datetime import datetime, timezone

from metrics import runtime_metrics

logger = logging.getLogger(__name__)

try:
    from pysnmp.carrier.asyncio.dgram import udp
    from pysnmp.entity import config as snmp_config
    from pysnmp.entity import engine
    from pysnmp.entity.rfc3413 import ntfrcv
    from pysnmp.smi import builder, view
    from pysnmp.smi.rfc1902 import ObjectIdentity
    _PYSNMP_OK = True
except ImportError:
    _PYSNMP_OK = False
    logger.warning("pysnmp not installed — SNMP trap collector will be unavailable")

# Optional: pysmi enables compiling MIB sources (ASN.1 → Python)
try:
    from pysnmp.smi import compiler as smi_compiler
    _PYSMI_OK = True
except ImportError:
    _PYSMI_OK = False


class MibResolver:
    """Resolves numeric OIDs to human-readable MIB names with caching.

    Attempts to resolve OIDs like '1.3.6.1.6.3.1.1.5.3' into
    'SNMPv2-MIB::linkDown'. Falls back to raw OID on failure.
    """

    def __init__(self, snmp_engine, mib_dirs=None, mib_modules=None):
        self._cache = {}
        self._mib_view = None

        try:
            mib_builder = snmp_engine.get_mib_builder()

            # Use pysnmp's built-in MIB compiler (pysmi) which automatically:
            # 1. Reads ASN.1 source files from mib_dirs
            # 2. Compiles them to Python format
            # 3. Caches compiled MIBs in ~/.pysnmp/mibs/
            # 4. Resolves all dependencies between MIBs
            if _PYSMI_OK and mib_dirs:
                sources = [f"file://{d}" for d in mib_dirs]
                try:
                    smi_compiler.add_mib_compiler(mib_builder, sources=sources)
                    logger.info("MIB compiler enabled with sources: %s", sources)
                except Exception:
                    logger.exception("MIB compiler setup failed, trying without custom sources")
                    try:
                        smi_compiler.add_mib_compiler(mib_builder)
                    except Exception:
                        pass
            elif _PYSMI_OK:
                try:
                    smi_compiler.add_mib_compiler(mib_builder)
                    logger.info("MIB compiler enabled (default sources)")
                except Exception:
                    pass

            # Pre-load MIB modules — pysmi will compile from ASN.1 on first load
            default_modules = [
                "SNMPv2-MIB", "SNMPv2-SMI", "IF-MIB", "IP-MIB",
                "TCP-MIB", "UDP-MIB", "HOST-RESOURCES-MIB",
                "ENTITY-MIB", "BRIDGE-MIB",
            ]
            load_list = mib_modules if mib_modules else default_modules
            loaded = []
            failed = []
            for mod in load_list:
                try:
                    mib_builder.load_modules(mod)
                    loaded.append(mod)
                except Exception as e:
                    failed.append(mod)
                    logger.debug("MIB module %s not loaded: %s", mod, e)
            if loaded:
                logger.info("MIB modules loaded (%d/%d): %s",
                            len(loaded), len(load_list), ", ".join(loaded))
            if failed:
                logger.warning("MIB modules not found (%d): %s",
                               len(failed), ", ".join(failed))

            self._mib_view = view.MibViewController(mib_builder)
            logger.info("MIB resolver initialised")

        except Exception:
            logger.exception("MIB resolver init failed — OIDs will be shown as raw numbers")

    def resolve(self, oid_str: str) -> str:
        """Resolve a dotted-decimal OID string to MODULE::name format.

        Returns the resolved name, or the original OID if resolution fails.
        Results are cached for performance.
        """
        if oid_str in self._cache:
            return self._cache[oid_str]

        if not self._mib_view:
            return oid_str

        try:
            oid = ObjectIdentity(oid_str)
            oid.resolve_with_mib(self._mib_view)
            resolved = oid.prettyPrint()

            # Skip unhelpful resolutions like "SNMPv2-SMI::enterprises.99999.1"
            # that just prepend a generic prefix without real meaning
            if "::enterprises." in resolved or "::mib-2." in resolved:
                self._cache[oid_str] = oid_str
                return oid_str

            self._cache[oid_str] = resolved
            return resolved

        except Exception:
            self._cache[oid_str] = oid_str
            return oid_str


class SNMPTrapCollector(threading.Thread):
    """pysnmp-based SNMP trap receiver with MIB resolution."""

    def __init__(self, write_queue: "queue.Queue[dict]",
                 host: str = "0.0.0.0", port: int = 162,
                 community: str = "public",
                 mib_dirs: list = None, mib_modules: list = None):
        super().__init__(daemon=True, name="snmptrap-collector")
        self.q = write_queue
        self.host = host
        self.port = port
        self.community = community
        self.mib_dirs = mib_dirs
        self.mib_modules = mib_modules
        self._engine = None
        self._resolver = None
        self._last_transport_address = None  # captured by observer

    def _resolve_varbinds(self, var_binds) -> dict:
        """Convert varbind list to dict with resolved OID keys."""
        result = {}
        for oid, val in var_binds:
            raw_oid = oid.prettyPrint()
            resolved_key = self._resolver.resolve(raw_oid) if self._resolver else raw_oid
            result[resolved_key] = val.prettyPrint()
        return result

    def _source_ip_from_state(self, snmp_engine, state_reference) -> str | None:
        """Resolve source IP from per-message state before using observer fallback."""
        dispatch = snmp_engine.msgAndPduDsp
        for method_name in ("get_transport_info", "getTransportInfo"):
            method = getattr(dispatch, method_name, None)
            if not method:
                continue
            try:
                transport_info = method(state_reference)
                if transport_info:
                    return str(transport_info[1][0])
            except Exception:
                pass

        observer = snmp_engine.observer
        for method_name in ("get_execution_context", "getExecutionContext"):
            method = getattr(observer, method_name, None)
            if not method:
                continue
            try:
                exec_ctx = method('rfc3412.receiveMessage:request')
                if exec_ctx:
                    addr = exec_ctx.get('transportAddress')
                    if addr:
                        return str(addr[0])
            except Exception:
                pass

        if self._last_transport_address:
            try:
                return str(self._last_transport_address[0])
            except (IndexError, TypeError):
                return None
        return None

    def _callback(self, snmp_engine, state_reference, context_engine_id,
                  context_name, var_binds, cb_ctx):
        """Called by pysnmp for every incoming trap/inform."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")

        vb_dict = self._resolve_varbinds(var_binds)

        # Extract snmpTrapOID — try resolved key first, then raw
        trap_oid_raw = None
        trap_oid_resolved = None
        for key, val in vb_dict.items():
            if "snmpTrapOID" in key or key == "1.3.6.1.6.3.1.1.4.1.0":
                trap_oid_raw = val
                break

        if trap_oid_raw and self._resolver:
            trap_oid_resolved = self._resolver.resolve(trap_oid_raw)
        else:
            trap_oid_resolved = trap_oid_raw

        # Build a readable payload summary
        # Format: "TrapName — key1=val1, key2=val2"
        display_name = trap_oid_resolved or trap_oid_raw or "unknown"
        detail_parts = []
        for k, v in vb_dict.items():
            if "snmpTrapOID" in k or k == "1.3.6.1.6.3.1.1.4.1.0":
                continue  # skip the trap OID itself from details
            if "sysUpTime" in k or k == "1.3.6.1.2.1.1.3.0":
                continue  # skip sysUpTime noise
            detail_parts.append(f"{k}={v}")
        payload = display_name
        if detail_parts:
            payload += " — " + ", ".join(detail_parts)

        src_ip = self._source_ip_from_state(snmp_engine, state_reference)

        evt = {
            "ts": now,
            "src_ip": src_ip,
            "type": "snmptrap",
            "facility": None,
            "severity": None,
            "oid": trap_oid_resolved or trap_oid_raw,
            "varbinds": json.dumps(vb_dict, ensure_ascii=False),
            "payload": payload,
            "tags": None,
        }
        try:
            self.q.put_nowait(evt)
        except queue.Full:
            runtime_metrics.inc_dropped("snmptrap")
            logger.warning("SNMP trap from %s dropped because write queue is full", src_ip)
            return
        logger.debug("SNMP trap from %s: %s", src_ip, display_name)

    def run(self) -> None:
        if not _PYSNMP_OK:
            logger.error("Cannot start SNMP trap collector — pysnmp missing")
            return

        # pysnmp 7.x requires an asyncio event loop in non-main threads
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        self._engine = engine.SnmpEngine()

        # Register observer to capture transport address (source IP)
        # This fires on every incoming message before the callback
        def _transport_observer(snmp_engine, execpoint, variables, cb_ctx):
            self._last_transport_address = variables.get('transportAddress')
            logger.debug("Observer captured transportAddress: %s", self._last_transport_address)

        try:
            # pysnmp 7.x uses snake_case
            self._engine.observer.register_observer(
                _transport_observer,
                'rfc3412.receiveMessage:request',
                'rfc3412.receiveMessage:response',
            )
            logger.info("Transport observer registered for source IP capture")
        except AttributeError:
            try:
                # pysnmp 4.x/5.x uses camelCase
                self._engine.observer.registerObserver(
                    _transport_observer,
                    'rfc3412.receiveMessage:request',
                    'rfc3412.receiveMessage:response',
                )
                logger.info("Transport observer registered (legacy API)")
            except Exception as e:
                logger.warning("Could not register transport observer: %s", e)
        except Exception as e:
            logger.warning("Could not register transport observer: %s", e)

        # SNMPv1/v2c community string — must be configured BEFORE loading extra MIBs
        snmp_config.addV1System(self._engine, "default-area", self.community)

        # Transport
        snmp_config.addTransport(
            self._engine,
            udp.domainName,
            udp.UdpTransport().openServerMode((self.host, self.port)),
        )

        # Initialise MIB resolver AFTER engine is fully configured
        self._resolver = MibResolver(
            self._engine,
            mib_dirs=self.mib_dirs,
            mib_modules=self.mib_modules,
        )

        ntfrcv.NotificationReceiver(self._engine, self._callback)

        logger.info("SNMP trap collector listening on %s:%d/udp", self.host, self.port)
        self._engine.transportDispatcher.jobStarted(1)

        try:
            self._engine.transportDispatcher.runDispatcher()
        except Exception:
            logger.exception("SNMP dispatcher error")
        finally:
            self._engine.transportDispatcher.closeDispatcher()

    def stop(self) -> None:
        if self._engine:
            self._engine.transportDispatcher.jobFinished(1)
