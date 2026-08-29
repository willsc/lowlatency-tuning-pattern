#!/usr/bin/env bash
# tuned hook - delegates to the same runtime script so there is exactly one implementation.
case "$1" in
  start) exec /opt/lowlatency-tuning-pattern/scripts/apply-runtime.sh ;;
  stop)  exit 0 ;;
esac
