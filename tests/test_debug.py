import pytest
import logging
from src.live_execution.execution_guard import ExecutionGuard

def test_debug(caplog):
    print('\nDEBUG START')
    log = logging.getLogger('src.live_execution.execution_guard')
    print('PROPAGATE:', log.propagate)
    print('LOG LEVEL:', log.level)
    print('ROOT LEVEL:', logging.getLogger().level)
    print('LOG DISABLED:', log.disabled)
    
    with caplog.at_level(logging.WARNING):
        log.warning("THIS IS A TEST WARNING")
        print('CAPLOG TEXT:', caplog.text)
    
    print('DEBUG END\n')
