"""Name the signals that cancel a run."""
import signal


CANCELLATION_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
